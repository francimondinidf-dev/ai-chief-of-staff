# AI Chief of Staff

An LLM pipeline that reads my email and meeting transcripts, works out **what I owe people and what
people owe me**, and sends me one numbered brief every morning. I reply to that email in plain
English and the system updates itself. A companion Claude Code skill (`/cos`) then lets me point
at any item ("do 12") and have the work done with the full context already loaded.

I built it for my own job in growth-equity investing, where commitments get made across dozens of
email threads and calls a week and the expensive failures are the quiet ones: the follow-up nobody
sent, the document a founder promised and never delivered.

---

## What it does

```
Outlook (Inbox + Sent)  ─┐
                         ├─> ingest ─> extract ─> reconcile ─> complete ─> brief (07:30 email)
Notion call transcripts ─┘      │          │           │            │              │
                              SQLite    Claude     dedup +      "likely done?"     │
                                        (JSON)     merge        from Sent mail     │
                                                                                   v
                              reply in plain English ─> reply_parse ─> state changes + standing rules
                                                                                   │
                              /cos skill in Claude Code ─> cosctl.py ─> dossier, do the work, log it
```

**The brief** has four sections: *Do now* (overdue first), *Waiting on others*, *Likely done,
confirm?* and *Going stale, still real?*. Every item links back to the exact quote it came from.

**Replies are the UI.** `1 3 done, 7 chase Sophie Friday, 9 not mine, 4 is Qonto not Konto` is
parsed into operations: close, flip to waiting-on with a due date, dismiss, and a name alias that
applies everywhere from then on.

**`/cos`** is the other half. The brief broadcasts; the skill acts on one item. It resolves a
reference the way you would say it out loud (a brief number, a company, "the press release"),
loads the originating email or transcript, the people, related tasks and previous work, does the
job (drafting, research, routing to another skill), and writes a progress note that shows up in
the next morning's brief.

---

## Design decisions

These are the parts that took iteration, and the reasons behind them.

- **Precision over recall.** A brief with 8 items you trust beats one with 30 where half are noise,
  because by week two you stop reading it. Extraction is tuned conservative, borderline items are
  kept at low confidence, and "return nothing" is treated as the correct answer for most documents.
- **Both directions are first-class.** `waiting_on` (what others owe me) is the half a to-do list
  misses, and the half that slips.
- **Direction reversal is the worst error.** Swapping promiser and promisee moves work onto the
  wrong person. The prompt forces a "who physically performs this?" check and drops anything where
  the quote doesn't make direction unmistakable.
- **Groups are not people.** "The committee will revert" is not my task. Commitments must name one
  individual on each side.
- **Transcripts are trimmed from the middle.** Next steps are agreed in the last minutes of a call,
  so long transcripts keep 30% head and 70% tail. Head-truncation would systematically drop every
  action item.
- **Never auto-close.** Completion inference (from Sent mail) marks items *likely done* and quotes
  the evidence. Silently closing things destroys trust faster than any duplicate.
- **Stable numbering.** An item keeps its number while open, so a reply written at 6pm still hits
  the right line.
- **Corrections become rules.** "X's requests are never tasks for me" is stored and injected into
  every future extraction prompt, so the same mistake doesn't come back.
- **Notes-to-self are authoritative.** An email I send only to myself (tasks that arrived on Teams
  or in a corridor) bypasses the conservative filter entirely: every line becomes a task.
- **Ambiguity is a question, not a guess.** If "Acme" matches two live items, `cosctl` exits with
  the candidates rather than picking one. Drafting against the wrong task is the failure that would
  end trust in the tool.

## Engineering notes

- **Runs on a subscription, not metered tokens.** `cos/llm.py` shells out to `claude -p
  --output-format json`. Each headless call carries ~28k tokens of Claude Code's own system prompt,
  so emails are batched (10 per call) to amortise it; an API-key backend exists as a fallback.
- **Outlook COM quirks.** `Items.Restrict()` silently returns nothing under some locales and
  `Items.Sort()` silently no-ops under Task Scheduler, so filtering is done in Python and the loop
  never `break`s on an old item.
- **Notion AI transcripts are nested.** The text lives in `transcription` blocks several levels
  down, so a non-recursive read returns an empty string that looks exactly like "no transcript".
- **Scheduler resilience.** A run killed by a reboot or Windows Update spends its trigger and is
  never retried, so the 3-hourly ingest job sends the morning brief itself if none went out today,
  and the brief job refuses to send twice. A stale lock file from a killed run is detected by PID.
- **Silent-failure diagnosis.** `claude -p` writes some refusals (session limit, logged out) to
  stdout, not stderr; the error reporter reads both.

## Layout

```
engine/
  run.py              pipeline entry point (--preview, --ingest, --refresh, --brief, --status)
  cosctl.py           single-task CLI that the /cos skill drives
  cos/schema.sql      SQLite schema (the data model is the design)
  cos/ingest_*.py     Outlook COM + Notion API
  cos/extract.py      documents -> commitments (batched LLM calls, strict JSON)
  cos/reconcile.py    exact + semantic dedup
  cos/complete.py     completion inference from Sent mail
  cos/brief.py        sections, stable numbering, grouping, HTML email
  cos/reply_parse.py  free-text reply -> operations
  cos/tasks_api.py    reference resolution, dossier, mutations
  register_tasks.ps1  Windows Task Scheduler setup
  CLAUDE.md           architecture notes for working on it with Claude Code
skill/
  SKILL.md            the /cos Claude Code skill
```

## Running it

Requirements: Windows with Outlook desktop, Python 3.10+, [Claude Code](https://claude.com/claude-code)
logged in, and optionally a Notion integration token for call transcripts.

```powershell
cd engine
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env; notepad .env      # your email, name, Notion token
$env:PYTHONUTF8=1; .venv\Scripts\python.exe run.py --preview   # builds a brief, sends nothing
powershell -ExecutionPolicy Bypass -File .\register_tasks.ps1  # schedule it
```

Install the skill by copying `skill/` to `~/.claude/skills/cos/`.

All state (mail text, transcripts, tasks) stays in a local SQLite file. The only data leaving the
machine is the text sent to Claude for extraction.

---

Built with Python, SQLite, the Notion API, Outlook COM and Claude (via Claude Code headless mode).

Built with AI coding assistance. The product design, the extraction rules and failure modes described
above, and the review of what shipped are mine.
