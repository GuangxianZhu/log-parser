# Configuration Guide

The tool has only one config file, **`my_config/logview.ini`**, plus an optional alarm table, **`alarm_table.csv`**.

- `my_config/logview.ini` is generated on first start (its content is `DEFAULT_INI` from `settings.py`); the comments in the file are the documentation.
- Edit it in Notepad; saving as UTF-8 or Shift_JIS both work. Changes take effect only after you click **"Re-parse"** on the page.
- When upgrading the tool (extracting a new zip over it), `my_config/` is not overwritten.
- Delete an entry to use its default. Exceptions are `[pairs]` and `[rule.*]`: only what you write in your file is used.
- If something is wrong, the top of the page tells you which section and which entry. Write regexes as-is; no need to double backslashes.

## Sections in logview.ini

### [parser] How to parse a log line (written by the company AI)

This section contains **only regexes**; all the tool's logic is in the code. The company AI can see the real logs, so it is best placed to write the regexes:

1. Open the logs, switch to the **"Regex check"** tab, and look at each regex's hit rate (low ones are red) and the unmatched lines.
2. Click **"Copy for company AI"**: the writing conventions, the current regexes, hit rates and unmatched lines (optionally with raw examples) are packed into one block of text to paste to the company AI.
3. The company AI returns an ini snippet; paste it into `my_config/logview.ini` (replacing entries with the same name), click **"Re-parse"**, and go back to the check tab to see the hit rates.
   **Paste the `;` note line above each regex together with it**: it records which approach the regex was written for. Next time you pack, the note goes to the company AI along with the regex,
   so it knows how it was written last time; entries not yet changed are marked "; (built-in default, not yet adapted to the real logs)". The Regex check tab also shows this note under each regex.

**Convention: a regex that extracts values hands them over via named groups `(?P<name>...)`, and the names are fixed.**

| Entry | Default match | Named group | Notes |
|---|---|---|---|
| `encodings` | `utf-8, cp932` | (not a regex) | Encodings tried in order (cp932 is Shift_JIS) |
| `files` | `*.log, *.txt` | (not a regex) | Which files in the log folder to read |
| `module` | `(TANK1)`: 2+ uppercase letters plus digits in parentheses | `module`; optionally also `sub` (`sub2`, `sub3`…) | A line without a module tag counts as a continuation line of the previous one, so every new line must match. `temp(C)` and `Foo()` are not taken as modules. It may capture the sub-module in the same match, e.g. `\((?P<module>[A-Z]{2,}\d*)\)\s*\[(?P<sub>\w+)\]` for `(TANK1) [PUMP]`: the sub-module is then tied to the module tag's position (recommended) |
| `function` | `Foo()` or `CClass::Foo` | `func`; for several forms use `func`, `func2`, `func3`… | Function name; the first one found is used. With `submodule` empty, this is what the tree on the left lists under each module |
| `submodule` | (empty) | `sub`; for several forms use `sub`, `sub2`, `sub3`… | Optional sub-module: the second level under a module, searched separately on the line. If `module` has a `sub` group, that wins and this is only the fallback when it captured nothing. When either is set, the tree on the left, the per-sub-module swimlane rows and the module filter group lines by it instead of the function name, and the log list shows it as `TANK1/xxx`. Empty: the function name is the second level |
| `source` | `xxx.cpp(123)` | `file`, line number `line` (optional) | Source file name and line number |
| `time` | 10 or 13 digits | `time` | UNIX time; the captured value must be digits only. A line without a time inherits the time from the previous line. A value outside 2000–2100 (e.g. a serial number `0000000001`) is not taken as a time; the next match on the line is tried |
| `event` | Words starting with `CMD_` | `event` | Captured automatically as events |
| `alarm` | `handle alarm` (case-insensitive) | No group needed | A line containing it is an alarm raised. If one alarm is logged over several lines, match only the line that represents "raised" (ideally one with the code), otherwise one alarm counts as several |
| `alarm_reset` | `reset` (case-insensitive) | No group needed | A line containing it is a RESET (RESET action); checked before `alarm`. A reset line without a code is just an ordinary line |
| `alarm_done` | Empty | No group needed | Reset done. Sometimes RESET only means "a reset action was performed", not that clearing succeeded; in that case write the regex for the reset-done line, and only a match on it counts as cleared, with RESET shown as "RESET sent, not done". Empty means RESET counts as cleared |
| `alarm_code` | `alarmindex: 1A2B` | `code`; for several forms use `code`, `code2`…; for lines with only a name use `name` | Alarm code; raised and cleared are matched by it, so it must be extractable on all those lines. Case differences are ignored. For lines with only a name (e.g. `ALMID(TMP1)`), the tool maps it back to a code using what `alarm_name` learned, then pairs |
| `alarm_name` | Empty | Both `code` and `name` required | Alarm name mapping: a form with both code and name on one line, e.g. `AlarmIndex = 12345678 = TMP1`. The tool learns "code ↔ name" from all logs, so alarms logged with only a code also show the name |
| `alarm_level` | Empty | `level` | Alarm level. It can be on the alarm line or a few lines after it; it is assigned to the nearest alarm in the same module. The alarm list shows `Lv6` and you can filter by level |
| `abnormal` | `\(NG\)\|NORMAL->ABNORMAL` | Optional `what` | Abnormal: not an alarm, but a line whose result is wrong. Drawn as a red diamond on the swimlanes; the filter area has "only abnormal". `what` is the displayed name; without the group the matched text is shown |
| `reset_same_module` | `yes` | (not a regex) | `yes`: cleared only with the same module and same code; `no`: code only (use when RESET is logged by another module) |
| `reset_check` | `60` | (not a regex) | If the same alarm is raised again within this many seconds after clearing, the alarm list marks it red "raised again N s later" (counted as not cleared). `0` disables the check |

