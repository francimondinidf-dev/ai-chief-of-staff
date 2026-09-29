"""The daily brief.

Numbering is deliberately STABLE: a task keeps the number it was first given for
as long as it stays open. You reply "3 done" hours after reading, and item 3
must still mean what it meant then, renumbering every morning would silently
mis-apply your replies.

Sections are decided by the CONTENT of a task, never by whether a previous brief
existed. "New" is a badge, not a section: on day one everything is new, and a
brief whose only populated section is "new since last time" tells you nothing.
"""
import html
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import config, db

SECTIONS = ("do", "waiting", "confirm", "stale")
_TITLES = {
    "do": "DO NOW",
    "waiting": "WAITING ON OTHERS",
    "confirm": "LIKELY DONE, CONFIRM?",
    "stale": "GOING STALE, STILL REAL?",
}
_COLOURS = {"do": "#b3261e", "waiting": "#8a6d00", "confirm": "#1b6b3a", "stale": "#666"}


def _rows(conn):
    return conn.execute(
        """SELECT t.*,
                  (SELECT quote FROM task_evidence e WHERE e.task_id=t.id
                    AND e.role='origin' ORDER BY e.id LIMIT 1)               AS origin_quote,
                  (SELECT d.title FROM task_evidence e JOIN documents d ON d.id=e.document_id
                    WHERE e.task_id=t.id AND e.role='origin' ORDER BY e.id LIMIT 1) AS origin_doc,
                  (SELECT d.source FROM task_evidence e JOIN documents d ON d.id=e.document_id
                    WHERE e.task_id=t.id AND e.role='origin' ORDER BY e.id LIMIT 1) AS origin_src,
                  (SELECT substr(d.ts,1,10) FROM task_evidence e JOIN documents d ON d.id=e.document_id
                    WHERE e.task_id=t.id AND e.role='origin' ORDER BY e.id LIMIT 1) AS origin_when,
                  (SELECT quote FROM task_evidence e WHERE e.task_id=t.id
                    AND e.role='completion' ORDER BY e.id DESC LIMIT 1)      AS done_quote,
                  (SELECT COUNT(*) FROM task_evidence e WHERE e.task_id=t.id
                    AND e.role IN ('origin','restated'))                     AS mentions,
                  -- what a /cos session did to it since the last brief, so the
                  -- brief reflects work done in Claude and not only in email
                  (SELECT text FROM task_log l WHERE l.task_id=t.id
                    AND l.action IN ('worked','note') ORDER BY l.id DESC LIMIT 1) AS last_work,
                  (SELECT substr(ts,1,10) FROM task_log l WHERE l.task_id=t.id
                    AND l.action IN ('worked','note') ORDER BY l.id DESC LIMIT 1) AS last_work_when
           FROM tasks t
           WHERE t.status IN ('open','likely_done')
             AND (t.snooze_until IS NULL OR t.snooze_until <= ?)
           ORDER BY t.id""",
        (date.today().isoformat(),),
    ).fetchall()


