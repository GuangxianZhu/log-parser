"""Config: read my_config/logview.ini and turn it into a Config object.

The whole tool has a single config file, logview.ini (plus an optional alarm table, alarm_table.csv).
- On first start, DEFAULT_INI below is written as-is to my_config/logview.ini, and the user edits that file.
- Any key missing from the user's file falls back to DEFAULT_INI (so deleting a key = back to the default).
- [pairs] and [rule.*] come only from the user's file (those sections in DEFAULT_INI are examples).
- my_config/ is never overwritten when the tool is upgraded.

Division of labor: [parser] and [parser.<name>] hold only regexes (written by the company AI); the logic
that uses them lives in logindex.py. The contract between regexes and code is FIELDS: which named group
each regex uses to hand over its value.

Used by: app.py calls load() and uses ai_* and module_names for the AI export; logindex.py uses
cfg.format_for(file) to get the regexes for parsing a log.
"""
import codecs
import configparser
import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(TOOL_DIR, "my_config")
INI_NAME = "logview.ini"

# The default config, which is also the template generated for the user to edit. The comments are the manual.
DEFAULT_INI = r"""; Config for the log analyzer (the only config file). Edit it in Notepad; save as UTF-8 or Shift_JIS.
; Changes take effect after clicking "Re-parse" on the web page. Lines starting with ; are comments.
; Delete a key to use its default. Write regexes as-is; no need to double the backslashes.

[parser]
; ★ This section holds only regexes, so it is the best part to hand to the company AI: the tool's logic is in
;   the code, this section only says "what to pick out of a line".
;   "Copy for company AI" on the "Regex check" tab bundles the conventions, current regexes, hit rates and missed examples.
; Convention: a regex that extracts a value marks it with a named group (?P<name>...); the names are fixed and
;   given in each key's comment.
; File encodings to try, in order
encodings = utf-8, cp932
; Which files to pick up when scanning a folder (comma-separated)
files = *.log, *.txt
; Module tag, group module, e.g. (TANK1) (HEAT1). A line without a module tag is a continuation line of the previous one
module = \((?P<module>[A-Z]{2,}\d*)\)
; Function name, group func. If there are several styles, use func, func2, func3...; the first one found wins
function = \b(?P<func>[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)+)|\b(?P<func2>[A-Za-z_]\w*)\(\)
; Source file, group file; line number in group line (optional)
source = (?P<file>[\w\-]+\.(?:cpp|cc|c|hpp|h))(?:\((?P<line>\d+)\))?
; UNIX time, group time: 10 digits (seconds) or 13 digits (milliseconds). A line without a time inherits the previous line's
time = (?<![\w.])(?P<time>\d{10}|\d{13})(?![\w.])
; Event name, group event
event = \b(?P<event>CMD_[A-Z0-9_]+)\b
; Alarm raised: a line matching this is an alarm (no group needed, case-insensitive)
alarm = (?i)handle\s*_?alarm
; Alarm cleared (no group needed). Checked before alarm, so HandleAlarm RESET counts as cleared; a clear line without a code is just a normal line
alarm_reset = (?i)reset
; Alarm reset done (no group needed). Sometimes RESET only logs that a reset action was sent, not that the alarm was cleared.
; If set: only a match of this (with a code) counts as cleared, and RESET shows as "RESET sent, not done". If empty: RESET counts as cleared
alarm_done =
; Alarm code, group code. Raised and cleared are matched by code. For lines that only give the alarm name (e.g. = TMP1), use group name; the tool maps it back to the code using the table below
alarm_code = alarmindex:\s*(?P<code>[0-9A-Fa-f]{4})
; Alarm name (may be empty): lines that give both code and name, groups code and name, e.g. AlarmIndex = 12345678 = TMP1.
; The tool learns the code <-> name table from all logs, so alarms logged with only a code also show their name
alarm_name =
; Alarm level (may be empty), group level. It can be on the alarm line or a few lines after it; it goes to the most recent alarm of the same module
alarm_level =
; Abnormal (may be empty): matching lines get a red diamond in the swimlanes, and the list can filter "only abnormal". Group what is the displayed name; without it the matched text is shown
abnormal = \(NG\)|NORMAL->ABNORMAL
; Must a clear also come from the same module? (yes: same module and same code; no: code only)
reset_same_module = yes
; If the same alarm is raised again within this many seconds after clearing, mark it red as "raised again after clearing" (clearing failed). 0 = don't check
reset_check = 60

; ---- Logs written in a different style: one [parser.<name>] section each; files says which files it covers ----
; Only write the keys that differ from [parser] (encodings module function source time event alarm alarm_reset alarm_done alarm_code
; alarm_name alarm_level abnormal);
; the rest come from [parser]. Each file uses the first section whose files matches, or [parser] if none does.
; files can be a file name (sys_*.log) or include a folder (sys/*.log). Example:
; [parser.sys]
; files = sys_*.log
; time = \[(?P<time>\d{10})\]

[view]
; Default seconds before and after an alarm when tracing it
trace_before = 600
trace_after = 120
; Alarm table CSV (alarmindex,name,...). If empty, alarm_table.csv in the log folder is used
alarm_table =

[ai]
; Max total characters for "Export for AI" (including the prompt)
max_chars = 15000
; Lines treated as noise and not exported (regex)
noise = iomon\.cpp|heartbeat
; Fold consecutive similar lines of one module (differing only in numbers) down to 5 lines
fold = yes

[modules]
; Meaning of each module name without its digits, given to the AI as background in the export. Examples:
; TANK = processing tank
; HEAT = heater
; MGR = scheduler, dispatches lots to the tanks

[pairs]
; Command pairs: name = start command -> end command 1 | end command 2; * is a wildcard.
; Pairs only within the same module; if start and end are in different modules, add @any to the name.
; Paired commands are drawn as colored blocks in the swimlanes. XXX_START / XXX_END not listed here are paired automatically.
; If both start and end contain *, the part matched by * must be the same: the line below pairs each XXX_REQ with the
; XXX_CPL of the same name as an interval (across modules).
; A REQ that never gets its CPL is drawn as an unfinished bar; if you only care about a few commands, narrow the *, e.g. CMD_UNIT_*_REQ.
; Request@any = CMD_*_REQ -> CMD_*_CPL
LotProcess = CMD_LOT_PROCESS_START -> CMD_LOT_PROCESS_END | CMD_LOT_PROCESS_ABORT | CMD_LOT_PROCESS_HOLD
Step = CMD_STEP_START -> CMD_STEP_END
LotFull@any = CMD_LOT_DISPATCH -> CMD_LOT_COMPLETE | CMD_LOT_ABORT

; ---- Key events: one [rule.<name>] section each; matching lines are drawn as vertical lines in the swimlanes ----
; match = regex; label = displayed name; color = color; alarm = yes to treat as an alarm; module = only for this module

[rule.recipe]
match = CMD_RECIPE_LOAD
label = Recipe load
color = #3b82f6

[rule.heater_full]
match = out=100%
label = Heater at 100%
color = #f59e0b
module = HEAT1
"""

