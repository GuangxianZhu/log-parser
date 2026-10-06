"""Entry point of the log analyzer: starts a local web server (listens on 127.0.0.1 only, no network).

    python app.py [log folder] [--port 8765] [--config config folder] [--no-browser]

Files (the whole tool is these 3 .py files and 1 web page; new features do not add files):
    app.py        entry point + HTTP API + text for the AI export (this file)
    settings.py   reads the config my_config/logview.ini; all parsing regexes are in [parser] (written by the company AI)
    logindex.py   parses logs with those regexes, builds the SQLite index, runs queries (the logic)
    index.html    the web page (HTML + CSS + JS all in one)

API overview (called by index.html; the GET filter f is JSON, see logindex.Index._where):
    GET  /api/status                    load state and summary
    GET  /api/count?f=                  number of matching lines
    GET  /api/page?f=&offset=&limit=    one page of log lines
    GET  /api/locate?f=&ts= or &id=     position of a time / line within the matching results
    GET  /api/events?t0=&t1=&expand=    swimlane events, intervals, density (expand: JSON list of modules expanded into sub-modules)
    GET  /api/alarms                    alarm list
    GET  /api/health                    parse health check (statistics and shapes only)
    GET  /api/regexcheck?examples=1     regex check: hit rate of each regex, values captured, missed lines
    GET  /api/regexpack?examples=1      regex check bundled as text for the company AI (so it can fix [parser])
    GET  /api/ai?f=&anchor=             text for the AI export
    GET  /api/export?f=&name=           export raw lines matching the filter (download)
    POST /api/open {folder, force}      open a folder (force=true re-parses)
"""
import argparse
import json
import os
import re
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import logindex
import settings

PAGE = os.path.join(settings.TOOL_DIR, "index.html")


class App:
    """The currently open folder and its index. Parsing runs in a background thread; the page polls /api/status for progress."""

    def __init__(self, config_dir=None, cache_dir=None):
        self.config_dir = config_dir or settings.USER_DIR
        settings.ensure_user_file(self.config_dir)
        self.cache_dir = cache_dir
        self.ix = None
        self.state = "idle"   # idle / loading / ready / error
        self.msg = ""
        self.rebuilt = False
        self.lock = threading.Lock()

    def progress(self, msg):
        self.msg = msg
        print(msg, flush=True)

    def open(self, folder, force=False):
        if self.state == "loading":
            raise ValueError("Still loading, please wait")
        if not os.path.isdir(folder):
            raise ValueError(f"Folder not found: {folder}")
        self.state = "loading"
        self.progress("Scanning files...")

        def work():
            try:
                with self.lock:  # on Windows a cache file in use cannot be replaced, so close the old one first
                    if self.ix is not None:
                        self.ix.db.close()
                        self.ix = None
                cfg = settings.load(self.config_dir)
                ix = logindex.Index(folder, cfg, cache_dir=self.cache_dir, progress=self.progress)
                if not ix.files:
                    raise ValueError("No log files found in the folder (per [parser] files in logview.ini)")
                self.rebuilt = ix.open(force=force)
                with self.lock:
                    self.ix = ix
                self.state = "ready"
                self.progress("Re-parsed" if self.rebuilt else "Using cache (files unchanged)")
            except Exception as e:  # noqa: BLE001  the error is shown at the top of the page
                traceback.print_exc()
                self.state, self.msg = "error", str(e)

        threading.Thread(target=work, daemon=True).start()

    def status(self):
        out = {"state": self.state, "msg": self.msg, "rebuilt": self.rebuilt}
        if self.ix and self.state == "ready":
            out["summary"] = self.ix.summary()
        return out

    def get(self, name, qs):
        """GET /api/<name>: returns (body, Content-Type, extra response headers)."""
        one = lambda k, d=None: qs.get(k, [d])[0]  # noqa: E731
        # drop empty filter values, but keep modules/subs even when empty: an empty list there means "nothing selected"
        f = {k: v for k, v in json.loads(one("f") or "{}").items()
             if k in ("modules", "subs") or not (v is None or v is False or v in ("", []))}
        ix = self.ix
        if name == "count":
            try:
                return {"count": ix.count(f)}
            except ValueError as e:  # e.g. an invalid keyword regex: show it next to the hit count instead of 0 hits
                return {"count": 0, "error": str(e)}
        if name == "page":
            return {"rows": ix.page(f, int(one("offset", 0)), min(int(one("limit", 200)), 1000))}
        if name == "locate":
            return {"pos": ix.locate(f, ts=one("ts"), rid=one("id"))}
        if name == "events":
            return ix.events(one("t0"), one("t1"), json.loads(one("expand") or "[]"))
        if name == "alarms":
            return {"alarms": ix.alarms()}
        if name == "health":
            return ix.health()
        if name == "ai":
            anchor, tzoff = one("anchor"), one("tzoff")
            tz = (timezone(timedelta(seconds=int(tzoff))), one("tzname") or "") if tzoff else None  # the time zone picked on the page
            return build_ai_text(ix, f, int(anchor) if anchor else None, tz), "text/plain; charset=utf-8"
        if name == "regexcheck":
            return ix.regexcheck(examples=one("examples") == "1")
        if name == "regexpack":
            return build_regex_pack(ix, examples=one("examples") == "1"), "text/plain; charset=utf-8"
        if name == "export":
            fn = urllib.parse.quote(one("name", "export.txt"))
            return (ix.export_text(f), "text/plain; charset=utf-8",
                    {"Content-Disposition": "attachment; filename*=UTF-8''" + fn})
        raise LookupError(name)


