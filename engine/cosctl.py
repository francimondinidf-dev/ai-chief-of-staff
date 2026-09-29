"""cosctl, talk to the Chief of Staff about ONE commitment.

`run.py` is the pipeline: ingest, extract, email everyone everything once a day.
This is the conversational end, driven by the `/cos` skill: resolve what you
just referred to, hand back everything known about it, and record what got done.

    python cosctl.py list                       what is live right now
    python cosctl.py show 63                    one task, one screen
    python cosctl.py context 63 --docs          the full dossier + background reading
    python cosctl.py doc 465 --full             a source email / transcript
    python cosctl.py done 63 -m "sent it"
    python cosctl.py note 63 "drafted v1, waiting on Sophie"
    python cosctl.py due 63 2026-09-08
    python cosctl.py add "Chase Sophie on the Acme memo" --entity Acme --due 2026-09-05

Output is compact text for a reader with a token budget; every command takes
--json when the caller would rather parse it. Exit codes: 0 ok, 2 ambiguous
(candidates printed), 3 not found.
"""
import argparse
import json
import sys
from datetime import date

from cos import config, db, tasks_api as T

W = 78


def _p(s=""):
    print(s)


def _short(s, n=110):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _clip(s, n):
    """Truncate but KEEP the line structure, an email quoted as one long line
    is unreadable, and reading it is the entire point of printing it."""
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + " …"


def _when(ts):
    return (ts or "")[:10]


def _src(s):
    return {"notion_call": "call", "outlook_inbox": "email in",
            "outlook_sent": "email sent"}.get(s or "", s or "?")


def _row_dict(r):
    return {k: r[k] for k in r.keys()} if r is not None else None


# ── commands ───────────────────────────────────────────────────────────
def cmd_list(conn, a):
    rows = T.active(conn, kind=a.kind, include_snoozed=a.snoozed)
    nums = T.brief_numbers(conn)
    if a.json:
        _p(json.dumps([{**_row_dict(r), "brief_num": nums.get(r["id"])} for r in rows],
                      ensure_ascii=False, indent=1))
        return 0
    _p(f"{len(rows)} active commitments   (ref = brief number, or #id)")
    _p("-" * W)
    for r in rows:
        ref = nums.get(r["id"])
        ref = f"{ref:>4}" if ref else f"{'#' + str(r['id']):>4}"
        kind = "WAIT" if r["kind"] == "waiting_on" else ("CONF" if r["status"] == "likely_done" else "TODO")
        _p(f"{ref}  {kind}  {_short(r['title'], 62):<62}{T.due_flag(r)}")
        if r["kind"] == "waiting_on" and r["promiser"]:
            _p(f"      owed by {r['promiser']}")
    _p("-" * W)
    _p("cosctl.py context <ref>   to start work on one")
    return 0


def _resolve_or_exit(conn, ref, include_closed=False):
    try:
        return T.resolve(conn, ref, include_closed=include_closed)
    except T.Ambiguous as exc:
        _p(f"AMBIGUOUS, {ref!r} could be any of these. Ask which one:")
        for r in exc.candidates:
            n = T._brief_num(conn, r["id"])
            _p(f"  {('#%d' % r['id']):<6} (brief {n if n else '-'})  {_short(r['title'], 70)}")
        sys.exit(2)
    except T.NotFound as exc:
        _p(f"NOT FOUND, {exc}")
        _p("Run: cosctl.py list")
        sys.exit(3)


def _print_task(conn, t, how, brief_num):
    _p("=" * W)
    _p(f"#{t['id']}  {t['title']}")
    _p("=" * W)
    bits = [f"kind {t['kind']}", f"status {t['status']}"]
    if brief_num:
        bits.append(f"brief #{brief_num}")
    if t["due"]:
        bits.append(f"due {t['due']}" + (" (OVERDUE)" if t["due"] < date.today().isoformat() else ""))
    if t["snooze_until"]:
        bits.append(f"snoozed until {t['snooze_until']}")
    age = T.age_days(t)
    if age is not None:
        bits.append(f"{age}d old")
    if t["confidence"] is not None:
        bits.append(f"confidence {t['confidence']:.2f}")
    _p(" · ".join(bits))
    if t["promiser"] or t["promisee"]:
        _p(f"{t['promiser'] or '?'} owes -> {t['promisee'] or '?'}")
    if t["detail"]:
        _p(f"detail: {_short(t['detail'], 400)}")
    if t["skill"]:
        _p(f"suggested skill: {t['skill']}")
    _p(f"(resolved by {how})")


def cmd_show(conn, a):
    t, how = _resolve_or_exit(conn, a.ref, a.closed)
    if a.json:
        _p(json.dumps({**_row_dict(t), "resolved_by": how}, ensure_ascii=False, indent=1))
        return 0
    _print_task(conn, t, how, T._brief_num(conn, t["id"]))
    return 0