# Defaults of earlier versions. If the user's file still has one of these old defaults, use the current
# default instead (values the user changed are left alone).
OLD_DEFAULTS = {
    ("parser", "module"): {r"\[([A-Za-z]+\d*)\]", r"\(([A-Z]{2,}\d*)\)"},
    ("parser", "alarm"): {r"alarmindex:\s*([0-9A-Fa-f]{4})"},
    # Below: the styles without named groups (they still work, but are replaced by the current default)
    ("parser", "function"): {r"\b([A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)+)|\b([A-Za-z_]\w*)\(\)"},
    ("parser", "source"): {r"([\w\-]+\.(?:cpp|cc|c|hpp|h))(?:\((\d+)\))?"},
    ("parser", "time"): {r"(?<![\w.])(\d{10}|\d{13})(?![\w.])"},
    ("parser", "event"): {r"\b(CMD_[A-Z0-9_]+)\b"},
    ("parser", "alarm_code"): {r"alarmindex:\s*([0-9A-Fa-f]{4})"},
}

# Comment lines of the [parser] section in the earlier Chinese template, as note_digest() values.
# Users' logview.ini files generated from that template still contain these lines; they are template help,
# not notes the company AI wrote, so they must not be carried into the regex pack. Stored as digests so the
# old Chinese text does not have to stay in the code.
OLD_TEMPLATE_NOTES = {
    "099d8b8232ba", "0bc56db5d96a", "0d418c233e25", "10924ea28326", "21852858e927", "2290d0c5b7d1",
    "32257a9fd242", "5a64ff3b822c", "5dd5acf57329", "640d9285c8f8", "755132aa85e2", "7d489960c8ea",
    "8cd6c8a3d61c", "9c35fa87ca43", "c028b527d07d", "c0fba7cae2ee", "c8c59b121086", "cd6a3bfafe01",
    "df4309bb5edc", "e5ea0c28ff1b", "fd8608f02f3a",
}