The old form without named groups (the first parenthesized group is the value) still works. If you use named groups with the wrong names, the top of the page warns you.

#### Logs written differently: [parser.<name>]

If the sys logs and the tank logs are written differently, give them their own section containing only the entries that differ; everything else falls back to `[parser]`:

```ini
[parser.sys]
files = sys_*.log
; sys logs put the module in angle brackets, e.g. <MGR3>
module = <(?P<module>[A-Z]{2,}\d*)>
```

- `files` is required: it decides which files the section covers. It can be just a file name (`sys_*.log`) or include a folder (`sys/*.log`).
- For each file, the first section in ini order whose `files` matches is used; if none matches, `[parser]` is used.
- The check tab shows each section separately, and the "By file" table has a column showing which section each file uses.

### [exclude] Modules left out

With many modules (70+), leave out the ones you never look at. Their lines, and the continuation lines under them, are dropped while parsing:
they appear nowhere (tree, swimlanes, log list, alarms, intervals, regex check, exports), and parsing is faster. Click "Re-parse" after changing.

| Entry | Default | Notes |
|---|---|---|
| `modules` | Empty | Module names, comma-separated; `*` and `?` wildcards, case-insensitive. E.g. `IOMON, SIM*, TANK9?` |
| `submodules` | Empty | `module/sub-module`, e.g. `TANK*/PUMP, */DEBUG*`. Without a `/` it applies to any module (`DEBUG*` = `*/DEBUG*`). The sub-module is what the `sub` group of `[parser] module` or `[parser] submodule` captures, or the function name when neither is set |

The status bar shows how many lines were left out; the "By file" counts on the Regex check tab count only the lines kept.

### [view] Alarm trace

| Entry | Default | Notes |
|---|---|---|
| `trace_before` / `trace_after` | 600 / 120 | How many seconds before and after to show when an alarm is clicked (can also be changed temporarily on the page) |
| `alarm_table` | Empty | Fixed path of the alarm table. When empty, `alarm_table.csv` in the log folder is used |

### [ai] Export for AI

