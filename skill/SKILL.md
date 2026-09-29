---
name: cos
description: Work on a commitment tracked by the Chief of Staff. Reference any live to-do the way you'd say it out loud, by its brief number ("do 63"), by name ("the Acme press release"), by company ("what's open on Acme?"), and this pulls the full dossier (the email or call it came from, the exact quote, the people, the company, everything else live on it), does the work, and writes the outcome back so tomorrow's brief reflects it. Also lists what's live, closes / snoozes / re-dates items, and adds ones the pipeline never saw. Triggers on "/cos …", "do 63", "work on the <X> task", "what do I owe on <company>", "mark 12 done", "what's on my plate".
---

# /cos, act on a tracked commitment

The Chief of Staff (`~/Documents/Claude Projects/chief-of-staff`) already knows what
the user owes people and what people owe them: it reads their Outlook mail and Notion call
transcripts, extracts commitments, and emails a numbered brief every morning at 07:30.

That brief is a broadcast. **You are the other half: the user names one item and you do it.**
The value is that they never re-explain context, the database already holds the email
thread, the transcript quote, the participants, the company and every sibling task.

## The one command you need

```bash
cd "$HOME/Documents/Claude Projects/chief-of-staff" && PYTHONUTF8=1 .venv/Scripts/python.exe cosctl.py <sub-command>
```

(PowerShell: `cd "$env:USERPROFILE\Documents\Claude Projects\chief-of-staff"; $env:PYTHONUTF8=1; .venv\Scripts\python.exe cosctl.py …`)

| Sub-command | Use it for |
|---|---|
| `list` | what is live right now (`--kind waiting_on`, `--snoozed`) |
| `show <ref>` | one task, headline only |
| **`context <ref> --docs`** | **the dossier, always run this before doing any work** |
| `doc <id> --full` | read a source email or call transcript in full |
| `search "<text>"` | grep the ingested corpus when the dossier isn't enough |
| `note <ref> "<what you did>"` | record progress without closing |
| `done <ref> -m "<what you did>"` | close it |
| `due <ref> YYYY-MM-DD` / `snooze <ref> YYYY-MM-DD` | re-date |
| `waiting <ref> "<who>"` | flip to "someone owes the user this" |
| `retitle <ref> "<title>"` / `dismiss <ref>` / `reopen <ref>` | fix / kill / revive |
| `add "<title>" --entity <company> --due YYYY-MM-DD` | a commitment the pipeline never saw |

Every command takes `--json`. Exit codes: **0** ok, **2** ambiguous (candidates printed),
**3** not found.

## How a reference resolves

`<ref>` is whatever the user actually said:

- **a bare number**, a *brief* number, the one the user read in the email. Falls back to a task
  id if no brief carries that number. The output always says which reading it took;
  repeat that back in one clause so a mis-resolution is caught immediately.
- **`#85`**, an explicit task id, no guessing.
- **free text**, `"the Acme press release"`, `"Acme"`, `"Sophie invite"`. Scored on
  title, detail, people and linked companies.

**Exit 2 means stop and ask.** Two live commitments mention Acme; picking one silently
and drafting against the wrong one is the failure that would make the user stop trusting this.
Print the candidates and ask which. Never resolve an ambiguity by guessing.

## The workflow

1. **Resolve and load.** `context <ref> --docs`. If the user named nothing specific, `list` first
   and ask which one, don't work on the whole list.
2. **Read the source, not the title.** The title is a one-line summary an extractor wrote;
   the real instruction is in the origin quote and the document behind it. When the task is
   at all substantive, `doc <id> --full` the origin document before drafting anything.
   Half the "what exactly does this mean?" questions are answered three lines further down
   the email thread.
3. **Check `WORK ALREADY DONE ON IT`** in the dossier. A previous session may have drafted
   this. Continue it; don't start over.
4. **Do the work.** Route to the specialised skill when one fits (see below), otherwise do
   it directly, draft the email, do the research, build the doc.
5. **Write back, always.** Even when nothing closed:
   `note 63 "drafted the PR, sent to Sophie for the product paragraph"`.
   Work that leaves no trace means tomorrow's brief asks again for something already
   handled, and the progress line now renders in the brief, so a note is visible
   the next morning.

## Routing to other skills

Most kinds of task already have a skill installed. When the dossier makes it obvious,
invoke it rather than improvising. Examples of the routing this was built with:

| Task looks like | Skill |
|---|---|
| add / track a company in the CRM | a CRM skill (e.g. Affinity) |
| file docs, decks, data rooms, reporting | a document-filing skill |
| map a target and its competitors | a market-mapping skill |
| draft an investment memo or meeting minutes | a memo / minutes skill |
| find expert calls on a company or theme | an expert-call research skill |

Say which skill you're handing to. If the task carries a `suggested skill` field, that
is a hint from the extractor, not an instruction, check it makes sense.

## Rules

**Never close something the user hasn't confirmed done.** `done` is for work you actually
completed in this session, or an explicit "mark it done". Everything else is a `note`. The
project's standing principle is that silently auto-closing kills trust faster than a
duplicate ever will, a `likely_done` item is a *question*, not a conclusion.

**Direction is the most damaging thing to get wrong.** `todo` = the user owes it, so you do the
work. `waiting_on` = someone owes *the user*, so the work is a chase: draft the follow-up to
that person, don't do their job for them. The dossier prints `X owes -> Y` explicitly.

**Know the user's seat.** Being on the deal team is not the same as being on the IC, and
"we" in a call is not automatically the user. Draft to seniors, partners or the IC in the
right register. See the Chief of Staff `CLAUDE.md` for the extraction failure modes this
guards against.

**Don't send anything the user hasn't seen.** Draft emails into the reply, or into a file, never
send from Outlook on your own initiative. Filing documents and writing to the CRM are
fine when the user asked for them (those skills carry their own confirmation rules).

**A correction is worth more than a fix.** If the user says the task itself is wrong, wrong
wording, not theirs, wrong person, fix it with `retitle` / `dismiss` / `waiting`, and if they
stated a general principle ("Sophie's requests are never tasks for me"), also add it as a
standing rule so the extractor stops repeating the mistake:

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe -c "from cos import db; c=db.init(); c.execute(\"INSERT INTO rules(kind,text,created_at) VALUES('preference',?,datetime('now'))\", ['<the rule>']); c.commit()"
```

## Answering questions instead of doing work

"What's open on Acme?", "what do I owe Sophie?", "what's overdue?", answer from
`list` plus `context` on the relevant items. Answer in prose with the refs the user can reuse
(`63`, `#85`), not a dump of the CLI output.

## When the data looks stale

The brief runs at 07:30 and ingests every 3 hours; a task created in an email an hour ago
may not be in the database yet. If the user references something that isn't there, say so and
offer either `add` (record it now) or a refresh:

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe run.py --refresh   # ingest + extract, sends nothing
```

`--refresh` takes a few minutes and needs Outlook running. Don't run it speculatively.