def note_digest(note):
    """Short digest of a comment line as _notes() returns it (used for OLD_TEMPLATE_NOTES)."""
    return hashlib.sha1(note.strip().encode("utf-8")).hexdigest()[:12]


# The named group each [parser] regex must provide. None = only whether it matches, no value
FIELDS = {
    "module": "module",
    "function": "func",
    "source": "file",      # may also have group line (line number)
    "time": "time",
    "event": "event",
    "alarm": None,
    "alarm_reset": None,
    "alarm_done": None,    # may be empty (no distinction between RESET action and reset done)
    "alarm_code": "code",  # may also have group name (lines that only give the alarm name)
    "alarm_name": "name",  # also needs group code: code and name on the same line
    "alarm_level": "level",
    "abnormal": "what",    # without a group, the whole matched text is used
}
OPTIONAL = {"alarm_done", "alarm_name", "alarm_level", "abnormal"}   # may be empty; empty = not used
ALT = {"source": "line", "alarm_code": "name", "alarm_name": "code"}  # second value that can be extracted besides the main one


@dataclass
class Rule:
    name: str
    pattern: re.Pattern
    label: str
    color: str
    alarm: bool = False
    module: str = ""


@dataclass
class Pair:
    """One line of [pairs]. If the start and every end contain * (e.g. CMD_*_REQ -> CMD_*_CPL),
    the part matched by * must be the same to pair; it is appended to the name, so each command pairs separately."""
    name: str
    start: str           # wildcard: * any run of characters, ? any single character
    ends: list
    any_module: bool = False

    def __post_init__(self):
        self.same = "*" in self.start and all("*" in e for e in self.ends)
        self._rx = [(True, _wild(self.start))] + [(False, _wild(e)) for e in self.ends]

    def match(self, event):
        """Returns (True, interval name) if event is a start, (False, interval name) if it is an end, else None."""
        for is_start, rx in self._rx:
            m = rx.fullmatch(event)
            if m:
                return is_start, f"{self.name} {'/'.join(m.groups())}" if self.same else self.name
        return None


def _wild(pat):
    """Command wildcard -> regex; * becomes a group so the matched part can be taken out."""
    return re.compile("".join("(.*)" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pat))


@dataclass
class Field:
    """One [parser] regex and the rule for taking the value out of a match."""
    key: str
    pattern: str
    rx: re.Pattern
    idx: tuple = ()      # group numbers for the value, first non-empty one wins; () = only whether it matches
    line_idx: int = 0    # source only: group number of the line number, 0 = none
    alt: tuple = ()      # group numbers of the second value (ALT): the name for alarm_code, the code for alarm_name

    def pick(self, m, idx=None):
        """Match -> value (None if nothing was captured)."""
        for i in self.idx if idx is None else idx:
            v = m.group(i)
            if v:
                return v
        return None

    def pick_alt(self, m):
        return self.pick(m, self.alt)

    def get(self, line):
        if self.rx is None:
            return None
        m = self.rx.search(line)
        return self.pick(m) if m else None