# ==== Export for AI ===========================================================
# "Export for AI": compress a stretch of log into text that fits into the company AI chat, with the analysis prompt in front.
#
# Compression steps:
# 1. Drop noise lines (logview.ini [ai] noise)
# 2. Consecutive similar lines of one module (differing only in numbers) are cut to 5, noting how many were left out and the value ranges
# 3. If still over the character limit ([ai] max_chars), drop ordinary lines farthest from the key moment first; alarms and CMD_ events go last

# Analysis prompt. {data} is replaced with the log excerpt. Edit here to change the questions.
PROMPT = """You are a fault analysis assistant for production equipment. Below is an excerpt the log analyzer took from the equipment logs; the modules are already merged by time and noise lines are removed.

Answer in the format below, and for each point quote the exact time and line from the log:
1. Timeline: list 5 to 10 key events in time order (time, module, what happened).
2. Earliest anomaly: which line is the first sign of something going off normal? How many seconds before the alarm is it?
3. Likely root cause: what is the most likely cause, and what is the causal chain (A causes B causes the alarm)? Clearly mark anything uncertain as "guess".
4. Next steps: which parts or parameters should be checked on site first, and which module's logs are still needed to confirm?

Note: answer only from the log below; do not make up anything that is not in the log. If there is not enough information to decide, say what is missing.

{data}"""

_num = re.compile(r"0x[0-9A-Fa-f]+|\d+(?:\.\d+)?")
_kv = re.compile(r"\b(\w+)=(-?\d+(?:\.\d+)?)(?![\w.])")


def _n(x):
    return str(int(x)) if x == int(x) else f"{x:g}"


def _fmt(ts, date=False, tz=None):
    """tz: a tzinfo, or None for this computer's local time."""
    if not ts:
        return "--"
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S" if date else "%H:%M:%S")