def cmd_context(conn, a):
    t, how = _resolve_or_exit(conn, a.ref, a.closed)
    d = T.dossier(conn, t["id"], body_chars=a.chars)
    docs = T.entity_documents(conn, t["id"]) if a.docs else []

    if a.json:
        out = {"resolved_by": how, "task": _row_dict(d["task"]),
               "brief_num": d["brief_num"],
               "entities": [_row_dict(r) for r in d["entities"]],
               "evidence": [_row_dict(r) for r in d["evidence"]],
               "siblings": [_row_dict(r) for r in d["siblings"]],
               "log": [_row_dict(r) for r in d["log"]],
               "related_documents": [_row_dict(r) for r in docs]}
        _p(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    _print_task(conn, d["task"], how, d["brief_num"])

    if d["entities"]:
        _p("")
        _p("ENTITIES")
        for e in d["entities"]:
            extra = f"  domain {e['domain']}" if e["domain"] else ""
            _p(f"  {e['kind']:<8} {e['canonical_name']}{extra}")

    _p("")
    _p("WHERE IT CAME FROM")
    if not d["evidence"]:
        _p("  (no evidence rows, this task was created by hand)")
    for e in d["evidence"]:
        _p(f"  [{e['role']}] {_src(e['source'])} · {_when(e['ts'])} · doc {e['doc_id']}"
           f" · {e['body_len']} chars")
        _p(f"    {_short(e['title'], 90)}")
        if e["quote"]:
            _p(f'    -> "{_short(e["quote"], 220)}"')
        if e["participants"]:
            try:
                who = [p.get("name") or p.get("email") for p in json.loads(e["participants"])]
                if who:
                    _p(f"    with: {_short(', '.join(x for x in who if x), 110)}")
            except Exception:
                pass
        if a.chars > 0 and e["body"]:
            _p("    --- source text ---")
            for line in _short(e["body"], a.chars).splitlines():
                _p(f"    {line}")
            if e["body_len"] > a.chars:
                _p(f"    … {e['body_len'] - a.chars} more chars, cosctl.py doc {e['doc_id']} --full")

    if d["log"]:
        _p("")
        _p("WORK ALREADY DONE ON IT")
        for l in d["log"]:
            _p(f"  {_when(l['ts'])} {l['actor']}/{l['action']}: {_short(l['text'], 100)}")

    if d["siblings"]:
        _p("")
        _p("OTHER LIVE ITEMS ON THE SAME COMPANIES/PEOPLE")
        for s in d["siblings"]:
            _p(f"  #{s['id']:<4} [{s['kind']}] {_short(s['title'], 66)}")

    if docs:
        _p("")
        _p("BACKGROUND READING (recent docs mentioning these entities)")
        for x in docs:
            _p(f"  doc {x['id']:<5} {_src(x['source']):<10} {_when(x['ts'])}  "
               f"{_short(x['title'], 52):<52} {x['body_len']}c")
        _p("  cosctl.py doc <id> --full   to read one")
    return 0


def cmd_doc(conn, a):
    d = T.document(conn, a.id)
    if not d:
        _p(f"NOT FOUND, no document {a.id}")
        return 3
    if a.json:
        _p(json.dumps(_row_dict(d), ensure_ascii=False, indent=1))
        return 0
    _p("=" * W)
    _p(f"doc {d['id']}  {_src(d['source'])}  {d['ts']}")
    _p(d["title"] or "(no subject)")
    try:
        who = [p.get("name") or p.get("email") for p in json.loads(d["participants"] or "[]")]
        if who:
            _p("with: " + ", ".join(x for x in who if x))
    except Exception:
        pass
    _p("=" * W)
    body = d["body"] or ""
    _p(body if a.full else body[: a.chars])
    if not a.full and len(body) > a.chars:
        _p(f"\n… {len(body) - a.chars} more chars, rerun with --full")
    return 0


def cmd_search(conn, a):
    rows = T.search_documents(conn, a.text, limit=a.limit)
    if a.json:
        _p(json.dumps([_row_dict(r) for r in rows], ensure_ascii=False, indent=1))
        return 0
    if not rows:
        _p(f"no documents mention {a.text!r}")
        return 0
    for r in rows:
        _p(f"doc {r['id']:<5} {_src(r['source']):<10} {_when(r['ts'])}  "
           f"{_short(r['title'], 58):<58} {r['body_len']}c")
    return 0


def _mutate(conn, a, fn, verb):
    t, how = _resolve_or_exit(conn, a.ref, include_closed=True)
    fn(conn, t["id"])
    _p(f"OK, #{t['id']} {verb}: {_short(t['title'], 70)}   ({how})")
    return 0


def cmd_done(conn, a):
    return _mutate(conn, a, lambda c, i: T.set_status(c, i, "done", "user_confirmed", a.message),
                   "marked done")


def cmd_dismiss(conn, a):
    return _mutate(conn, a, lambda c, i: T.set_status(c, i, "dismissed", "dismissed", a.message),
                   "dismissed")


def cmd_reopen(conn, a):
    return _mutate(conn, a, lambda c, i: T.reopen(c, i, a.message), "reopened")


def cmd_note(conn, a):
    return _mutate(conn, a, lambda c, i: T.log(c, i, "worked", a.text), "noted")


def cmd_due(conn, a):
    _valid_date(a.date)
    return _mutate(conn, a, lambda c, i: T.set_field(c, i, "due", a.date), f"due {a.date}")


def cmd_snooze(conn, a):
    _valid_date(a.until)
    return _mutate(conn, a, lambda c, i: T.set_field(c, i, "snooze_until", a.until),
                   f"snoozed to {a.until}")


def cmd_waiting(conn, a):
    def go(c, i):
        T.set_field(c, i, "kind", "waiting_on", f"now waiting on {a.who}")
        T.set_field(c, i, "promiser", a.who)
    return _mutate(conn, a, go, f"now waiting on {a.who}")


def cmd_retitle(conn, a):
    return _mutate(conn, a, lambda c, i: T.set_field(c, i, "title", a.title), "retitled")


def cmd_add(conn, a):
    if a.due:
        _valid_date(a.due)
    tid = T.create(conn, a.title, kind=a.kind, detail=a.detail, due=a.due,
                   promiser=a.who or config.USER_NAME, entity=a.entity)
    conn.commit()
    _p(f"OK, created #{tid}: {a.title}")
    return 0


def _valid_date(s):
    try:
        date.fromisoformat(s)
    except ValueError:
        _p(f"BAD DATE {s!r}, use YYYY-MM-DD")
        sys.exit(1)


# ── wiring ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(prog="cosctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="every live commitment")
    p.add_argument("--kind", choices=["todo", "waiting_on"])
    p.add_argument("--snoozed", action="store_true", help="include snoozed items")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="one task, headline only")
    p.add_argument("ref")
    p.add_argument("--closed", action="store_true", help="search closed tasks too")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("context", help="the full dossier, start here before working")
    p.add_argument("ref")
    p.add_argument("--chars", type=int, default=1200, help="source text per evidence doc")
    p.add_argument("--docs", action="store_true", help="also list background reading")
    p.add_argument("--closed", action="store_true")
    p.set_defaults(fn=cmd_context)

    p = sub.add_parser("doc", help="read a source email or transcript")
    p.add_argument("id", type=int)
    p.add_argument("--full", action="store_true")
    p.add_argument("--chars", type=int, default=4000)
    p.set_defaults(fn=cmd_doc)

    p = sub.add_parser("search", help="grep the ingested corpus")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("done", help="close it")
    p.add_argument("ref")
    p.add_argument("-m", "--message", help="what was actually done")
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("dismiss", help="never was a real task")
    p.add_argument("ref")
    p.add_argument("-m", "--message")
    p.set_defaults(fn=cmd_dismiss)

    p = sub.add_parser("reopen", help="closed too early")
    p.add_argument("ref")
    p.add_argument("-m", "--message")
    p.set_defaults(fn=cmd_reopen)

    p = sub.add_parser("note", help="record progress without closing")
    p.add_argument("ref")
    p.add_argument("text")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("due", help="set a deadline")
    p.add_argument("ref")
    p.add_argument("date")
    p.set_defaults(fn=cmd_due)

    p = sub.add_parser("snooze", help="resurface later")
    p.add_argument("ref")
    p.add_argument("until")
    p.set_defaults(fn=cmd_snooze)

    p = sub.add_parser("waiting", help="flip to waiting-on someone")
    p.add_argument("ref")
    p.add_argument("who")
    p.set_defaults(fn=cmd_waiting)

    p = sub.add_parser("retitle", help="fix the wording")
    p.add_argument("ref")
    p.add_argument("title")
    p.set_defaults(fn=cmd_retitle)

    p = sub.add_parser("add", help="a commitment the pipeline never saw")
    p.add_argument("title")
    p.add_argument("--kind", choices=["todo", "waiting_on"], default="todo")
    p.add_argument("--detail")
    p.add_argument("--due")
    p.add_argument("--who", help="promiser, for waiting_on items")
    p.add_argument("--entity", help="company to link it to")
    p.set_defaults(fn=cmd_add)

    a = ap.parse_args()
    conn = db.init()
    try:
        return a.fn(conn, a)
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