@dataclass
class Format:
    """One log style: [parser] (name "") or [parser.<name>]."""
    name: str
    files: list          # which files it covers (wildcards)
    encodings: list
    fields: dict         # key (module, function...) -> Field
    notes: dict = field(default_factory=dict)     # key -> ; comments above the regex (not the template's own), included in the pack for the company AI
    defaults: set = field(default_factory=set)    # keys still at the default (not adapted to the real logs)

    def __getattr__(self, key):  # fmt.module, fmt.time ...
        try:
            return self.__dict__["fields"][key]
        except KeyError:
            raise AttributeError(key) from None

    @property
    def label(self):
        return f"[parser.{self.name}]" if self.name else "[parser]"


@dataclass
class Config:
    file_glob: list                   # which files to scan (files of [parser] and every [parser.<name>] combined)
    formats: list                     # [parser.<name>] in file order, [parser] last
    reset_same_module: bool = True
    reset_check: int = 60             # raised again within this many seconds after clearing = clearing failed; 0 = don't check
    trace_before: int = 600
    trace_after: int = 120
    alarm_table: str = ""
    ai_max_chars: int = 15000
    ai_noise: re.Pattern = None
    ai_fold: bool = True
    module_names: dict = field(default_factory=dict)
    pairs: list = field(default_factory=list)
    rules: list = field(default_factory=list)
    warnings: list = field(default_factory=list)  # keys in the user's file that the tool does not know (typos), shown on the page
    path: str = ""       # path of the user's config file
    digest: str = ""     # hash of the config content; when it changes the cache is invalid

    @property
    def default(self):
        return self.formats[-1]

    @property
    def use_done(self):
        """alarm_done is set: only "reset done" counts as cleared; RESET is just the action."""
        return any(f.alarm_done.rx is not None for f in self.formats)

    def format_for(self, relpath):
        """Which log style this file (path relative to the log folder) uses."""
        rel = relpath.replace("\\", "/").lower()
        name = rel.rsplit("/", 1)[-1]
        for f in self.formats[:-1]:
            if any(fnmatch.fnmatch(name, g.lower()) or fnmatch.fnmatch(rel, g.lower()) for g in f.files):
                return f
        return self.default


def _split(v, sep=","):
    return [x.strip() for x in v.split(sep) if x.strip()]


def _yes(v):
    return v.strip().lower() in ("yes", "true", "1", "on")


def read_text(path):
    """The config and the alarm table may be UTF-8 or Shift_JIS."""
    for enc in ("utf-8-sig", "cp932"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot read {path} (save it as UTF-8 or Shift_JIS)")


def _encodings(v, where):
    """encodings = utf-8, cp932: every name must be one Python knows; empty = the default."""
    encs = _split(v) or ["utf-8", "cp932"]
    for e in encs:
        try:
            codecs.lookup(e)
        except LookupError:
            raise ValueError(f"{where}: unknown encoding \"{e}\" (e.g. utf-8, cp932, shift_jis)") from None
    return encs


def _int(v, where, default):
    """Whole-number setting; empty = the default."""
    v = v.strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{where} must be a whole number (seconds/characters), not \"{v}\"") from None


def _compile(pattern, where):
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex in {where}: {pattern} ({e})") from None