def _fold(items):
    """Runs of 6+ similar consecutive lines in one module: keep the first, the last and 3 evenly spaced ones in between,
    with a note before the first. Returns the number of lines folded away."""
    folded = 0
    runs = {}  # module -> (similarity key, [lines])

    def close(run):
        nonlocal folded
        if not run or len(run[1]) < 6:
            return
        rs = run[1]
        keep = {0, len(rs) - 1} | {round(k * (len(rs) - 1) / 4) for k in (1, 2, 3)}
        for i, it in enumerate(rs):
            if i not in keep:
                it["keep"] = False
                folded += 1
        stats = {}
        for it in rs:
            for k, v in _kv.findall(it["text"]):
                if abs(float(v)) < 1e9:  # skip timestamps like t=1791074499
                    stats.setdefault(k, []).append(float(v))
        ranges = [f"{k} {_n(v[0])}→{_n(v[-1])} (min {_n(min(v))}, max {_n(max(v))})"
                  for k, v in stats.items() if len(set(v)) > 1 and len(v) == len(rs)]
        rs[0]["note"] = (f"    ... ({rs[0]['module']}: {len(rs)} similar lines, only 5 shown"
                         + ("; " + "; ".join(ranges[:4]) if ranges else "") + ")")

    for it in items:
        m = it["module"]
        if it["imp"]:
            close(runs.pop(m, None))
            continue
        key = (it["src"], _num.sub("#", it["text"].split("\n")[0]))
        run = runs.get(m)
        if run and run[0] == key:
            run[1].append(it)
        else:
            close(run)
            runs[m] = (key, [it])
    for run in runs.values():
        close(run)
    return folded


