"""Merge duplicate commitments.

The same promise shows up in a call on Tuesday, an email on Wednesday and a
follow-up on Friday. If that lands as three rows you abandon the tool inside a
week, so this is load-bearing rather than cosmetic.

Two stages: a cheap exact-ish key catches obvious repeats for free, then one LLM
pass clusters the semantic duplicates ("send Tom the model" / "get the
updated model over to Tom"). Merging keeps the OLDEST row, its id is what
the brief numbered and what your replies refer to, and moves all evidence onto
it, so a merge never renumbers an item under you.
"""
import json
import re

from . import db, llm

_STOP = re.compile(r"\b(the|a|an|to|for|on|with|and|of|in|re|please|pls)\b", re.I)
_PUNCT = re.compile(r"[^\w\s]")


def _key(title: str) -> str:
    s = _PUNCT.sub(" ", (title or "").lower())
    s = _STOP.sub(" ", s)
    return " ".join(sorted(s.split()))


def merge(conn, keep_id: int, drop_id: int) -> None:
    if keep_id == drop_id:
        return
    conn.execute("UPDATE task_evidence SET task_id=? WHERE task_id=?", (keep_id, drop_id))
    conn.execute("UPDATE OR IGNORE task_entities SET task_id=? WHERE task_id=?",
                 (keep_id, drop_id))
    # keep the earliest due date and the highest confidence seen
    conn.execute(
        """UPDATE tasks SET
             due = COALESCE((SELECT MIN(d) FROM (SELECT due d FROM tasks WHERE id IN (?,?)
                             AND due IS NOT NULL)), due),
             confidence = MAX(COALESCE(confidence,0),
                              COALESCE((SELECT confidence FROM tasks WHERE id=?),0)),
             updated_at = ?
           WHERE id=?""",
        (keep_id, drop_id, drop_id, db.now(), keep_id),
    )
    conn.execute("DELETE FROM tasks WHERE id=?", (drop_id,))


def _exact_pass(conn) -> int:
    rows = conn.execute(
        "SELECT id, kind, title FROM tasks WHERE status IN ('open','likely_done') ORDER BY id"
    ).fetchall()
    first: dict[tuple, int] = {}
    merged = 0
    for r in rows:
        k = (r["kind"], _key(r["title"]))
        if not k[1]:
            continue
        if k in first:
            merge(conn, first[k], r["id"])
            merged += 1
        else:
            first[k] = r["id"]
    conn.commit()
    return merged


PROMPT = """These are open commitments extracted from one person's emails and call \
transcripts. The same real-world commitment is often phrased differently across \
sources.

Group together ONLY entries that are unmistakably the SAME real commitment, same \
deliverable, same people. Do NOT group things that are merely similar, related, or \
about the same company. Two different deliverables for one company are two commitments.

When in doubt, do not group. A wrong merge silently deletes a real task, which is \
worse than showing a near-duplicate.

ENTRIES:
{entries}

Reply with JSON ONLY:
{{"groups": [[<id>, <id>, ...], ...]}}
Include only groups of 2 or more. Omit everything else."""


def _llm_pass(conn) -> int:
    rows = conn.execute(
        """SELECT id, kind, title, promiser, promisee FROM tasks
           WHERE status IN ('open','likely_done') ORDER BY id"""
    ).fetchall()
    if len(rows) < 2:
        return 0
    entries = "\n".join(
        f'{r["id"]}. [{r["kind"]}] {r["title"]} (owed by {r["promiser"] or "?"} '
        f'to {r["promisee"] or "?"})' for r in rows
    )
    try:
        parsed = llm.ask_json(PROMPT.format(entries=entries))
    except Exception:
        return 0                      # dedup failing must never block the brief

    valid = {r["id"] for r in rows}
    merged = 0
    for group in parsed.get("groups", []) or []:
        ids = sorted({int(i) for i in group if int(i) in valid})
        if len(ids) < 2:
            continue
        keep = ids[0]                 # oldest id -> stable numbering across briefs
        for drop in ids[1:]:
            merge(conn, keep, drop)
            merged += 1
    conn.commit()
    return merged


def run(conn) -> dict:
    return {"exact_merged": _exact_pass(conn), "llm_merged": _llm_pass(conn)}
