# Chief of Staff

Reads the user's email and call transcripts, extracts commitments, and emails a
daily brief. Replies to that brief in plain English are parsed back into state
changes.

## Run

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe run.py --preview   # build a brief, print it, send nothing
PYTHONUTF8=1 .venv/Scripts/python.exe run.py --ingest    # ingest only (the 3-hourly job)
PYTHONUTF8=1 .venv/Scripts/python.exe run.py --brief     # full pipeline + email
PYTHONUTF8=1 .venv/Scripts/python.exe run.py --status    # counts
```

`--skip-ingest` reuses what's already in the database (much faster when iterating
on extraction or rendering).

## Working on ONE commitment, `cosctl.py`

The brief is a broadcast; `cosctl.py` is the conversational end, driven by the
`/cos` skill (`~/.claude/skills/cos/`). It resolves a reference the way the user would say
it out loud, hands back the full dossier, and records the outcome.

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe cosctl.py list
PYTHONUTF8=1 .venv/Scripts/python.exe cosctl.py context 63 --docs   # dossier
PYTHONUTF8=1 .venv/Scripts/python.exe cosctl.py doc 465 --full      # source email
PYTHONUTF8=1 .venv/Scripts/python.exe cosctl.py note 63 "drafted v1"
PYTHONUTF8=1 .venv/Scripts/python.exe cosctl.py done 63 -m "sent"
```

A **bare integer is a brief number**, not a task id, that is what the user reads in the
email, and the two ranges overlap. `#85` forces a task id. Free text scores against
title, detail, people and linked entities. Anything close is reported as ambiguous
(exit 2, candidates printed) rather than guessed: a wrong dossier is worse than a
question. Logic lives in `cos/tasks_api.py`, output formatting in `cosctl.py`.

Work done in a session lands in `task_log` and renders as a `progress (date): …`
line in the next brief, so the loop closes: what Claude did yesterday is visible
in the morning email instead of being silently re-asked.

## Scheduled

| Task | When | Does |
|---|---|---|
| `ChiefOfStaff Ingest` | 07:00, repeating every 3h for 24h | ingest only |
| `ChiefOfStaff Brief`  | 07:30 daily, `WakeToRun` | full pipeline + email |

Both use `StartWhenAvailable`, so a missed run fires when the machine wakes.
But a run that *starts* and is then killed (reboot, Windows Update restart, exit
`0x40010004`) has spent its trigger and is never retried. So `--refresh` catches
up: on a weekday after 07:30 with no row in `briefs` for today, it sends the
brief itself. `--brief` in turn skips if today's brief already went out, so a
late restart can't send a duplicate (`--force` resends).
**Battery flags must stay permissive** (`AllowStartIfOnBatteries`,
`DontStopIfGoingOnBatteries`). The Windows defaults are the opposite and kill
runs mid-execution with exit code `0xC000013A` and no log.

## Billing

Runs on the **Claude subscription**, not the metered API key: `cos/llm.py` shells
out to `claude -p --output-format json`. `ANTHROPIC_API_KEY` is wired only as an
automatic fallback for when OAuth lapses, so a scheduled run still produces a
brief. Set `COS_BACKEND=api` to force the metered path.

Each `claude -p` invocation carries ~28k tokens of Claude Code's own system
prompt, so **emails are batched** (10 per call, or 60k chars, whichever first)
and transcripts go one per call. `--bare` would remove that overhead but forces
API-key auth, so it can't be combined with subscription billing.

## Data sources

| Source | Where | Notes |
|---|---|---|
| Email | Outlook COM, Inbox + Sent | `Restrict()` is unreliable and `Sort()` silently no-ops under Task Scheduler, iterate and filter in Python, never `break` on an old item |
| Transcripts | Notion, 2 databases | team `Meeting Notes` (filtered to the user's calls) + optional private calls DB (all rows); IDs in `.env` |

Notion AI stores meeting text in **`transcription` blocks whose content is nested
several levels down**, not in the block's own `rich_text`. A non-recursive read
returns "" and looks exactly like "no transcript", see `notion_blocks.page_text`.

Transcripts are **never head-truncated**: commitments are agreed in the closing
minutes, so `trim_transcript` keeps 30% head + 70% tail and drops the middle.

## Design decisions worth not re-litigating

- **Stable numbering.** A task keeps its brief number while open. Renumbering
  daily would mis-apply a reply written hours after the brief was read.
- **Never silently auto-close.** Completion inference sets `likely_done` with the
  evidence quoted and asks. Silent closing kills trust faster than duplicates.
- **Precision over recall.** A missed item costs little; a false one teaches the user
  to skim the section. Borderline items sit at low confidence.
- **Both sides of a commitment.** `waiting_on` (what others owe the user) is
  first-class, and is the half a plain todo list would miss.
- **Corrections become rules.** Feedback is stored in `rules` and injected into
  every future extraction prompt, so the same mistake isn't repeated.

## Known failure modes in extraction

Found in the first review; standing rules now guard each:

1. **Direction reversal**, promiser/promisee swapped. Most damaging error.
2. **Collective identity**, obligations owed by/to "the IC", "the deal team"
   or "we" are not automatically the user's. If the user is not on the IC, add
   that as a standing rule (see README) so the extractor knows.
3. **Request ≠ commitment**, the user asking someone to do something is not that
   person committing to it.

## Layout

```
run.py              pipeline entry point
cosctl.py           single-task CLI (the /cos skill drives this)
cos/schema.sql      SQLite schema (the design lives here)
cos/db.py           access; upsert_document re-reads on version change
cos/llm.py          cc | api backends, JSON coercion, fallback
cos/ingest_outlook.py
cos/ingest_notion.py + notion_blocks.py
cos/extract.py      documents -> commitments (batched)
cos/reconcile.py    exact + semantic dedup
cos/complete.py     completion inference from Sent
cos/brief.py        sections, stable numbering, grouping
cos/reply_parse.py  free-text reply -> operations
cos/entities.py     surface form -> canonical entity
cos/tasks_api.py    reference resolution, dossier, mutations
data/cos.db         all state
```