def build_ai_text(ix, f, anchor_ts=None, tz=None):
    """/api/ai: log lines under the current filter -> drop noise, fold, fit the size limit, wrap in PROMPT.
    tz: (tzinfo, name) of the time zone picked on the page, or None for this computer's local time."""
    cfg = ix.cfg
    tzi = tz[0] if tz else None
    if tz:
        off = int(tz[0].utcoffset(None).total_seconds()) // 60
        tzlabel = f"{tz[1] + ', ' if tz[1] else ''}UTC{'-' if off < 0 else '+'}{abs(off) // 60:02d}:{abs(off) % 60:02d}"
    else:
        tzlabel = "local time"
    ids, tss = ix.rows(f)
    if not ids:
        return "(No log lines match the current filter)"
    if len(ids) > 50000:
        return ("(The current range has %d lines, which is too many. First click an alarm in the alarm list to trace it, "
                "or hold Shift and select a time span in the swimlanes, then export.)" % len(ids))
    rows = ix.fetch(ids, "ts, module, src, event, level, alarm, alarm_state, text")
    rule_labels = {r.label for r in cfg.rules}

    # 1. Drop noise. Alarms and CMD_ events are "important lines": never folded, dropped last (plain events marked by [rule.*] don't count)
    items, noise = [], 0
    for ts, module, src, event, level, alarm, state, text in rows:
        if level is None and cfg.ai_noise and cfg.ai_noise.search(text):
            noise += 1
            continue
        important = level in ("alarm", "clear", "abn") or (level == "event" and event not in rule_labels)
        items.append({"ts": ts, "module": module, "src": src, "imp": important, "alarm": alarm,
                      "state": state, "text": text, "keep": True, "note": ""})

    # 2. Fold
    folded = _fold(items) if cfg.ai_fold else 0
    kept = [it for it in items if it["keep"]]

    # Key moment: the traced alarm; else the first alarm; else the middle
    if anchor_ts is None:
        first = next((it for it in kept if it["alarm"] and it["state"] == "SET"), None)
        anchor_ts = first["ts"] if first else kept[len(kept) // 2]["ts"] if kept else tss[0]
    anchor_ts = int(anchor_ts)

    # Header
    def mod_name(m):
        return cfg.module_names.get(re.sub(r"\d+$", "", m), "")
    head = ["[About this log excerpt]",
            f"- Time range: {_fmt(tss[0], True, tzi)} - {_fmt(tss[-1], False, tzi)} ({tzlabel})",
            f"- Key moment: {_fmt(anchor_ts, True, tzi)}",
            f"- Each line starts with the time ({tzlabel}), followed by the raw log line; 10-digit numbers in the raw line are UNIX time (seconds).",
            "- Indented lines are continuation lines of the line above; uppercase words in parentheses are module names (e.g. (TANK1))."]
    mods = sorted({it["module"] for it in kept})
    if mods:
        head.append("- Modules: " + "; ".join(f"{m} ({mod_name(m)})" if mod_name(m) else m for m in mods))
    alarms = {}
    for it in kept:
        if it["alarm"] and it["alarm"] not in alarms:
            alarms[it["alarm"]] = it
    if alarms:
        head.append("- Alarms present:")
        for code, it in alarms.items():
            name = ix.alarm_names.get(code, "")
            desc = ix.alarm_table.get(code, "") or ix.alarm_table.get(name.upper(), "")
            head.append(f"  - {code}{(' = ' + name) if name else ''} ({it['module']}, first at {_fmt(it['ts'], False, tzi)})"
                        f"{(': ' + desc) if desc else ''}")
    # 3. Fit the size limit
    lines = [(it["note"] + "\n" if it["note"] else "") + f"{_fmt(it['ts'], False, tzi)} {it['text']}" for it in kept]
    budget = cfg.ai_max_chars - len(PROMPT) - sum(len(h) + 1 for h in head) - 200
    size = sum(len(x) + 1 for x in lines)
    dropped = 0
    if size > budget:
        order = sorted(range(len(kept)), key=lambda i: (kept[i]["imp"], -abs(kept[i]["ts"] - anchor_ts)))
        drop = set()
        for i in order:
            if size <= budget:
                break
            drop.add(i)
            size -= len(lines[i]) + 1
        lines = [x for i, x in enumerate(lines) if i not in drop]
        dropped = len(drop)
    head.append(f"- {len(rows)} raw lines: {noise} noise lines removed, {folded} similar lines folded"
                + (f", {dropped} more lines far from the key moment left out for the size limit" if dropped else "")
                + f"; {len(lines)} lines kept below.")
    return PROMPT.replace("{data}", "\n".join(head) + "\n\n[Log]\n" + "\n".join(lines))


# ==== Regex check pack for the company AI =====================================
#
# Division of labor: the tool's logic (continuation lines, time inheritance, joining across files, alarm pairing...)
# is written by Claude in code; the regexes for "what to pick out of a log line" are written by the company AI
# in [parser] of logview.ini. "Copy for company AI" on the Regex check tab is the text below: conventions +
# current regexes + hit rates + missed examples + answer format. You paste the ini snippet the company AI returns
# into my_config/logview.ini, click "Re-parse", and look at the Regex check tab again.

# What each regex is for and which named groups it needs. Matches settings.FIELDS
FIELD_DOCS = {
    "module": ("Module tag, e.g. (TANK1). A line it does not match is treated as a continuation line of the previous one, "
               "so every new line must match", "named group (?P<module>...)"),
    "function": ("Function name, e.g. Foo() or CClass::Foo",
                 "named group (?P<func>...); for several styles use func, func2, func3..."),
    "submodule": ("Sub-module: the second level under a module (e.g. the unit, axis or thread name the line belongs to). The tree, the per-sub-module "
                  "swimlane rows and the module filter group lines by it. May be empty: then the function name is the second level. "
                  "Only set it if the logs have a sub-module that is more useful than the function name",
                  "named group (?P<sub>...); for several styles use sub, sub2, sub3..."),
    "source": ("Source file, e.g. tankctrl.cpp(123)", "named group (?P<file>...); line number in (?P<line>...), optional"),
    "time": ("UNIX time, 10 digits (seconds) or 13 digits (milliseconds). A line it does not match inherits the previous line's time",
             "named group (?P<time>...); the captured value must be digits only"),
    "event": ("Event name, usually a command starting with CMD_", "named group (?P<event>...)"),
    "alarm": ("Alarm raised: each matching line counts as one alarm. If one alarm is logged over several lines "
              "(e.g. every line contains HandleAlarm), match only the line that means \"raised\", ideally the one with the code; "
              "otherwise one alarm is counted several times", "no group needed"),
    "alarm_reset": ("Alarm cleared (RESET action): checked before alarm. A clear line without an alarm code is a normal line. "
                    "Like alarm, match only one line per clear", "no group needed"),
    "alarm_done": ("Alarm reset done: checked before alarm_reset. May be empty; if set, only a match of this counts as cleared "
                   "and RESET is just the action. Set it only when the RESET action and reset done are logged differently",
                   "no group needed"),
    "alarm_code": ("Alarm code; raised and cleared are paired by it, so it must be found on the raised, cleared and reset-done lines. "
                   "Codes differing only in case are the same code", "named group (?P<code>...); for several styles use code, code2, code3...; "
                   "for lines that only give the alarm name (e.g. = TMP1) use (?P<name>...), and the tool maps it back to the code "
                   "using the table learned by alarm_name"),
    "alarm_name": ("Alarm name: lines that give both code and name (e.g. AlarmIndex = 12345678 = TMP1), not necessarily the alarm line. "
                   "The tool learns the code/name table from all logs, so alarms logged with only a code can show their name too. May be empty",
                   "both named groups (?P<code>...) and (?P<name>...) are required"),
    "alarm_level": ("Alarm level. Can be on the alarm line or a few lines after it (it goes to the most recent alarm of the same module). May be empty",
                    "named group (?P<level>...), capturing only the level itself (e.g. 6)"),
    "abnormal": ("Abnormal: lines that are not alarms but report a bad result, e.g. CMD_xxx_CPL(NG), NORMAL->ABNORMAL. Marked red in the swimlanes. May be empty",
                 "optional named group (?P<what>...) for the displayed name; without it the matched text is shown"),
}

REGEX_GUIDE = """Please help me fix the regexes that parse logs in logview.ini, the config file of my log analyzer. The tool is written in Python and uses re.search on each line (no need to match from the start of the line).

[Conventions]
{fields}
- encodings is not a regex: it is the file encodings to try, in order, comma-separated.
- Log files written in a different style: add a section [parser.<name>] whose first line is files = file name wildcard (e.g. sys_*.log, or with a folder: sys/*.log),
  and write only the keys that differ from [parser]; the rest come from [parser].
- A higher hit rate is not always better: don't force matches on lines that shouldn't match (e.g. module must not take the (C) in temp(C) as a module, alarm_reset must not treat an ordinary counter reset as a clear,
  and alarm must not match every line of one alarm).

[Current regexes] (lines starting with ; are the notes left when each regex was written: which log style it was written for)
{current}

[Check results]{when}
{stats}

[Lines not matched]{misses_note}
{misses}

[Please answer like this]
1. First describe in a few sentences the patterns you see in the logs, and why each change is needed.
2. Then put the sections to change in one ini code block, with only the keys you change, and above each key one ; comment line saying which style it matches (with an example).
   That comment stays in the config file and comes back to you in the next pack. For example:
```ini
[parser]
; time is in square brackets at the start of the line, e.g. [1791068500]
time = \\[(?P<time>\\d{{10}})\\]
```
3. Some lines are not supposed to match (separator lines, blank lines, etc.); just say so, no need to change the regex for them."""


MISS_LIMIT = 12   # max number of missed shapes listed per kind in the pack (too long and it won't fit into the company AI)
MISS_KINDS = {"no_module": "Lines with no module found (and not an indented continuation line)",
              "no_time": "Lines with a module but no time found",
              "no_func": "Lines with a module but no function name found",
              "no_sub": "Lines with a module but no sub-module found",
              "no_src": "Lines with a module but no source file found",
              "no_code": "Alarm-raised lines with no alarm code found (or alarm matched a line it shouldn't)"}


def _pct(a, b):
    return f"{a * 100 / b:.1f}%" if b else "-"


def build_regex_pack(ix, examples=True):
    """/api/regexpack: the regex check bundled as text for the company AI. examples=True adds a raw example of each missed line shape."""
    rc = ix.regexcheck(examples=examples)
    cfg = ix.cfg
    fields = "\n".join(f"- {k}: {d}. {g}" for k, (d, g) in FIELD_DOCS.items())
    current, stats = [], []
    for f in rc["formats"]:
        current.append(f["format"])
        if f["format"] != "[parser]":
            current.append("files = " + ", ".join(f["files"]))
        current.append("encodings = " + ", ".join(f["encodings"]))
        for r in f["fields"]:  # send back the comments left when the regex was last written; mark the ones still at the default
            if r["note"]:
                current += ["; " + x for x in r["note"].split("\n")]
            elif r["default"]:
                current.append("; (built-in default, not yet adapted to the real logs)")
            current.append(f"{r['key']} = {r['pattern']}")
        current.append("")
        stats.append(f"{f['format']}: used for {f['nfiles']} files")
        for r in f["fields"]:
            line = f"- {r['key']}: checked {r['base']} lines, matched {r['hit']} ({_pct(r['hit'], r['base'])})"
            if r["top"]:
                line += f"; {r['nvalues']} distinct values, most common: " + ", ".join(f"{v}×{n}" for v, n in r["top"][:10])
            stats.append(line)
        stats.append("")
    if rc["warnings"]:  # unknown keys in logview.ini (e.g. a misspelled key name) are ignored by the tool
        stats += ["Config warnings:"] + ["- " + w for w in rc["warnings"]]
    misses, groups = [], {}
    for m in rc["misses"]:  # group by (which regex set, which key missed); list only the most common few per group
        groups.setdefault((m["format"], m["kind"]), []).append(m)
    for (fmt, kind), ms in groups.items():
        ms.sort(key=lambda m: -m["n"])
        misses.append(f"{fmt} {MISS_KINDS.get(kind, kind)}:")
        for m in ms[:MISS_LIMIT]:
            misses.append(f"- {m['module'] + ' ' if m['module'] else ''}{m['n']} lines, shape: {m['shape']}")
            if examples and m.get("example"):
                misses.append(f"    e.g.: {m['example'][:200]}")
        if len(ms) > MISS_LIMIT:
            misses.append(f"- ({len(ms) - MISS_LIMIT} more shapes, {sum(m['n'] for m in ms[MISS_LIMIT:])} lines in total, not listed)")
    note = (" Each \"shape\" (letters→a, digits→9, Japanese→あ) is listed with its count" + (" and one raw example line" if examples else "")
            + f", at most {MISS_LIMIT} per kind.")
    return REGEX_GUIDE.format(
        fields=fields, current="\n".join(current).rstrip(), stats="\n".join(stats).rstrip(),
        when=f" ({len(rc['files'])} files in the log folder, config {os.path.basename(cfg.path)})",
        misses_note=note, misses="\n".join(misses) or "(None, everything matched)")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # don't print every request to the console
            pass

        def send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            try:
                if u.path in ("/", "/index.html"):
                    with open(PAGE, "rb") as fh:
                        return self.send(200, fh.read(), "text/html; charset=utf-8")
                if u.path == "/api/status":
                    return self.send(200, app.status())
                if not u.path.startswith("/api/"):
                    return self.send(404, "not found", "text/plain")
                if app.ix is None or app.state != "ready":
                    return self.send(409, {"error": "No logs loaded yet"})
                with app.lock:
                    res = app.get(u.path[5:], urllib.parse.parse_qs(u.query))
                self.send(200, *res) if isinstance(res, tuple) else self.send(200, res)
            except LookupError:
                self.send(404, {"error": "unknown api"})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.send(500, {"error": str(e)})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                if self.path == "/api/open":
                    app.open((body.get("folder") or "").strip().strip('"'), force=bool(body.get("force")))
                    return self.send(200, app.status())
                self.send(404, {"error": "unknown api"})
            except ValueError as e:
                self.send(400, {"error": str(e)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Equipment log analyzer (local, offline)")
    ap.add_argument("folder", nargs="?", help="log folder (can also be entered on the web page)")
    ap.add_argument("--config", help="config folder (default: my_config next to the tool)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cache", help="index cache folder (default: ~/.logview_cache)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()

    app = App(config_dir=args.config, cache_dir=args.cache)
    print(f"Config file: {os.path.join(app.config_dir, settings.INI_NAME)}", flush=True)
    if args.folder:
        app.open(os.path.abspath(args.folder))
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Log analyzer running: {url}  (Ctrl+C to quit)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
