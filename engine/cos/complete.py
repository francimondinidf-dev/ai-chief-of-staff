"""Completion inference from your Sent items and your own words on calls.

Never closes anything silently. A task moves to `likely_done` with the evidence
quoted, and the brief asks you to confirm. Silent auto-closing destroys trust in
a task list faster than duplicates do, once you suspect it drops things, you
start keeping a parallel list, and the tool is dead.
"""
import json

from . import db, llm

PROMPT = """Below are open commitments, and things the user has SENT or SAID since \
each was created. Decide which commitments look already fulfilled.

Only mark done when the sent item plausibly IS the deliverable. Discussing, \
promising again, or apologising for the delay is NOT completion. "I'll send it \
tomorrow" is not sending it.

Err towards leaving things open. The user confirms each one, so a false "done" \
costs a correction and a little trust; a missed one costs nothing, it simply \
appears again tomorrow.

OPEN COMMITMENTS:
{tasks}

RECENT SENT ITEMS AND YOUR OWN REMARKS:
{sent}

Reply with JSON ONLY:
{{"done": [{{"id": <task id>, "evidence": "<verbatim quote from the sent item>",
            "doc": <DOC number it came from>, "confidence": <0.0-1.0>}}]}}
Include only genuine matches. An empty list is a fine answer."""


def _open_tasks(conn):
    return conn.execute(
        """SELECT id, title, promiser, promisee, first_seen FROM tasks
           WHERE status='open' AND kind='todo' ORDER BY id"""
    ).fetchall()


def _sent_docs(conn, limit: int = 60):
    return conn.execute(
        """SELECT id, ts, title, body FROM documents
           WHERE source IN ('outlook_sent','notion_call')
           ORDER BY ts DESC LIMIT ?""", (limit,)
    ).fetchall()


def run(conn) -> dict:
    tasks = _open_tasks(conn)
    docs = _sent_docs(conn)
    if not tasks or not docs:
        return {"flagged": 0}

    task_lines = "\n".join(
        f'{t["id"]}. {t["title"]} (owed by {t["promiser"] or "?"} '
        f'to {t["promisee"] or "?"}, opened {t["first_seen"][:10]})' for t in tasks
    )
    sent_lines = "".join(
        f'\n===== DOC {i} =====\nWHEN: {d["ts"]}\nSUBJECT: {d["title"]}\n'
        f'{(d["body"] or "")[:2500]}\n' for i, d in enumerate(docs)
    )

    try:
        parsed = llm.ask_json(PROMPT.format(tasks=task_lines, sent=sent_lines))
    except Exception:
        return {"flagged": 0, "error": "completion pass failed"}

    valid = {t["id"] for t in tasks}
    flagged = 0
    for hit in parsed.get("done", []) or []:
        try:
            tid = int(hit.get("id"))
        except (TypeError, ValueError):
            continue
        if tid not in valid or float(hit.get("confidence") or 0) < 0.5:
            continue
        doc_id = None
        try:
            doc_id = docs[int(hit["doc"])]["id"]
        except Exception:
            pass
        conn.execute(
            "UPDATE tasks SET status='likely_done', updated_at=? WHERE id=?",
            (db.now(), tid),
        )
        if doc_id:
            conn.execute(
                """INSERT INTO task_evidence(task_id, document_id, quote, role, created_at)
                   VALUES(?,?,?,'completion',?)""",
                (tid, doc_id, (hit.get("evidence") or "")[:600], db.now()),
            )
        flagged += 1
    conn.commit()
    return {"flagged": flagged}