| Entry | Default | Notes |
|---|---|---|
| `max_chars` | 15000 | Maximum number of characters exported (including the prompt) |
| `noise` | `iomon\.cpp\|heartbeat` | These lines are treated as noise and removed on export |
| `fold` | `yes` | Consecutive similar lines from the same module are folded into 5 lines, with the ranges of `name=number` values written out |

The prompt itself is `PROMPT` in `app.py`.

### [modules] Module meanings

```ini
[modules]
TANK = processing tank
HEAT = heater
```

Module names are matched with digits removed (TANK1 → TANK). Used as background information in Export for AI.

### [pairs] Command pairs

A matched pair of commands is drawn as a colored block on the swimlanes.

```ini
[pairs]
LotProcess  = CMD_LOT_PROCESS_START -> CMD_LOT_PROCESS_END | CMD_LOT_PROCESS_ABORT
LotFull@any = CMD_LOT_DISPATCH -> CMD_LOT_COMPLETE | CMD_LOT_ABORT
```

- One pair per line: `name = start command -> end command`; separate multiple end commands with `|`; `*` wildcards are allowed.
- Pairing happens only within one module; when start and end are in different modules, add `@any` after the name.
- `XXX_START` / `XXX_END` not listed here are also paired automatically. `XXX_REQ` / `XXX_CPL` are not; to pair them, write a line like the one below.
- When both start and end contain `*`, they pair only if the `*` part is the same; the block's name gets that part appended, and each command pairs separately:

  ```ini
  Request@any = CMD_*_REQ -> CMD_*_CPL
  ```

  So `CMD_UNIT_AUTO_LOCK_REQ` pairs only with `CMD_UNIT_AUTO_LOCK_CPL` and is drawn as "Request UNIT_AUTO_LOCK". The CPL is often sent back by another module, hence `@any`.
  A REQ that never gets its CPL (e.g. `CMD_RESET_ALARM_REQ`) is drawn as an unfinished long bar; if you only care about a few commands, narrow the `*`, e.g. `CMD_UNIT_*_REQ -> CMD_UNIT_*_CPL`.

### [rule.<name>] Key events

Marks a kind of line as a vertical line on the swimlanes (or treats it as an alarm).

```ini
[rule.heater_full]
match  = out=100%
label  = Heater at 100%
color  = #f59e0b
module = HEAT1
alarm  = no
```

`match` is a regex; with `alarm = yes` the line is marked red and added to the alarm list; `module` is optional.

## alarm_table.csv: alarm table

```
alarmindex,name,description
1A2B,TEMP LOW,Temperature below lower limit
```

- The first column is the code; add any columns you like after it, and they are all displayed (on hover over an alarm, in the alarm list, in Export for AI).
- By default it goes in the **log folder**; to share one table across all logs, put its path in `[view] alarm_table =`.

## I want to…

| To do this | Change |
|---|---|
| Many lines have no recognized function name, time or module | On the "Regex check" tab click "Copy for company AI" and have it fix `[parser]` |
| One kind of file is written completely differently | Add a `[parser.<name>]` section with `files` set to those files |
| Alarm clears don't match up | RESET lines logged by another module: `reset_same_module = no`; code written differently: change `alarm_code` |
| RESET is only an action; I want to see reset done | Set `alarm_done` (have the company AI write it from the reset-done line) |
| The alarm list shows only codes; I want names like TMP1 | Set `alarm_name` (from lines with code and name on the same line); for codes the logs never map, add them to alarm_table.csv |
| Filter by alarm level | Set `alarm_level` |
| Highlight results like NG | Change `abnormal` |
| A start/end isn't drawn as a block | Add a line to `[pairs]` |
| Make a kind of line stand out on the swimlanes | Add `[rule.<name>]` |
| Export for AI is too long | Lower `[ai] max_chars`, or add noise lines to `noise` |
| Restore defaults after changing config | Delete that entry; or delete the whole `logview.ini` and it is regenerated on next start |
