"""Read a log folder, parse it loosely, build a SQLite index, and serve the queries the web page needs.

Parsing relies on only a few anchors; fields that are not found stay empty, and the full raw line is always kept.
The anchor regexes are all in [parser] of logview.ini ([parser.<name>] for files in another style); this file
only has the logic that uses them:
- Module tag (TANK1): a line without a module tag is a continuation line of the previous record and is appended to its text
- Function = function name (Foo() or CClass::Foo), empty if none
- Sub-module = group sub of [parser] module (same match as the module), else [parser] submodule, the second level under a module (tree, swimlane rows, module filter);
  with submodule empty it is the function name, so the hierarchy is module -> function as before
- A line with a module tag but no UNIX time: its own record, time inherited from previous line (ts_inh=1);
  a captured number outside TS_MIN..TS_MAX (e.g. a 10-digit serial number) is not a time
- CMD_xxx: event; HandleAlarm: alarm raised, RESET: cleared, matched by the alarmindex code
- Alarm name: lines with both code and name build the alarm_names table; alarm lines with only the name are mapped back to the code before pairing
- Alarm level: goes to the most recent alarm of the same module (same line or within LEVEL_WINDOW records after it), stored in alarm_levels
- Abnormal ([parser] abnormal): level = 'abn', drawn as a red diamond in the swimlanes
- [exclude] modules / submodules: those records and their continuation lines are dropped while parsing
- Rolling files (log0058.log -> log0059.log): the last record of one file is joined to the start of the next

The index is cached at ~/.logview_cache/<folder hash>.sqlite and rebuilt when the log files or the config change.
Table lines holds all records sorted by time (rowid is the order on the timeline);
intervals holds the paired intervals (command start to end, alarm to clear);
for the Regex check tab: rxstats has each regex's hit counts and captured values, shapes has the "shape" of missed lines with one raw example.

Used by: app.py creates Index and calls its query methods; the AI export uses ix.rows() / ix.fetch().
"""
import array
import bisect
import csv
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict

SCHEMA_VERSION = "minimal-9"   # bump when the table layout or stored labels change; old caches are then invalidated

LEVEL_WINDOW = 20   # when the alarm level comes after the alarm, how many records of the same module to look ahead
# A captured time outside this range is not a real time (e.g. a serial number 0000000001 or 9999999999 that happens to be 10 digits);
# the line inherits the previous time instead, so one stray number can't stretch the timeline back to 1970
TS_MIN, TS_MAX = 946684800, 4102444800   # 2000-01-01 .. 2100-01-01
EVENT_LIMIT = 20000  # max event points sent to the swimlanes at once; beyond that, plain events are thinned out evenly

_shape_alpha = re.compile(r"[A-Za-z]+")
_shape_digit = re.compile(r"\d+")
_shape_wide = re.compile(r"[^\x00-\x7f]+")


def shape(line):
    """Turn a line into its "shape": letters→a, digits→9, non-ASCII→あ. Contains no raw text; used by the health check."""
    s = _shape_wide.sub("あ", line.strip())
    s = _shape_alpha.sub("a", s)
    return _shape_digit.sub("9", s)[:120]


def _natural(name):
    """log9.log sorts before log10.log."""
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", name)]


def list_files(folder, globs):
    """A wildcard can be a file name only (*.log) or include a folder (sys/*.log)."""
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=_natural)
        for name in sorted(files, key=_natural):
            rel = os.path.relpath(os.path.join(root, name), folder).replace("\\", "/").lower()
            if any(fnmatch.fnmatch(name.lower(), g.lower()) or fnmatch.fnmatch(rel, g.lower()) for g in globs):
                out.append(os.path.join(root, name))
    return out


def same_series(a, b):
    """Whether b is the next rolling file after a: same folder, names differ only in the last number, and it is larger."""
    if not b or os.path.dirname(a) != os.path.dirname(b):
        return False
    na, nb = os.path.basename(a), os.path.basename(b)
    da, db_ = re.findall(r"\d+", na), re.findall(r"\d+", nb)
    return (bool(da) and re.sub(r"\d+", "#", na) == re.sub(r"\d+", "#", nb)
            and da[:-1] == db_[:-1] and int(db_[-1]) > int(da[-1]))


def _decode(raw, encodings, stats):
    if raw.isascii():
        return raw.decode("ascii"), "ascii"
    for enc in encodings:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    stats["decode_errors"] += 1
    return raw.decode(encodings[-1], errors="replace"), encodings[-1] + "?"


def _regexp(pattern, value):
    """SQLite REGEXP, for keyword filtering."""
    try:
        return value is not None and re.search(pattern, value) is not None
    except re.error:
        return False


# columns of table lines (during parsing each record is a list like this)
COLS = ("ts", "ts_inh", "module", "sub", "func", "src", "srcline", "file_id", "lineno", "event", "level",
        "alarm", "alarm_state", "color", "text")