def _prior_numbers(conn) -> dict[int, int]:
    row = conn.execute("SELECT id FROM briefs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {}
    return {r["task_id"]: r["num"] for r in conn.execute(
        "SELECT task_id, num FROM brief_items WHERE brief_id=?", (row["id"],))}


def _age_days(row) -> int | None:
    when = row["origin_when"]
    if not when:
        return None
    try:
        return (date.today() - date.fromisoformat(when)).days
    except ValueError:
        return None


def _overdue(row) -> bool:
    return bool(row["due"]) and row["due"] < date.today().isoformat()


def _due_soon(row) -> bool:
    if not row["due"]:
        return False
    return row["due"] <= (date.today() + timedelta(days=3)).isoformat()


def _classify(row) -> str:
    """By content only. A first-run brief must be as useful as the hundredth."""
    if row["status"] == "likely_done":
        return "confirm"
    age = _age_days(row)
    gone_quiet = age is not None and age > config.STALE_AFTER_DAYS
    # a live deadline keeps something current no matter how old the promise is
    if gone_quiet and not _due_soon(row):
        return "stale"
    return "waiting" if row["kind"] == "waiting_on" else "do"


def _src_label(src: str | None) -> str:
    return {"notion_call": "call", "outlook_inbox": "email",
            "outlook_sent": "your email"}.get(src or "", "source")


def _sort_key(pair):
    num, r = pair
    return (not _overdue(r), not _due_soon(r), -(r["confidence"] or 0), num)


def build(conn) -> dict:
    rows = _rows(conn)
    prior = _prior_numbers(conn)
    used = set(prior.values())
    nxt = (max(used) + 1) if used else 1

    numbered, is_new = [], {}
    for r in rows:
        num = prior.get(r["id"])
        if num is None:
            num, nxt = nxt, nxt + 1
            is_new[r["id"]] = bool(prior)      # only meaningful once a prior brief exists
        numbered.append((num, r))

    buckets = defaultdict(list)
    for num, r in numbered:
        buckets[_classify(r)].append((num, r))
    for sec in buckets:
        buckets[sec].sort(key=_sort_key)

    counts = {s: len(buckets.get(s, [])) for s in SECTIONS}
    subject = (f"Chief of Staff, {datetime.now().strftime('%A %d %B')} · "
               f"{counts['do']} to do · {counts['waiting']} waiting · "
               f"{counts['confirm']} to confirm")

    def group_waiting(items):
        """One chatty call shouldn't produce five separate lines for one person."""
        by_person = defaultdict(list)
        for num, r in items:
            by_person[(r["promiser"] or "").strip() or "-"].append((num, r))
        grouped, singles = [], []
        for person, entries in by_person.items():
            if person != "-" and len(entries) >= config.GROUP_WAITING_AT:
                grouped.append((person, sorted(entries, key=_sort_key)))
            else:
                singles.extend(entries)
        grouped.sort(key=lambda g: -len(g[1]))
        return grouped, sorted(singles, key=_sort_key)

    # ───────────────────────── plain text ─────────────────────────
    L = [subject, "=" * 70, ""]

    def line_item(num, r, section, indent=""):
        badge = " · NEW" if is_new.get(r["id"]) else ""
        if _overdue(r):
            flag = f"  [OVERDUE {r['due']}]"
        elif r["due"]:
            flag = f"  [due {r['due']}]"
        else:
            flag = ""
        L.append(f"{indent}{num:>3}. {r['title']}{flag}{badge}")
        pad = indent + "     "
        if section == "waiting" and r["promiser"] and not indent:
            L.append(f"{pad}owed by {r['promiser']}")
        if section == "stale":
            L.append(f"{pad}no movement for {_age_days(r)} days, close it or tell me it's live")
        q = (r["done_quote"] if section == "confirm" else r["origin_quote"]) or ""
        if q:
            L.append(f'{pad}-> "{" ".join(q.split())[:150]}"')
        if r["origin_doc"]:
            L.append(f"{pad}[{_src_label(r['origin_src'])}: "
                     f"{(r['origin_doc'] or '')[:50]}, {r['origin_when'] or ''}]")
        if r["last_work"]:
            L.append(f"{pad}progress ({r['last_work_when']}): "
                     f"{' '.join(r['last_work'].split())[:120]}")
        if (r["mentions"] or 0) > 1:
            L.append(f"{pad}mentioned {r['mentions']}x across sources")
        if (r["confidence"] or 1) < 0.55:
            L.append(f"{pad}(low confidence, say so if this isn't real)")
        L.append("")

    for sec in SECTIONS:
        items = buckets.get(sec, [])
        if not items:
            continue
        L.append(f"--- {_TITLES[sec]} " + "-" * max(0, 64 - len(_TITLES[sec])))
        if sec == "waiting":
            grouped, singles = group_waiting(items)
            for person, entries in grouped:
                oldest = max((_age_days(r) or 0) for _, r in entries)
                L.append(f"  {person} owes you {len(entries)} things "
                         f"(oldest {oldest} days):")
                for num, r in entries:
                    line_item(num, r, sec, indent="  ")
            for num, r in singles:
                line_item(num, r, sec)
        else:
            for num, r in items:
                line_item(num, r, sec)
        L.append("")

    L += ["-" * 70,
          "Reply with item numbers in plain English.",
          'e.g. "1 3 done, 7 chase Sophie Friday, 9 not mine, 4 is Qonto not Konto"']
    text = "\n".join(L)

    # ───────────────────────── html ─────────────────────────
    H = ["<div style='font-family:ui-monospace,Consolas,monospace;font-size:13px;"
         "line-height:1.5;color:#1a1a1a;max-width:760px'>",
         f"<h2 style='font-family:system-ui;margin:0 0 2px'>Chief of Staff</h2>",
         f"<div style='color:#666;font-family:system-ui;margin-bottom:20px'>"
         f"{html.escape(subject.split(', ', 1)[1])}</div>"]

    def html_item(num, r, section, pad=0):
        badge = ("<span style='background:#1a4fa0;color:#fff;font-size:10px;padding:1px 5px;"
                 "border-radius:3px;margin-left:6px'>NEW</span>" if is_new.get(r["id"]) else "")
        if _overdue(r):
            flag = f"<span style='color:#b3261e;font-weight:600'> · OVERDUE {r['due']}</span>"
        elif r["due"]:
            flag = f"<span style='color:#666'> · due {r['due']}</span>"
        else:
            flag = ""
        H.append(f"<div style='margin:9px 0 12px {pad}px'>"
                 f"<b>{num}.</b> {html.escape(r['title'] or '')}{flag}{badge}")
        if section == "waiting" and r["promiser"] and not pad:
            H.append(f"<div style='color:#8a6d00'>owed by {html.escape(r['promiser'])}</div>")
        if section == "stale":
            H.append(f"<div style='color:#888'>no movement for {_age_days(r)} days, "
                     f"close it or tell me it's live</div>")
        q = (r["done_quote"] if section == "confirm" else r["origin_quote"]) or ""
        if q:
            H.append(f"<div style='color:#555;padding-left:12px;border-left:3px solid #ddd;"
                     f"margin:4px 0'>&ldquo;{html.escape(' '.join(q.split())[:220])}"
                     f"&rdquo;</div>")
        if r["last_work"]:
            H.append(f"<div style='color:#1b6b3a'>progress ({r['last_work_when']}): "
                     f"{html.escape(' '.join(r['last_work'].split())[:160])}</div>")
        if r["origin_doc"]:
            H.append(f"<div style='color:#999;font-size:11.5px'>"
                     f"{_src_label(r['origin_src'])}: "
                     f"{html.escape((r['origin_doc'] or '')[:58])} · {r['origin_when'] or ''}</div>")
        H.append("</div>")

    for sec in SECTIONS:
        items = buckets.get(sec, [])
        if not items:
            continue
        c = _COLOURS[sec]
        H.append(f"<div style='margin:22px 0 6px;font-family:system-ui;font-weight:600;"
                 f"color:{c};border-bottom:2px solid {c};padding-bottom:3px'>"
                 f"{_TITLES[sec]}</div>")
        if sec == "waiting":
            grouped, singles = group_waiting(items)
            for person, entries in grouped:
                oldest = max((_age_days(r) or 0) for _, r in entries)
                H.append(f"<div style='margin:12px 0 2px;color:#8a6d00;font-weight:600'>"
                         f"{html.escape(person)} owes you {len(entries)} things "
                         f"<span style='font-weight:400;color:#999'>(oldest {oldest}d)</span></div>")
                for num, r in entries:
                    html_item(num, r, sec, pad=18)
            for num, r in singles:
                html_item(num, r, sec)
        else:
            for num, r in items:
                html_item(num, r, sec)

    H += ["<hr style='border:none;border-top:1px solid #ddd;margin:24px 0 10px'>",
          "<div style='font-family:system-ui;color:#555'>Reply with item numbers in plain "
          "English, <i>&ldquo;1 3 done, 7 chase Sophie Friday, 9 not mine&rdquo;</i></div></div>"]

    return {"subject": subject, "text": text, "html": "\n".join(H),
            "items": [(n, r["id"], _classify(r)) for n, r in numbered],
            "counts": counts}


def save(conn, built: dict) -> int:
    cur = conn.execute("INSERT INTO briefs(ts, subject, html) VALUES(?,?,?)",
                       (db.now(), built["subject"], built["html"]))
    brief_id = cur.lastrowid
    for num, task_id, section in built["items"]:
        conn.execute("INSERT OR REPLACE INTO brief_items VALUES(?,?,?,?)",
                     (brief_id, num, task_id, section))
    conn.commit()
    return brief_id