def _field(key, pattern, where):
    """Compile one [parser] regex and find the groups to take values from, following FIELDS.
    With named groups: take name, name2, name3...; without named groups (old style): the first non-empty
    parenthesized group, or the whole match if there are no groups."""
    if key in OPTIONAL and not pattern.strip():  # optional key; empty = not used
        return Field(key, "", None)
    rx = _compile(pattern, where)
    want = FIELDS[key]
    if want is None:
        return Field(key, pattern, rx)

    def groups(name):
        return tuple(sorted(i for n, i in rx.groupindex.items() if re.fullmatch(re.escape(name) + r"\d*", n)))
    if rx.groupindex or key == "alarm_name":
        idx, alt = groups(want), groups(ALT.get(key, "-"))
        if key == "alarm_name" and not (idx and alt):
            raise ValueError(f"{where} needs named groups (?P<code>...) and (?P<name>...): {pattern}")
        if not idx and not (key == "alarm_code" and alt):
            raise ValueError(f"{where} needs a named group (?P<{want}>...): {pattern}")
        return Field(key, pattern, rx, idx, rx.groupindex.get("line", 0) if key == "source" else 0, alt)
    if key == "source":  # old style: group 1 is the file name, group 2 the line number
        return Field(key, pattern, rx, (1 if rx.groups else 0,), 2 if rx.groups >= 2 else 0)
    return Field(key, pattern, rx, tuple(range(1, rx.groups + 1)) or (0,))


def _notes(text):
    """The ; comments directly above each key in [parser...] sections. When the company AI writes a regex it
    puts a line above it saying which log style it matches; that line is sent back in the next pack, so the AI
    knows what the regex was written for last time. Returns {(section, key): [comment lines]}."""
    out, sec, buf = {}, "", []
    for raw in text.splitlines():
        t = raw.strip()
        if t.startswith("[") and t.endswith("]"):
            sec, buf = t[1:-1].strip(), []
        elif t.startswith((";", "#")):
            if t.lstrip(";#").strip():
                buf.append(t.lstrip(";#").strip())
        elif "=" in t and sec.startswith("parser"):
            if buf:
                out[(sec, t.split("=", 1)[0].strip())] = buf
            buf = []
        else:
            buf = []
    return out


def _parser(text, name):
    cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    cp.optionxform = str  # names are case-sensitive (non-ASCII names, command names)
    try:
        cp.read_string(text)
    except configparser.Error as e:
        raise ValueError(f"{name} has a format error: {e}") from None
    return cp


def ensure_user_file(user_dir=None):
    """On first run, create my_config/logview.ini; return its path."""
    user_dir = user_dir or USER_DIR
    os.makedirs(user_dir, exist_ok=True)
    path = os.path.join(user_dir, INI_NAME)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_from_old_version(user_dir) or DEFAULT_INI)
    return path


def _from_old_version(user_dir):
    """Earlier versions used several files (rules.ini, pairs.ini, ...). If they exist, move the command pairs
    and key events over; otherwise return None. Parsing rules use the new defaults (the format changed).
    The old files are not deleted, just no longer read."""
    rules, pairs = os.path.join(user_dir, "rules.ini"), os.path.join(user_dir, "pairs.ini")
    if not (os.path.isfile(rules) or os.path.isfile(pairs)):
        return None
    out = [DEFAULT_INI[:DEFAULT_INI.index("[pairs]")].rstrip("\n"), "",
           "; ---- The following was moved over from the old pairs.ini / rules.ini ----", ""]
    if os.path.isfile(pairs):
        cp = _parser(read_text(pairs), pairs)
        if cp.has_section("pairs"):
            out += ["[pairs]"] + [f"{k} = {v}" for k, v in cp.items("pairs")] + [""]
    if os.path.isfile(rules):
        cp = _parser(read_text(rules), rules)
        for s in cp.sections():
            if s.startswith("rule."):
                out += [f"[{s}]"] + [f"{k} = {v}" for k, v in cp.items(s)] + [""]
    return "\n".join(out)