TS, TS_INH, MODULE, SUB, FUNC, SRC, SRCLINE, FILE_ID, LINENO, EVENT, LEVEL, ALARM, ALARM_STATE, COLOR, TEXT = range(15)
INSERT = "INSERT INTO raw VALUES(%s)" % ",".join("?" * len(COLS))


class Index:
    def __init__(self, folder, cfg, cache_dir=None, progress=None):
        self.folder = os.path.abspath(folder)
        self.cfg = cfg
        self.progress = progress or (lambda msg: None)
        cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), ".logview_cache")
        os.makedirs(cache_dir, exist_ok=True)
        key = hashlib.sha1(self.folder.encode("utf-8")).hexdigest()[:16]
        self.db_path = os.path.join(cache_dir, key + ".sqlite")
        self.files = list_files(self.folder, cfg.file_glob)
        self.db = None
        self.alarm_table = {}
        self._filter_cache = {}

    # ==== Build the index ===========================================================

    def _fingerprint(self):
        h = hashlib.sha1(SCHEMA_VERSION.encode())
        h.update(self.cfg.digest.encode())
        for p in self.files:
            st = os.stat(p)
            h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8"))
        return h.hexdigest()

    def open(self, force=False):
        """Open the index: use the cache if still valid (returns False), otherwise re-parse (returns True)."""
        fp = self._fingerprint()
        if not force and os.path.exists(self.db_path):
            db = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                old = db.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
            except sqlite3.DatabaseError:
                old = None
            if old and old[0] == fp:
                self.db = db
                self._after_open()
                return False
            db.close()
        self._build(fp)
        self._after_open()
        return True

    def _build(self, fingerprint):
        t_start = time.time()
        tmp = self.db_path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        db = sqlite3.connect(tmp, check_same_thread=False)
        db.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
            CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, size INT, lines INT,
                               records INT, excluded INT, encodings TEXT, decode_errors INT, no_time_at_start INT,
                               format TEXT);
            CREATE TABLE raw(ts INT, ts_inh INT, module TEXT, sub TEXT, func TEXT, src TEXT, srcline INT,
                             file_id INT, lineno INT, event TEXT, level TEXT,
                             alarm TEXT, alarm_state TEXT, color TEXT, text TEXT);
            CREATE TABLE shapes(kind TEXT, format TEXT, module TEXT, shape TEXT, n INT, example TEXT);
            CREATE TABLE rxstats(format TEXT, key TEXT, base INT, hit INT, nvalues INT, top TEXT);
            CREATE TABLE alarm_names(code TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE alarm_levels(file_id INT, lineno INT, level TEXT);
        """)
        # statistics for the regex check; format in the keys is "[parser]" or "[parser.<name>]"
        self._shapes = defaultdict(Counter)        # (kind, format, module) -> shape counts of missed lines
        self._examples = {}                        # (kind, format, module, shape) -> first raw line seen
        self._rx = defaultdict(lambda: [0, 0])     # (format, regex key) -> [lines checked, lines matched]
        self._vals = defaultdict(Counter)          # (format, regex key) -> counts of captured values
        self._names = defaultdict(Counter)         # alarm code -> alarm name counts (alarm_name)
        self._name_refs = set()                    # alarm lines where only the name was captured (group name of alarm_code)
        self._levels = []                          # (file_id, line number, level): level of that alarm record
        self._level_wait = {}                      # module -> [file_id, line number, records seen]: alarms still waiting for a level
        carry = None  # rolling files: the last record of the previous file, carried into the next file
        for fid, path in enumerate(self.files):
            self.progress(f"Parsing {fid + 1}/{len(self.files)}: {os.path.relpath(path, self.folder)}")
            nxt = self.files[fid + 1] if fid + 1 < len(self.files) else None
            carry = self._parse_file(db, fid, path, carry, keep_last=same_series(path, nxt))

        self.progress("Sorting and merging the timeline...")
        db.executescript("""
            CREATE TABLE lines AS SELECT * FROM raw ORDER BY ts, file_id, lineno;
            DROP TABLE raw;
            CREATE INDEX ix_ts ON lines(ts);
            CREATE INDEX ix_mod ON lines(module, sub, ts);
            CREATE INDEX ix_ev ON lines(level, ts);
        """)
        self._resolve_names(db)
        db.executemany("INSERT INTO alarm_levels VALUES(?,?,?)", self._levels)
        db.executemany("INSERT INTO shapes VALUES(?,?,?,?,?,?)",
                       [(kind, fmt, mod, s, n, self._examples.get((kind, fmt, mod, s)))
                        for (kind, fmt, mod), cnt in self._shapes.items() for s, n in cnt.most_common(30)])
        db.executemany("INSERT INTO rxstats VALUES(?,?,?,?,?,?)",
                       [(fmt, key, base, hit, len(self._vals[(fmt, key)]),
                         json.dumps(self._vals[(fmt, key)].most_common(15), ensure_ascii=False))
                        for (fmt, key), (base, hit) in self._rx.items()])
        self.progress("Pairing event intervals...")
        self._build_intervals(db)
        db.execute("INSERT INTO meta VALUES('fingerprint', ?)", (fingerprint,))
        db.execute("INSERT INTO meta VALUES('built_sec', ?)", (f"{time.time() - t_start:.1f}",))
        db.commit()
        db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.replace(tmp, self.db_path)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)

    def _parse_file(self, db, fid, path, carry=None, keep_last=False):
        """Parse one file into the raw / files tables.
        keep_last: the next file continues this one; hold back the last record and hand it, with the current time, to the next file."""
        cfg = self.cfg
        rel = os.path.relpath(path, self.folder)
        fmt = cfg.format_for(rel)       # which regex set this file uses
        F = fmt.label
        rx, vals = self._rx, self._vals

        def miss(kind, module, line):  # record a missed line: shape count + one raw example per shape
            sh = shape(line)
            cnt = self._shapes[(kind, F, module)]
            if sh not in cnt and len(self._examples) < 20000:
                self._examples[(kind, F, module, sh)] = line.strip()[:300]
            cnt[sh] += 1

        def hit(key, value=None):
            r = rx[(F, key)]
            r[1] += 1
            if value is not None:
                vals[(F, key)][value] += 1

        stats, encs_seen = Counter(), Counter()
        batch = []
        cur, last_ts, held, skipping = carry if carry else (None, None, [], False)  # held: records seen before any time
        lineno = nrec = no_time_at_start = excluded = 0  # skipping: inside a record of an [exclude]d module
        excl_mod, excl_sub = bool(cfg.exclude_modules), bool(cfg.exclude_subs)

        def flush(rec):
            if rec is None:
                return
            if rec[TS] is None:
                held.append(rec)
                return
            batch.append(tuple(rec))

        with open(path, "rb") as f:
            for raw in f:
                lineno += 1
                line, enc = _decode(raw.rstrip(b"\r\n"), fmt.encodings, stats)
                encs_seen[enc] += 1
                if not line.strip():
                    continue
                indented = line[:1].isspace()
                module, msub = fmt.module.get_both(line)  # msub: sub-module captured by the module regex itself (group sub)
                if module is None and skipping:  # continuation line of an excluded record
                    excluded += 1
                    continue
                if module is not None and (excl_mod or excl_sub):  # [exclude]: drop the record without parsing it further
                    sub = fmt.sub_of(line, msub, None if fmt.has_sub else fmt.function.get(line)) if excl_sub else None
                    if cfg.excluded(module, sub):
                        flush(cur)
                        cur, skipping = None, True
                        excluded += 1
                        continue
                skipping = False
                if module is None:
                    if not indented:
                        rx[(F, "module")][0] += 1
                        miss("no_module", "", line)
                    if cur is not None:  # continuation line
                        cur[TEXT] += "\n" + line
                        continue
                    module = "?"
                else:
                    rx[(F, "module")][0] += 1
                    hit("module", module)
                checked = module != "?"  # lines without a module at the start of a file are not counted in the regex check
                if checked:
                    for k in ("time", "function", "source", "event", "alarm", "alarm_reset", "alarm_done",
                              "alarm_name", "alarm_level", "abnormal"):
                        rx[(F, k)][0] += 1

                ts, ts_inh = None, 0
                for m in fmt.time.rx.finditer(line):  # the first plausible time on the line (skips e.g. a 10-digit serial number before it)
                    tv = fmt.time.pick(m)
                    v = int(tv) if tv and tv.isdigit() else 0
                    v = v // 1000 if v > 10_000_000_000 else v
                    if TS_MIN <= v < TS_MAX:
                        ts = v
                        break
                if ts is not None:
                    last_ts = ts
                    if checked:
                        hit("time")
                else:
                    ts, ts_inh = last_ts, 1
                    if checked:
                        miss("no_time", module, line)

                func = fmt.function.get(line)
                if checked:
                    hit("function", func) if func else miss("no_func", module, line)
                sub = fmt.sub_of(line, msub, func)  # the second level under the module; without a sub-module it is the function
                if fmt.has_sub:
                    if checked:
                        rx[(F, "submodule")][0] += 1
                        hit("submodule", sub) if sub else miss("no_sub", module, line)
                sm = fmt.source.rx.search(line)
                src = fmt.source.pick(sm) if sm else None
                srcline = sm.group(fmt.source.line_idx) if src and fmt.source.line_idx else None
                srcline = int(srcline) if srcline and srcline.isdigit() else None
                if checked:
                    hit("source", src) if src else miss("no_src", module, line)
                event = fmt.event.get(line)
                if event and checked:
                    hit("event", event)
                level = "event" if event else None
                color = alarm = alarm_state = None
                what = None
                if fmt.abnormal.rx is not None:
                    m = fmt.abnormal.rx.search(line)
                    if m:
                        what = fmt.abnormal.pick(m) or m.group(0)
                        if checked:
                            hit("abnormal", what)
                if fmt.alarm_name.rx is not None:  # code and name on the same line: add to the table
                    m = fmt.alarm_name.rx.search(line)
                    nm, cd = (fmt.alarm_name.pick(m), fmt.alarm_name.pick_alt(m)) if m else (None, None)
                    if nm and cd:
                        self._names[cd.upper()][nm] += 1
                        if checked:
                            hit("alarm_name", f"{cd.upper()}={nm}")
                lv = fmt.alarm_level.get(line)
                if lv and checked:
                    hit("alarm_level", lv)
                # Alarm: first check for reset done (alarm_done), then RESET (HandleAlarm RESET counts too), and finally raised.
                # alarm_state: SET raised, RESET the RESET action, DONE reset done. The code may be missing
                is_done = fmt.alarm_done.rx is not None and fmt.alarm_done.rx.search(line) is not None
                is_reset = is_done or fmt.alarm_reset.rx.search(line) is not None
                if is_reset or fmt.alarm.rx.search(line):
                    m = fmt.alarm_code.rx.search(line)
                    code = fmt.alarm_code.pick(m) if m else None
                    code = code.upper() if code else None
                    if m and not code:  # only the name: keep the name for now, map it back to the code once everything is read
                        code = fmt.alarm_code.pick_alt(m)
                        if code:
                            self._name_refs.add(code)
                    if checked:
                        hit("alarm_done" if is_done else "alarm_reset" if is_reset else "alarm", code)
                        if not is_reset:  # the alarm code hit rate counts only alarm-raised lines
                            rx[(F, "alarm_code")][0] += 1
                            hit("alarm_code", code) if code else miss("no_code", module, line)
                    if code or not is_reset:  # a RESET without a code is just a normal line
                        alarm, alarm_state = code, ("DONE" if is_done else "RESET" if is_reset else "SET")
                        level = "clear" if is_reset else "alarm"
                        label = ("Reset done " if is_done else "RESET " if cfg.use_done else "Cleared ") if is_reset else "Alarm "
                        event = event or (label + (code or "?"))
                if what and not alarm_state:  # abnormal: if the same line is also an alarm or clear, it counts as the alarm
                    level, event = "abn", event or "Abnormal " + what
                for r in cfg.rules:  # [rule.*]: the first match wins
                    if (not r.module or r.module == module) and r.pattern.search(line):
                        color = r.color
                        event = event or r.label
                        if r.alarm:
                            level = "alarm"
                        elif level is None:
                            level = "event"
                        break

                # Alarm level: on the alarm line, use it directly; otherwise give it to the most recent alarm of the same module that has no level yet (within LEVEL_WINDOW records)
                wait = self._level_wait
                if alarm_state:
                    wait.pop(module, None)
                    if lv:
                        self._levels.append((fid, lineno, lv))
                    else:
                        wait[module] = [fid, lineno, 0]
                elif module in wait:
                    w = wait[module]
                    if lv:
                        self._levels.append((w[0], w[1], lv))
                        del wait[module]
                    else:
                        w[2] += 1
                        if w[2] >= LEVEL_WINDOW:
                            del wait[module]

                flush(cur)
                cur = [ts, ts_inh, module, sub, func, src, srcline, fid, lineno, event, level,
                       alarm, alarm_state, color, line]
                nrec += 1
                if ts is None:
                    no_time_at_start += 1
                elif held:  # records at the start without a time take the first time that follows
                    for p in held:
                        p[TS] = ts
                        batch.append(tuple(p))
                    held = []
                if len(batch) >= 5000:
                    db.executemany(INSERT, batch)
                    batch.clear()

        if keep_last:
            out = (cur, last_ts, held, skipping)
        else:
            flush(cur)
            for p in held:  # no time all the way to the end
                p[TS] = 0
                batch.append(tuple(p))
            out = None
        db.executemany(INSERT, batch)
        db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (fid, rel, os.path.getsize(path), lineno, nrec, excluded,
                    ",".join(f"{k}:{v}" for k, v in encs_seen.most_common()),
                    stats["decode_errors"], no_time_at_start, F))
        return out

    def _resolve_names(self, db):
        """Write the code <-> name table learned by alarm_name into alarm_names;
        alarm lines where only the name was captured (group name of alarm_code) are mapped back to the code, so they pair with the lines that give the code."""
        names = {code: cnt.most_common(1)[0][0] for code, cnt in self._names.items()}
        db.executemany("INSERT INTO alarm_names VALUES(?,?)", names.items())
        by_name = Counter()
        for code, cnt in self._names.items():
            for nm, n in cnt.items():
                by_name[(nm, code)] += n
        to_code = {}
        for (nm, code), _ in sorted(by_name.items(), key=lambda x: x[1]):  # if one name maps to several codes, use the most common
            to_code[nm] = code
        refs = [(nm, to_code[nm]) for nm in self._name_refs if nm in to_code]
        if refs:
            db.execute("CREATE TEMP TABLE name_map(name TEXT PRIMARY KEY, code TEXT)")
            db.executemany("INSERT INTO name_map VALUES(?,?)", refs)
            db.execute("""UPDATE lines SET event = replace(event, alarm, (SELECT code FROM name_map WHERE name = lines.alarm)),
                                           alarm = (SELECT code FROM name_map WHERE name = lines.alarm)
                          WHERE level IN ('alarm', 'clear') AND alarm IN (SELECT name FROM name_map)""")
            db.execute("DROP TABLE name_map")

    def _build_intervals(self, db):
        """Pair intervals: command pairs from [pairs] (including same-name pairs like CMD_*_REQ -> CMD_*_CPL), unlisted
        XXX_START/XXX_END, and alarm to RESET. An interval belongs to the module where it starts; if start and end are in the
        same sub-module (the function when [parser] submodule is empty), sub is that one ("" for none, also drawn on its swimlane row);
        across sub-modules sub is NULL and it is drawn only on the module row."""
        pairs = self.cfg.pairs
        db.execute("""CREATE TABLE intervals(module TEXT, sub TEXT, name TEXT, kind TEXT,
                      t0 INT, t1 INT, end_event TEXT, closed INT)""")
        open_ = {}   # (module or *, name) -> (start time, start module, start function)
        out = []
        last_ts = 0

        def start(k, ts, module, func, name):
            if k in open_:  # started again before the previous one ended: the previous one counts as unfinished
                t0, m0, f0 = open_.pop(k)
                out.append((m0, f0 or "", name, "event", t0, ts, None, 0))
            open_[k] = (ts, module, func)

        def end(k, ts, func, name, kind, end_event):
            if k in open_:
                t0, m0, f0 = open_.pop(k)
                out.append((m0, (f0 or "") if f0 == func else None, name, kind, t0, ts, end_event, 1))

        use_done = self.cfg.use_done
        for ts, module, func, event, alarm, state in db.execute(
                "SELECT ts, module, sub, event, alarm, alarm_state FROM lines WHERE level IS NOT NULL ORDER BY rowid"):
            last_ts = ts
            if alarm:  # alarm raised to cleared: the thin red band at the top of the swimlanes. With alarm_done set, the RESET action does not end it
                k = (self._alarm_key(module), "ALARM " + alarm)
                if state == "SET":
                    open_.setdefault(k, (ts, module, func))
                elif state == "DONE" or not use_done:
                    end(k, ts, func, k[1], "alarm", "Reset done" if state == "DONE" else "RESET")
                continue
            if not event:
                continue
            hit = None
            for p in pairs:
                hit = p.match(event)
                if hit:
                    break
            if hit:
                is_start, name = hit
                k = ("*" if p.any_module else module, name)
                if is_start:
                    start(k, ts, module, func, name)
                else:
                    end(k, ts, func, name, "event", event)
            elif event.endswith("_START"):
                base = event[:-6]
                start((module, base), ts, module, func, base)
            elif event.endswith("_END"):
                base = event[:-4]
                end((module, base), ts, func, base, "event", event)
        for (_, name), (t0, m0, f0) in open_.items():  # never ended
            out.append((m0, f0 or "", name, "alarm" if name.startswith("ALARM ") else "event", t0, last_ts, None, 0))
        db.executemany("INSERT INTO intervals VALUES(?,?,?,?,?,?,?,?)", out)
        db.execute("CREATE INDEX ix_iv ON intervals(t0, t1)")

    def _alarm_key(self, module):
        """Alarms and clears are matched by (module, code); with [parser] reset_same_module = no, by code only."""
        return module if self.cfg.reset_same_module else "*"

    def _after_open(self):
        self.db.create_function("REGEXP", 2, _regexp, deterministic=True)
        self._filter_cache = {}
        self.total = self.db.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        self.excluded = self.db.execute("SELECT IFNULL(SUM(excluded), 0) FROM files").fetchone()[0]
        self.modules = [r[0] for r in self.db.execute("SELECT DISTINCT module FROM lines ORDER BY module")]
        r = self.db.execute("SELECT MIN(ts), MAX(ts) FROM lines WHERE ts > 0").fetchone()
        self.t_min, self.t_max = r if r[0] is not None else (0, 0)
        self.alarm_names = dict(self.db.execute("SELECT code, name FROM alarm_names"))
        self._load_alarm_table()

    def _load_alarm_table(self):
        """alarm_table.csv: the first column is the alarm code, the remaining columns are joined as its description."""
        path = self.cfg.alarm_table or os.path.join(self.folder, "alarm_table.csv")
        self.alarm_table = {}
        if not os.path.exists(path):
            return
        rows = list(csv.reader(read_lines(path)))
        if rows and "alarmindex" in [h.strip().lower() for h in rows[0]]:
            rows = rows[1:]  # has a header row
        for row in rows:
            if row and row[0].strip():
                self.alarm_table[row[0].strip().upper()] = " / ".join(x.strip() for x in row[1:] if x.strip())

    # ==== Queries (one method per web API) ======================================

    def summary(self):
        return {
            "folder": self.folder, "files": len(self.files), "total": self.total,
            "modules": self.modules, "t_min": self.t_min, "t_max": self.t_max,
            "trace_before": self.cfg.trace_before, "trace_after": self.cfg.trace_after,
            "alarm_table": self.alarm_table, "alarm_names": self.alarm_names,
            "config": self.cfg.path, "use_done": self.cfg.use_done, "warnings": self.cfg.warnings,
            "has_sub": self.cfg.has_sub, "excluded": self.excluded,
            "tree": self.tree(),
        }

    def tree(self):
        """The module -> sub-module tree on the left: {module: [[sub-module, lines], ...]}. Sub-modules in alphabetical order,
        case-insensitive with numbers by value (PUMP2 before PUMP10); names equal apart from case: uppercase first; none ("") last."""
        out = defaultdict(list)
        for mod, sub, n in self.db.execute("SELECT module, IFNULL(sub, ''), COUNT(*) FROM lines GROUP BY 1, 2"):
            out[mod].append([sub, n])
        for subs in out.values():
            subs.sort(key=lambda x: (x[0] == "", _natural(x[0]), x[0]))
        return dict(out)

    def _where(self, f):
        """Filter f (dict from the web page) -> SQL WHERE."""
        conds, args = [], []
        if "modules" in f or "subs" in f:
            # modules: whole modules; subs: [[module, sub-module], ...] only these sub-modules ("" = lines without one)
            mods, subs = f.get("modules") or [], f.get("subs") or []
            parts = []
            if mods:
                parts.append("module IN (%s)" % ",".join("?" * len(mods)))
                args += mods
            if subs:
                parts.append("module || char(1) || IFNULL(sub, '') IN (%s)" % ",".join("?" * len(subs)))
                args += [m + "\x01" + sb for m, sb in subs]
            conds.append("(" + (" OR ".join(parts) or "0") + ")")
        if f.get("t0") is not None:
            conds.append("ts >= ?")
            args.append(int(f["t0"]))
        if f.get("t1") is not None:
            conds.append("ts <= ?")
            args.append(int(f["t1"]))
        if f.get("events_only"):
            conds.append("level IS NOT NULL")
        if f.get("alarms_only"):
            conds.append("level = 'alarm'")
        if f.get("abn_only"):
            conds.append("level = 'abn'")
        if f.get("src"):
            conds.append("src LIKE ?")
            args.append("%" + f["src"] + "%")
        q = f.get("q")
        if q:
            if f.get("regex"):
                try:
                    re.compile(q)
                except re.error as e:
                    raise ValueError(f"Invalid regex: {e}") from None
                conds.append("text REGEXP ?")
            else:
                conds.append("text LIKE ? ESCAPE '\\'")
                q = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.append(q)
        return (" WHERE " + " AND ".join(conds)) if conds else "", args

    def rows(self, f):
        """(rowid array, ts array) of all lines matching the filter, cached."""
        key = repr(sorted(f.items()))
        hit = self._filter_cache.get(key)
        if hit is None:
            where, args = self._where(f)
            ids, tss = array.array("q"), array.array("q")
            for rid, ts in self.db.execute(f"SELECT rowid, ts FROM lines{where} ORDER BY rowid", args):
                ids.append(rid)
                tss.append(ts)
            if len(self._filter_cache) > 20:
                self._filter_cache.clear()
            hit = self._filter_cache[key] = (ids, tss)
        return hit

    def fetch(self, ids, cols="rowid, ts, ts_inh, module, sub, func, src, srcline, file_id, lineno, event, level, "
                               "alarm, alarm_state, color, text"):
        """Fetch records by rowid (in batches, to avoid too many SQL parameters)."""
        out = []
        ids = list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            out += self.db.execute(f"SELECT {cols} FROM lines WHERE rowid IN ({','.join('?' * len(chunk))}) "
                                   "ORDER BY rowid", chunk).fetchall()
        return out

    def count(self, f):
        return len(self.rows(f)[0])

    def page(self, f, offset, limit):
        ids, _ = self.rows(f)
        files = dict(self.db.execute("SELECT id, path FROM files"))
        keys = ("id",) + COLS
        out = []
        for r in self.fetch(ids[offset: offset + limit]):
            d = dict(zip(keys, r))
            d["file"] = files.get(d.pop("file_id"), "").replace(os.sep, "/")  # always / (Windows paths too)
            out.append(d)
        return out

    def locate(self, f, ts=None, rid=None):
        """Position of the first filtered line with ts >= the given time (or rowid >= the given id)."""
        ids, tss = self.rows(f)
        return bisect.bisect_left(ids, int(rid)) if rid is not None else bisect.bisect_left(tss, int(ts))

    def events(self, t0, t1, expand=(), limit=EVENT_LIMIT):
        """What the swimlanes need: event points, intervals, log density per module.
        expand: modules expanded in the swimlanes; their density is also counted per (module, sub-module), keyed "module\x01sub" (none is "").
        More than limit events in the range: alarms, clears and abnormal lines are all kept, plain events are thinned out evenly
        over the whole range (every k-th one), so the right side of the swimlanes doesn't go blank; truncated tells the page."""
        t0, t1 = int(t0), int(t1)
        sql = ("SELECT rowid, ts, module, sub, event, level, alarm, alarm_state, color FROM lines "
               "WHERE level IS NOT NULL AND ts BETWEEN ? AND ?")
        n = self.db.execute("SELECT COUNT(*) FROM lines WHERE level IS NOT NULL AND ts BETWEEN ? AND ?", (t0, t1)).fetchone()[0]
        k = -(-n // int(limit))  # keep every k-th plain event
        rows = self.db.execute(sql + (f" AND (level != 'event' OR rowid % {k} = 0)" if k > 1 else "") + " ORDER BY rowid",
                               (t0, t1)).fetchall()
        ivs = self.db.execute("SELECT module, sub, name, kind, t0, t1, end_event, closed FROM intervals "
                              "WHERE t1 >= ? AND t0 <= ?", (t0, t1)).fetchall()
        buckets = 200
        width = max(1, (t1 - t0 + buckets - 1) // buckets)
        counts = defaultdict(lambda: [0] * buckets)
        for mod, b, n in self.db.execute(
                "SELECT module, (ts - ?) / ?, COUNT(*) FROM lines WHERE ts BETWEEN ? AND ? GROUP BY 1, 2",
                (t0, width, t0, t1)):
            if 0 <= b < buckets:
                counts[mod][b] = n
        expand = [m for m in expand if m in self.modules]
        if expand:
            for mod, sub, b, n in self.db.execute(
                    "SELECT module, IFNULL(sub, ''), (ts - ?) / ?, COUNT(*) FROM lines "
                    f"WHERE ts BETWEEN ? AND ? AND module IN ({','.join('?' * len(expand))}) GROUP BY 1, 2, 3",
                    (t0, width, t0, t1, *expand)):
                if 0 <= b < buckets:
                    counts[mod + "\x01" + sub][b] = n
        return {
            "events": [dict(zip(("id", "ts", "module", "sub", "event", "level", "alarm", "alarm_state", "color"), r))
                       for r in rows],
            "truncated": k > 1,
            "intervals": [dict(zip(("module", "sub", "name", "kind", "t0", "t1", "end_event", "closed"), r))
                          for r in ivs],
            "density": {"t0": t0, "width": width, "counts": dict(counts)},
        }

    def alarms(self):
        """Alarm list. Each alarm with a code is paired with the first later clear of the same code (by default also the same module):
        - [parser] alarm_done not set: RESET is the clear.
        - alarm_done set: only "reset done" counts as cleared; an earlier RESET is only recorded as reset_req_ts / reset_req_id (RESET sent, not done).
        reset_ts / reset_id are the time and line of the clear; None means not cleared by the end of the log.
        If the same alarm is raised again within [parser] reset_check seconds after clearing, reraise_ts / reraise_id record that line (clearing failed).
        The same alarm raised again before it is cleared counts as the same occurrence and gets the same clear time.
        name is the alarm name (learned by [parser] alarm_name), level the alarm level ([parser] alarm_level); None if absent."""
        use_done, check = self.cfg.use_done, self.cfg.reset_check
        levels = {(f, n): v for f, n, v in self.db.execute("SELECT file_id, lineno, level FROM alarm_levels")}
        out, open_, last_done = [], {}, {}  # last_done: the alarms cleared most recently for each (module, code)
        for rid, ts, module, sub, func, alarm, event, level, state, fid, lineno in self.db.execute(
                "SELECT rowid, ts, module, sub, func, alarm, event, level, alarm_state, file_id, lineno FROM lines "
                "WHERE level IN ('alarm', 'clear') ORDER BY rowid"):
            k = (self._alarm_key(module), alarm)
            if level == "clear":
                if state == "RESET" and use_done:  # only the RESET action
                    for a in open_.get(k, ()):
                        if a["reset_req_ts"] is None:
                            a["reset_req_ts"], a["reset_req_id"] = ts, rid
                    continue
                closed = open_.pop(k, [])
                for a in closed:
                    a["reset_ts"], a["reset_id"] = ts, rid
                if closed:
                    last_done[k] = closed
                continue
            if alarm and check:
                for p in last_done.pop(k, ()):
                    if ts - p["reset_ts"] <= check:
                        p["reraise_ts"], p["reraise_id"] = ts, rid
            a = {"id": rid, "ts": ts, "module": module, "sub": sub, "func": func, "alarm": alarm, "event": event,
                 "name": self.alarm_names.get(alarm), "level": levels.get((fid, lineno)),
                 "reset_ts": None, "reset_id": None, "reset_req_ts": None, "reset_req_id": None,
                 "reraise_ts": None, "reraise_id": None}
            if alarm:
                open_.setdefault(k, []).append(a)
            out.append(a)
        return out

    def health(self):
        """Parse health check: statistics and line shapes only, no raw log text."""
        db = self.db
        built = db.execute("SELECT v FROM meta WHERE k='built_sec'").fetchone()
        return {
            "modules": [dict(zip(("module", "records", "no_time", "no_func", "no_src", "events", "alarms",
                                  "with_cont"), r)) for r in db.execute("""
                SELECT module, COUNT(*), SUM(ts_inh), SUM(func IS NULL), SUM(src IS NULL), SUM(level IS NOT NULL),
                       SUM(level='alarm'), SUM(instr(text, char(10)) > 0)
                FROM lines GROUP BY module ORDER BY module""")],
            "files": [dict(zip(("path", "size", "lines", "records", "encodings", "decode_errors",
                                "no_time_at_start", "format"), r)) for r in db.execute(
                "SELECT path, size, lines, records, encodings, decode_errors, no_time_at_start, format "
                "FROM files ORDER BY id")],
            "shapes": [dict(zip(("kind", "format", "module", "shape", "n"), r)) for r in db.execute(
                "SELECT kind, format, module, shape, n FROM shapes ORDER BY kind, format, n DESC")],
            "built_sec": built[0] if built else None,
            "config": self.cfg.path,
        }

    def regexcheck(self, examples=False):
        """Regex check: for each regex set ([parser] / [parser.<name>]), how many lines each key matched, which values it captured,
        and what missed lines look like. examples=True adds one raw line per shape (viewed only inside the company, for the company AI)."""
        stats = {(f, k): (base, hit, nv, json.loads(top)) for f, k, base, hit, nv, top in
                 self.db.execute("SELECT format, key, base, hit, nvalues, top FROM rxstats")}
        nfiles = Counter(f for (f,) in self.db.execute("SELECT format FROM files"))
        formats = []
        for fmt in self.cfg.formats:
            F = fmt.label
            rows = []
            for key, fld in fmt.fields.items():
                base, hit, nv, top = stats.get((F, key), (0, 0, 0, []))
                pattern = fld.pattern
                if key == "submodule" and fmt.sub_in_module:
                    pattern = "(group sub of the module regex)" + (", else: " + pattern if pattern else "")
                rows.append({"key": key, "pattern": pattern, "base": base, "hit": hit,
                             "nvalues": nv, "top": top, "note": "\n".join(fmt.notes.get(key, [])),
                             "default": key in fmt.defaults})
            formats.append({"format": F, "files": fmt.files, "nfiles": nfiles.get(F, 0),
                            "encodings": fmt.encodings, "fields": rows})
        cols = "kind, format, module, shape, n" + (", example" if examples else "")
        misses = [dict(zip(cols.split(", "), r)) for r in self.db.execute(
            f"SELECT {cols} FROM shapes ORDER BY format, kind, n DESC")]
        return {"formats": formats, "misses": misses, "config": self.cfg.path, "warnings": self.cfg.warnings,
                "files": [dict(zip(("path", "format"), r)) for r in
                          self.db.execute("SELECT path, format FROM files ORDER BY id")]}

    def export_text(self, f, limit=200000):
        """Export raw lines matching the current filter, each prefixed with "log file:line<TAB>" so it can be found on site
        (path relative to the log folder). Continuation lines of a record follow it, prefixed with a TAB only."""
        ids, _ = self.rows(f)
        files = dict(self.db.execute("SELECT id, path FROM files"))
        out = []
        for fid, lineno, text in self.fetch(ids[:limit], "file_id, lineno, text"):
            first, *cont = text.split("\n")
            out.append(f"{files.get(fid, '').replace(os.sep, '/')}:{lineno}\t{first}\n" + "".join(f"\t{c}\n" for c in cont))
        return "".join(out)


def read_lines(path):
    """Small files like CSV: UTF-8 or Shift_JIS."""
    for enc in ("utf-8-sig", "cp932"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return f.read().splitlines()
        except UnicodeDecodeError:
            continue
    return []
