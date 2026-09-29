"""Extraction: documents -> commitments.

Tuned for PRECISION. A brief with 8 real items you trust beats one with 30 where
half are noise, because you stop reading the second one by week two. Borderline
items are kept at low confidence and surfaced only via `cos more`.

Emails are batched (many, small); transcripts go one per call (few, large).
Batching exists purely to amortize Claude Code's per-invocation system prompt.
"""
import json
import re

from . import config, db, entities, llm

_BULK = re.compile(
    r"unsubscribe|se d[ée]sabonner|manage your preferences|view in browser|"
    r"no-?reply@|donotreply|mailer-daemon|notifications?@|calendar\.google",
    re.I,
)

INSTRUCTIONS = """You are the extraction stage of a personal chief-of-staff system for \
{user} <{email}>, {role}.

For EACH document below, extract only GENUINE COMMITMENTS involving the user.

  todo        = the user owes a deliverable or action to someone
  waiting_on  = someone else owes a deliverable or action to the user

A commitment needs an identifiable deliverable and an owner. Extract it only if \
a competent chief of staff would put it on a list.

DO NOT extract:
  - newsletters, marketing, automated notifications, calendar noise
  - conversational filler: "let's stay in touch", "happy to help", "we should catch up",
    "I'll take a look" said in passing with no actual deliverable
  - hypotheticals, or anything conditional on a decision not yet made
  - anything the same document shows as already completed
  - commitments between other people that don't involve the user

Be conservative. Precision matters far more than recall: a missed item costs \
little, a false one costs trust in the whole list. Returning an empty list is a \
good answer and the right answer for most documents.

THE TWO ERRORS THAT MATTER MOST, check both before emitting anything:

1. DIRECTION. Re-read your own quote and ask who physically performs the action.
   If the user performs it   -> kind="todo",       promiser = the user.
   If someone else performs  -> kind="waiting_on", promiser = that named person.
   Reversing these is worse than missing the item entirely, because it moves work
   onto the wrong person's plate. If the quote does not make the direction
   unmistakable, do not extract it.

2. COLLECTIVE vs INDIVIDUAL. The user belongs to several groups (the deal team,
   the Investment Committee, the firm, "we"). An obligation owed BY or TO one of
   those groups is NOT automatically theirs. `promiser` and `promisee` must each name
   ONE person. Never write a compound like "Jane Doe / IC". If you cannot name a
   single individual on both sides, skip the commitment.

Also: a request the user MAKES is not a commitment by the recipient. "Could you
confirm X?" creates a commitment only once that person agrees.

NOTE-TO-SELF DOCUMENTS ARE THE EXCEPTION TO ALL OF THE ABOVE.
A document marked "NOTE YOU WROTE TO YOURSELF" is an email the user sent only to
themselves, that is how tasks reaching them by Teams message or spoken conversation
enter this system. They have already decided these are real by writing them down.
So for those documents ONLY:
  - extract EVERY item listed, including terse fragments ("call Pierre", "CRM
    Acme"), one commitment per item
  - kind="todo" and promiser = the user, unless they explicitly name someone else
    as the owner
  - confidence 0.9 or above; do not second-guess, do not apply the "is this really
    a commitment?" test, and never return is_noise for such a document
  - the `quote` is their own wording, however short

On CALL TRANSCRIPTS specifically: speech is full of politeness that sounds like \
commitment. Extract only what a participant would actually expect to happen. \
Pay closest attention to the final minutes, where next steps are agreed.

`quote` MUST be copied verbatim from the document. Never paraphrase it.
If a company or person name is ambiguous, list it in `ambiguous` and still \
extract the commitment.
{rules}

Reply with JSON ONLY, no prose, in exactly this shape:

{{"documents": [
  {{"doc": <the DOC number>,
    "is_noise": false,
    "commitments": [
      {{"kind": "todo" | "waiting_on",
        "title": "<imperative, <=90 chars>",
        "detail": "<optional one line>",
        "promiser": "<who owes it>",
        "promisee": "<who is owed>",
        "due": "<ISO date or empty>",
        "confidence": <0.0-1.0>,
        "quote": "<verbatim>",
        "companies": ["..."],
        "people": ["..."],
        "ambiguous": ["..."]}}
    ]}}
]}}"""


def _rules_block(conn) -> str:
    rows = conn.execute(
        "SELECT kind, text FROM rules WHERE active=1 ORDER BY id DESC LIMIT 60"
    ).fetchall()
    if not rows:
        return ""
    lines = "\n".join(f"  - [{r['kind']}] {r['text']}" for r in rows)
    return ("\n\nSTANDING RULES the user has taught you. These override your "
            "defaults:\n" + lines)


