"""Chief of Staff, pipeline entry point.

  python run.py --preview     ingest, extract, build a brief, print it (sends nothing)
  python run.py --ingest      ingest only (the every-3-hours job)
  python run.py --brief       full pipeline and email the brief
  python run.py --status      what's in the database right now
"""
import argparse
import sys
from datetime import date, datetime
import traceback

from cos import (brief, complete, config, db, extract, ingest_notion,
                 ingest_outlook, reconcile)


def _ingest(conn, cold_days=None) -> dict:
    out = {}
    try:
        out["outlook"] = ingest_outlook.ingest(conn, cold_start_days=cold_days)
    except Exception as exc:
        out["outlook"] = {"errors": [repr(exc)]}
    try:
        out["notion"] = ingest_notion.ingest(conn, cold_start_days=cold_days)
    except Exception as exc:
        out["notion"] = {"errors": [repr(exc)]}
    return out


class _Tee:
    """Mirror everything printed into logs/cos.log, timestamped.

    The log used to come from cmd's `>> logs\cos.log` in run_cos.bat. cmd takes
    that handle exclusively, so a task launching while another still held it
    silently skipped its command and exited 0, two briefs disappeared that way
    with a green tick in Task Scheduler. Python opens the file shared, so
    concurrent runs both write and neither is lost.
    """

    def __init__(self, stream, path):
        self.stream, self.path, self.at_line_start = stream, path, True

    def write(self, text):
        self.stream.write(text)
        stamped = ""
        for part in text.splitlines(keepends=True):
            if self.at_line_start and part.strip():
                stamped += datetime.now().strftime("%Y-%m-%d %H:%M:%S ")
            stamped += part
            self.at_line_start = part.endswith('\n')
        try:
            with open(self.path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(stamped)
        except OSError:
            pass                      # never let logging break the run
        return len(text)

    def flush(self):
        self.stream.flush()


def _start_logging() -> None:
    path = config.ROOT / "logs" / "cos.log"
    path.parent.mkdir(exist_ok=True)
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)


class _Busy(RuntimeError):
    """Another run holds the lock. Benign, not a failure."""


def _pid_alive(pid: str) -> bool:
    """Is the PID in the lock file still running?

    Age alone is not enough. When a run is killed mid-flight - the scheduler
    hands the interactive job a Ctrl+C on session resume and it dies with
    0xC000013A - the lock file survives with a dead PID in it, and every run
    for the next two hours is skipped as "another run is active". Seen 31 Aug.
    Unknown/unreadable PID stays conservative and reports alive.
    """
    if not pid.isdigit():
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return True                       # not Windows, or no ctypes - assume alive
    handle = kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259      # STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


class _Lock:
    """Single-instance guard.

    Task Scheduler's IgnoreNew only stops *scheduled* runs overlapping. A manual
    run colliding with the 3-hourly job will happily send a second identical
    brief, which is how the duplicate on 30 July happened.
    """

    def __init__(self, path, stale_after=7200, wait=0):
        self.path, self.stale_after, self.held = path, stale_after, False
        # The brief is the one run that must not be skipped: a 3-hourly refresh
        # that happens to overlap it costs the whole day's brief. Wait it out.
        self.wait = wait

    def __enter__(self):
        import os, time
        deadline = time.time() + self.wait
        while True:
            if self.path.exists():
                age = time.time() - self.path.stat().st_mtime
                pid = self.path.read_text(errors="ignore").strip()
                if age < self.stale_after and _pid_alive(pid):
                    if time.time() < deadline:
                        print(f"· waiting for run {pid} to finish…", flush=True)
                        time.sleep(30)
                        continue
                    raise _Busy(f"another run is active (pid {pid}, {int(age)}s old) - skipping")
                if self.path.exists():             # previous run died; take over
                    print(f"· clearing stale lock (pid {pid})", flush=True)
                self.path.unlink(missing_ok=True)
            self.path.write_text(str(os.getpid()))
            self.held = True
            return self

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)
        return False