def load(user_dir=None):
    path = ensure_user_file(user_dir)
    text = read_text(path)
    user = _parser(text, path)
    base = _parser(DEFAULT_INI, "DEFAULT_INI")
    notes, base_notes = _notes(text), _notes(DEFAULT_INI)

    def get(sec, key):
        """User's file first; if missing (or still an old default), use the current default."""
        if user.has_option(sec, key):
            v = user.get(sec, key).strip()
            if v not in OLD_DEFAULTS.get((sec, key), ()):
                return v
        return base.get(sec, key, fallback="").strip()

    def fmt(name, sec):
        """[parser] or [parser.<name>]: keys missing from the latter come from [parser]."""
        val = (lambda k: get("parser", k)) if not name else (
            lambda k: user.get(sec, k).strip() if user.has_option(sec, k) else get("parser", k))
        fields = {k: _field(k, val(k), f"[{sec}] {k}") for k in FIELDS}
        files = _split(val("files")) if name else _split(get("parser", "files"))
        if name and not user.has_option(sec, "files"):
            raise ValueError(f"[{sec}] needs files (which files it covers)")
        note = {}
        for k in FIELDS:  # keys not written in [parser.<name>] come from [parser], and so do their comments
            n = notes.get((sec, k)) if user.has_option(sec, k) else notes.get(("parser", k))
            # drop the template's own help, from the current template and from the old Chinese one
            n = [x for x in n or () if x not in base_notes.get(("parser", k), ())
                 and note_digest(x) not in OLD_TEMPLATE_NOTES]
            if n:
                note[k] = n
        return Format(name=name, files=files, encodings=_encodings(val("encodings"), f"[{sec}] encodings"), fields=fields, notes=note,
                      defaults={k for k, f in fields.items() if f.pattern and f.pattern == base.get("parser", k).strip()})

    formats = [fmt(s[7:], s) for s in user.sections() if s.startswith("parser.")] + [fmt("", "parser")]
    noise = get("ai", "noise")
    c = Config(
        file_glob=list(dict.fromkeys(g for f in formats for g in f.files)),
        formats=formats,
        reset_same_module=_yes(get("parser", "reset_same_module")),
        reset_check=_int(get("parser", "reset_check"), "[parser] reset_check", 0),
        trace_before=_int(get("view", "trace_before"), "[view] trace_before", 600),
        trace_after=_int(get("view", "trace_after"), "[view] trace_after", 120),
        alarm_table=get("view", "alarm_table").strip('"'),
        ai_max_chars=_int(get("ai", "max_chars"), "[ai] max_chars", 15000),
        ai_noise=_compile(noise, "[ai] noise") if noise else None,
        ai_fold=_yes(get("ai", "fold")),
        module_names=dict((user if user.has_section("modules") else base)["modules"].items()),
        path=path,
        digest=hashlib.sha1(text.encode("utf-8")).hexdigest(),
    )

    # keys the tool doesn't know are ignored, so a typo (alarm_codes =, Module =) would silently fall back to the default: list them
    known = {"parser": set(FIELDS) | {"encodings", "files", "reset_same_module", "reset_check"},
             "view": {"trace_before", "trace_after", "alarm_table"}, "ai": {"max_chars", "noise", "fold"}}
    known["parser."] = known["parser"] - {"reset_same_module", "reset_check"}
    for s in user.sections():
        keys = known.get("parser." if s.startswith("parser.") else s)
        if keys is not None:
            c.warnings += [f"[{s}] {k} is not a known key, ignored (typo?)" for k in user[s] if k not in keys]

    if user.has_section("pairs"):
        for name, spec in user["pairs"].items():
            if "->" not in spec:
                raise ValueError(f"[pairs] \"{name}\" is missing ->: {spec}")
            start, ends = spec.split("->", 1)
            any_mod = name.endswith("@any")
            c.pairs.append(Pair(name=name[:-4] if any_mod else name, start=start.strip(),
                                ends=_split(ends, "|"), any_module=any_mod))

    for s in user.sections():
        if s.startswith("rule."):
            r = user[s]
            c.rules.append(Rule(
                name=s[5:], pattern=_compile(r.get("match", ""), f"[{s}] match"),
                label=r.get("label", s[5:]), color=r.get("color", "#888888"),
                alarm=_yes(r.get("alarm", "")), module=r.get("module", "").strip()))
    return c
