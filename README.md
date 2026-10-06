# Equipment Log Analyzer (Lite)

Runs locally and fully offline: your logs never leave your computer. Requires only Python 3.8+, with **no third-party packages**.

## Starting

```
python app.py D:\logs\20261004
```

- On Windows you can also double-click `start.bat`, or drag a log folder onto `start.bat`.
- The browser opens `http://127.0.0.1:8765/` automatically. You can also start without a folder and paste the path at the top of the page.
- The first open parses the logs and builds an index (about 20 s per 100 MB), cached in `~/.logview_cache`; if the files haven't changed, the next open is instant.

## How to use

| To do this | Do this |
|---|---|
| See which module did what first | Swimlanes at the top: one row per module. ▲ alarm, ○ alarm cleared, ◆ abnormal (e.g. `(NG)`); the thin red band at the top is alarm duration, vertical lines are events, colored blocks are command intervals, the gray bar at the bottom is log density |
| Zoom and pan | Scroll wheel to zoom, drag to pan, double-click to show the whole time range, Shift+drag to select a time span |
| Swimlanes per function | Click a module name to the left of the swimlanes (or ▶ in the tree on the left): one row per function in that module. The module row keeps the summary of the whole module, so a CMD_ shows up both on the module row and on its function row; command intervals that start and end in the same function are also drawn on the function row, those spanning functions only on the module row |
| Show times in another time zone | "Time zone" at the top right: the default is the browser's local time zone; pick UTC or a common zone, or "Other…" to type any IANA name (e.g. `Europe/Paris`). The log list, swimlane axis, alarm list, "From / To" input and Export for AI all use it; the choice is remembered in the browser |
| View an exact time span | Enter times in "From / To" in the filter area (to the second, both ends inclusive) and click "Find by time". You can write `2026-10-04 08:30:00`, `08:30:00` (the date is the log's first day; "To" uses the day of "From") or a 10-digit UNIX time; fill in only one end to run to the start or end of the log. If "To" is a time only and earlier than "From" (e.g. 23:50 → 00:10), it is taken as the next day |
| See which alarms repeat | "Group by code" (on by default) shows one line per alarm code: count (×37), modules, first ~ last time and how many are not cleared. Click the line to list each occurrence; untick it for the plain time-ordered list |
| Find the cause of an alarm | Click an entry in the alarm list at the bottom left: only the 600 s before to 120 s after it are shown (adjustable), and the swimlanes zoom in to match |
| Check whether an alarm was cleared | Each entry in the alarm list shows "Cleared time (duration)" or, in red, "not cleared"; click "Cleared …" to jump to the RESET line. If RESET is only an action, set `alarm_done` (regex for reset done) in `logview.ini`; ones that never completed show "RESET sent, not done". If the same alarm is raised again within 60 s after clearing, it is marked red "raised again N s later". You can tick "only not cleared" |
| See what an alarm is and how severe | With `alarm_name` set, the alarm list shows the name (e.g. TMP1), even for alarms logged with only a code, using the mapping found elsewhere in the logs; with `alarm_level` set it shows `Lv6` and you can filter by level |
| View the log at a given moment | Click an event or empty space on the swimlanes and the list below jumps there; click a line in the list and the bottom pane shows the full text (with continuation lines) and the file line number |
| Copy several log lines | In the log list, Ctrl+click (Cmd+click on Mac) adds or removes a line, Shift+click selects every line from the last clicked one. The bottom pane then shows "N lines selected" with "Copy" (raw lines, continuation lines included) and "Copy with file:line"; Ctrl+C on the list copies too. Up to 50,000 lines; use "Export as text" for more |
| Filter | Module checkboxes, keyword (regex allowed), source file, only events, only alarms, only abnormal |
| Browse by module and function | Top left is the "Modules / Functions" tree: click ▶ to expand a module and see its functions. The checkboxes decide which lines appear in the log list; you can filter by function name |
| Resize | Every gray splitter can be dragged: left pane width, pane heights, swimlane height, bottom detail height. Drag the vertical line on the right of each log column header to resize the column. "Reset layout" restores the defaults |
| Have the company AI analyze | First click an alarm trace, then "Export for AI": removes noise, folds similar lines, adds the prompt, then copy or download as .txt |
| Find a line in the original log file | The "Log file:line" column shows which file and line each record comes from. Click a record: the panel below shows the file path (relative to the log folder) and line number, with "Copy path" / "Copy file:line" buttons |
| Write a report | "Export as text": exports the raw lines matching the current filters, each starting with `file:line` and a TAB (e.g. `sys/sys_20261004_08.log:1234`); continuation lines follow, starting with a TAB |
| Check that parsing is right | "Regex check" tab: how many lines each `[parser]` regex hits, which values it extracted, and what the unmatched lines look like |
| Have the company AI fix the regexes | On the "Regex check" tab click "Copy for company AI", paste the ini snippet it returns into `logview.ini`, click "Re-parse", and come back to check the hit rate |

All configuration is in `my_config/logview.ini` (generated on first start); see **[CONFIG.md](CONFIG.md)**.

## Development notes for the company AI

This tool is deliberately small: **only 3 .py files and 1 web page; future features go into these files too, no new files.** The comment at the top of each file says what it is responsible for and who calls it.

Division of labor: **"what to extract from a log line" is regexes, written in `[parser]` of `logview.ini`, by the company AI** (it can see the real logs);
**what to do with those values (continuation lines, time inheritance, joining across files, alarm pairing, drawing) is logic, in the code.**
There is only one contract between regexes and code: each regex hands over values via named groups with fixed names; see `FIELDS` in `settings.py` and [CONFIG.md](CONFIG.md).

| File | Lines | Responsible for |
|---|---|---|
| `app.py` | ~450 | Entry point, local HTTP server, all `/api/...` endpoints (API overview at the top); the Export for AI prompt `PROMPT`; the instructions packed for the company AI by Regex check, `REGEX_GUIDE` |
| `settings.py` | ~380 | Reads `my_config/logview.ini`; default config `DEFAULT_INI`; named-group contract for regexes `FIELDS` |
| `logindex.py` | ~620 | Parses logs with the `[parser]` regexes and builds the SQLite index; queries for Timeline / alarms / module tree / Regex check |
| `index.html` | ~790 | The whole web page (HTML + CSS + JS), no external libraries |

Data flow:

```
log folder ─→ logindex.Index._parse_file()  picks a regex set per file ([parser] or [parser.<name>]),
                                              and finds in each line: module / function / time / source / CMD_ / alarm / abnormal / [rule.*]
           ─→ SQLite (~/.logview_cache): lines (sorted by time), intervals, alarm_names + alarm_levels (alarm names, levels), rxstats + shapes (Regex check)
           ─→ app.py /api/...  ─→  index.html draws charts and lists
```

Where to change things:

| To change | Where |
|---|---|
| A field is extracted wrongly | Only change the `[parser]` regex in `logview.ini` (use the "Regex check" tab to pack it for the company AI); no code change |
| Extract a new field from each line | Add an entry to `FIELDS` and `DEFAULT_INI` in `settings.py`, use it in `_parse_file()` in `logindex.py` |
| Rules for command pairing and alarm clearing | `_build_intervals()` and `alarms()` in `logindex.py` |
| Alarm name mapping, which alarm a level belongs to, abnormal | `_parse_file()` (extraction) and `_resolve_names()` (maps name-only alarms back to codes) in `logindex.py` |
| Add a config option | `settings.py`: add a comment + default line to `DEFAULT_INI`, a field to `Config`, read it in `load()` |
| Add an API endpoint | Add a query method in `logindex.py`, add an `if name == ...` in `App.get()` in `app.py` |
| Add a button or change charts on the page | `index.html`; the JS is split into sections by `// =====` |
| The questions asked in Export for AI | `PROMPT` in `app.py` |
| The instructions Regex check packs for the company AI | `REGEX_GUIDE` and `FIELD_DOCS` in `app.py` |

After changing:

1. If you changed the structure of `lines` or other tables, bump `SCHEMA_VERSION` in `logindex.py`; old caches are invalidated automatically.
2. Try it on a log folder and check the Regex check tab.

Conventions: Python standard library only; the web page makes no external connections. No logs of any kind (real or test) and no
company or product names go into this repository; log-like examples in comments and docs are made up (`(TANK1)`, `TMP1`).
The development tests and the synthetic log generator are kept outside the repository.

## Parsing approach

Relies only on a few anchors that can be identified reliably; everything else is kept as-is:

- Module tags like `(TANK1)`: a line without one is treated as a continuation line of the previous one
- Function name: `Foo()` or `CClass::Foo`, the first one in each line
- `xxx.cpp`: source file
- 10-digit UNIX seconds (13-digit milliseconds also accepted): anywhere in the line; a line without a time inherits the time from the previous line. Numbers outside 2000–2100 are not times
- `CMD_XXX`: event name, paired into intervals per `[pairs]`; `_START`/`_END` not listed there are paired automatically; `_REQ`/`_CPL` are paired by matching name only when `[pairs]` has `CMD_*_REQ -> CMD_*_CPL`
- Alarms: a line with `HandleAlarm` is an alarm raised, a line with `RESET` is a clear (`HandleAlarm RESET` and `ResetAlarm` both count), matched by the `alarmindex: XXXX` code; by default they must also be in the same module. With `alarm_done` set, RESET counts only as an action and only reset done counts as cleared; alarms raised again soon after clearing are marked red
- Alarm names and levels (optional): `alarm_name` learns the code-to-name mapping; an `alarm_level` level is assigned to the nearest alarm in the same module
- Abnormal (`abnormal`, by default `(NG)` and `NORMAL->ABNORMAL`): red diamonds on the swimlanes; you can show only abnormal
- Encoding: each line tries UTF-8 first, then Shift_JIS
- Multiple files are merged by time into one Timeline; file names are sorted numerically (log9 before log10)
- Rolling files (`log0058.log` → `log0059.log`): continuation lines cut over to the start of the next file are joined back to the previous record