def _apply_pending_replies(conn) -> dict:
    """Find replies to briefs sitting in Sent Items and apply them.

    A reply is matched to the brief it answers by timestamp, not by "the latest
    brief", otherwise a reply written yesterday evening gets applied against
    this morning's numbering.
    """
    from cos import reply_parse

    rows = conn.execute(
        """SELECT id, ts, body FROM documents
           WHERE source='outlook_sent'
             AND json_extract(meta,'$.is_brief_reply') = 1
             AND id NOT IN (SELECT COALESCE(document_id,-1) FROM reply_log)
           ORDER BY ts""").fetchall()
    out = {"replies": 0, "ops": 0}
    for row in rows:
        brief_row = conn.execute(
            "SELECT id FROM briefs WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (row["ts"],)
        ).fetchone()
        if not brief_row:
            continue
        text = reply_parse.strip_quoted(row["body"] or "")
        if not text:
            continue
        res = reply_parse.apply_reply(conn, text, brief_id=brief_row["id"])
        if res.get("error"):
            # Do NOT log it: reply_log is "this reply has been consumed", and
            # writing it on a failure throws the reply away for good. Three
            # replies were lost that way while the LLM backend was down. Leave
            # it unlogged and the next run retries it.
            out["failed"] = out.get("failed", 0) + 1
            print(f"   REPLY NOT APPLIED (doc {row['id']}, will retry): "
                  f"{res['error']}", flush=True)
            continue
        conn.execute("INSERT OR IGNORE INTO reply_log(document_id, brief_id, applied_at) "
                     "VALUES(?,?,?)", (row["id"], brief_row["id"], db.now()))
        conn.commit()
        out["replies"] += 1
        out["ops"] += res.get("applied", 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--ingest", action="store_true")
    # The 3-hourly job. Ingest AND extract, but send nothing. Without this the
    # whole day's documents queue up for the 07:30 run, which then has to
    # extract ~50 documents before it can send anything.
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--cold-days", type=int, default=None)
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="send even at the weekend")
    args = ap.parse_args()

    if not args.preview:
        _start_logging()

    conn = db.init()

    if args.status:
        for label, sql in (
            ("documents", "SELECT COUNT(*) FROM documents"),
            ("  unprocessed", "SELECT COUNT(*) FROM documents WHERE processed_at IS NULL"),
            ("tasks open", "SELECT COUNT(*) FROM tasks WHERE status='open'"),
            ("  waiting_on", "SELECT COUNT(*) FROM tasks WHERE status='open' AND kind='waiting_on'"),
            ("  likely_done", "SELECT COUNT(*) FROM tasks WHERE status='likely_done'"),
            ("entities", "SELECT COUNT(*) FROM entities"),
            ("rules", "SELECT COUNT(*) FROM rules WHERE active=1"),
        ):
            print(f"{label:<16} {conn.execute(sql).fetchone()[0]}")
        return 0

    try:
        with _Lock(config.DATA / "cos.lock", wait=1800 if args.brief else 0):
            return _pipeline(conn, args)
    except _Busy as exc:
        # Not a failure: another run is already doing this work. Exit 0 so the
        # scheduler's history shows success and a real crash still stands out.
        print(f"· {exc}", flush=True)
        return 0


def _brief_sent_today(conn) -> bool:
    """Has a brief already gone out today (local date)? briefs.ts is UTC."""
    today = date.today()
    for (ts,) in conn.execute("SELECT ts FROM briefs ORDER BY id DESC LIMIT 5"):
        try:
            if datetime.fromisoformat(ts).astimezone().date() == today:
                return True
        except ValueError:
            continue
    return False


def _pipeline(conn, args) -> int:
    run_id = db.start_run(conn, "brief" if args.brief else
                                ("refresh" if args.refresh else "preview"))
    try:
        if not args.skip_ingest:
            print("· ingesting…", flush=True)
            stats = _ingest(conn, args.cold_days)
            print("  ", {k: {kk: vv for kk, vv in v.items() if kk != "errors"}
                         for k, v in stats.items()}, flush=True)

        if args.ingest:
            db.finish_run(conn, run_id, {"mode": "ingest"})
            return 0

        # Replies must land BEFORE a new brief is built, or the new brief shows
        # items you already closed and the numbering shifts under your answer.
        print("· applying replies…", flush=True)
        print("  ", _apply_pending_replies(conn), flush=True)

        print("· extracting…", flush=True)
        ex = extract.run(conn, verbose=True)
        print("  ", {k: v for k, v in ex.items() if k != "errors"}, flush=True)
        for e in ex.get("errors", [])[:3]:
            print("   ERR", e, flush=True)

        print("· reconciling…", flush=True)
        print("  ", reconcile.run(conn), flush=True)

        print("· checking completions…", flush=True)
        print("  ", complete.run(conn), flush=True)

        # Catch-up. If the 07:30 run was killed mid-flight (10 Sep 2026: a
        # Windows Update restart at 09:29 took it down with 0x40010004), the
        # trigger is spent and nothing retries it. The first refresh after
        # 07:30 on a weekday with no brief yet today sends it instead.
        now = datetime.now()
        if (args.refresh and now.weekday() < 5
                and (now.hour, now.minute) >= (7, 30)
                and not _brief_sent_today(conn)):
            print("· no brief sent yet today - catching up", flush=True)
            args.refresh, args.brief = False, True

        # The reverse guard: a StartWhenAvailable/restart re-run of --brief
        # after a catch-up already sent must not email a second copy.
        if args.brief and not args.force and _brief_sent_today(conn):
            db.finish_run(conn, run_id, {"mode": "brief", "skipped": "already sent today"})
            print("  brief already sent today (use --force to resend).")
            return 0

        if args.refresh:
            db.finish_run(conn, run_id, {"mode": "refresh", **{k: v for k, v in ex.items()
                                                               if k != "errors"}})
            print("  refresh done (no brief sent).")
            return 0

        # No weekend briefs. The weekday-only trigger normally handles this, but
        # StartWhenAvailable can carry a missed Friday run into Saturday, so guard
        # here too. Work still gets ingested and extracted; it just isn't sent.
        if args.brief and not args.force and date.today().weekday() >= 5:
            db.finish_run(conn, run_id, {"mode": "brief", "skipped": "weekend"})
            print(f"  {date.today():%A} - weekend, not sending. Use --force to override.")
            return 0

        print("· building brief…", flush=True)
        built = brief.build(conn)

        if args.preview:
            print("\n" + "=" * 70)
            print(built["text"])
            (config.DATA / "preview_brief.html").write_text(built["html"], encoding="utf-8")
            print(f"\n[html preview written to {config.DATA / 'preview_brief.html'}]")
        else:
            from cos import mailer
            brief.save(conn, built)
            mailer.send(built["subject"], built["html"])
            print("  sent.")

        db.finish_run(conn, run_id, {"counts": built["counts"]})
        return 0
    except Exception:
        db.finish_run(conn, run_id, error=traceback.format_exc()[:4000])
        raise


if __name__ == "__main__":
    sys.exit(main())