def _prefilter(doc) -> str | None:
    body = doc["body"] or ""
    if doc["direction"] == "self":
        # a note-to-self is deliberate; even three words are a real task
        return None if body.strip() else "empty_note"
    if doc["source"] == "notion_call":
        return None if len(body.strip()) >= 200 else "transcript_too_short"
    if doc["direction"] == "in" and _BULK.search(body[:4000] + (doc["title"] or "")):
        return "bulk_mail"
    if len(body.strip()) < 40:
        return "too_short"
    return None


def _render(doc, index: int) -> str:
    people = json.loads(doc["participants"] or "[]")
    who = ", ".join(f"{p.get('name')} <{p.get('email','')}>" for p in people[:25])
    if doc["direction"] == "self":
        kind = "NOTE YOU WROTE TO YOURSELF"
    else:
        kind = {"notion_call": "CALL TRANSCRIPT", "outlook_sent": "EMAIL YOU SENT",
                "outlook_inbox": "EMAIL YOU RECEIVED"}.get(doc["source"], doc["source"])
    return (f"\n===== DOC {index} =====\n"
            f"TYPE: {kind}\nWHEN: {doc['ts']}\nTITLE: {doc['title']}\n"
            f"PARTICIPANTS: {who}\n--- CONTENT ---\n{doc['body']}\n")


def _batches(docs, max_chars: int = 60_000, max_items: int | None = None):
    """Group by character budget, not a fixed count, one huge transcript must
    not ride along with nine emails and blow the context."""
    max_items = max_items or config.BATCH_SIZE
    batch, size = [], 0
    for doc in docs:
        length = len(doc["body"] or "")
        if batch and (size + length > max_chars or len(batch) >= max_items):
            yield batch
            batch, size = [], 0
        batch.append(doc)
        size += length
    if batch:
        yield batch


def extract_batch(conn, docs) -> dict[int, list[dict]]:
    """Returns {document_id: [commitment, ...]}."""
    prompt = INSTRUCTIONS.format(
        user=config.USER_NAME, email=config.USER_EMAIL, role=config.USER_ROLE, rules=_rules_block(conn)
    ) + "\n" + "".join(_render(d, i) for i, d in enumerate(docs))

    parsed = llm.ask_json(prompt, model=config.MODEL_EXTRACT)
    if isinstance(parsed, list):
        parsed = {"documents": parsed}

    out: dict[int, list[dict]] = {}
    for entry in parsed.get("documents", []):
        try:
            idx = int(entry.get("doc"))
            doc = docs[idx]
        except (TypeError, ValueError, IndexError):
            continue
        out[doc["id"]] = [] if entry.get("is_noise") else (entry.get("commitments") or [])
    return out


def store(conn, doc_id: int, items: list[dict]) -> int:
    n = 0
    for it in items:
        if not it.get("title"):
            continue
        cur = conn.execute(
            """INSERT INTO tasks(kind, title, detail, promiser, promisee, status,
                                 due, confidence, first_seen, updated_at)
               VALUES(?,?,?,?,?,'open',?,?,?,?)""",
            (it.get("kind", "todo"), str(it["title"])[:300], it.get("detail"),
             it.get("promiser"), it.get("promisee"), it.get("due") or None,
             float(it.get("confidence") or 0.5), db.now(), db.now()),
        )
        task_id = cur.lastrowid
        conn.execute(
            """INSERT INTO task_evidence(task_id, document_id, quote, role, created_at)
               VALUES(?,?,?,'origin',?)""",
            (task_id, doc_id, it.get("quote"), db.now()),
        )
        for name in (it.get("companies") or []):
            eid = entities.resolve(conn, name, "company")
            if eid:
                conn.execute("INSERT OR IGNORE INTO task_entities VALUES(?,?)", (task_id, eid))
        for name in (it.get("people") or []):
            eid = entities.resolve(conn, name, "person")
            if eid:
                conn.execute("INSERT OR IGNORE INTO task_entities VALUES(?,?)", (task_id, eid))
        n += 1
    return n


def run(conn, limit: int = 300, verbose: bool = False) -> dict:
    stats = {"docs": 0, "skipped": 0, "commitments": 0, "batches": 0, "errors": []}
    pending = []
    for doc in db.unprocessed_documents(conn, limit):
        reason = _prefilter(doc)
        if reason:
            db.mark_processed(conn, doc["id"])
            stats["skipped"] += 1
            continue
        pending.append(doc)
    conn.commit()

    for batch in _batches(pending):
        stats["batches"] += 1
        try:
            results = extract_batch(conn, batch)
        except Exception as exc:
            stats["errors"].append(f"batch of {len(batch)}: {exc!r}"[:250])
            continue
        for doc in batch:
            items = results.get(doc["id"], [])
            stats["commitments"] += store(conn, doc["id"], items)
            db.mark_processed(conn, doc["id"])
            stats["docs"] += 1
            if verbose:
                print(f"  [{len(items)}] {(doc['title'] or '')[:62]}")
        conn.commit()
    return stats
