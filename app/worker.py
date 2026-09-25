"""Background worker: checks sources, drafts, publishes scheduled stories, sends notifications, backs up.

Runs as its own process (`python -m app worker`) so the website stays fast.
"""
import logging
import sqlite3
import time
import zipfile
from datetime import datetime, timezone

from . import db as dbm
from . import pipeline, settings, util
from .db import BACKUPS, DATA_DIR, DB, UPLOADS

log = logging.getLogger("newsroom.worker")


def make_backup(label="manual"):
    """Zip of a consistent database copy + uploads + keys. Returns the path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tmp = BACKUPS / f"db-{stamp}.sqlite"
    src = sqlite3.connect(dbm.DB_PATH)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    out = BACKUPS / f"newsroom-{label}-{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "newsroom.db")
        for key in ("secret.key", "session.key"):
            if (DATA_DIR / key).exists():
                z.write(DATA_DIR / key, key)
        for f in UPLOADS.iterdir():
            if f.is_file():
                z.write(f, f"uploads/{f.name}")
    tmp.unlink()
    return out


def prune_backups(keep):
    nightly = sorted(BACKUPS.glob("newsroom-nightly-*.zip"))
    for f in nightly[:-max(1, keep)]:
        f.unlink()
    for f in sorted(BACKUPS.glob("newsroom-manual-*.zip"))[:-3]:
        f.unlink()


def notify(db, new_items, failing_sources):
    to = util.notify_address(db)
    if not to or not settings.get(db, "smtp_host"):
        return
    name = settings.get(db, "site_name")
    if new_items and settings.get(db, "notify_drafts"):
        lines = [f"- {src}: {n} new item{'s' if n != 1 else ''}" for src, n in new_items.items()]
        util.send_email(db, to, f"{name}: {sum(new_items.values())} new item(s) to look at",
                        "New items have been collected:\n\n" + "\n".join(lines) +
                        "\n\nOpen the newsroom to pick which ones to develop.")
    for s in failing_sources:
        if settings.get(db, "notify_failures"):
            util.send_email(db, to, f"{name}: source '{s['name']}' stopped working",
                            f"The source '{s['name']}' has failed 3 times in a row.\n\nLast error: {s['last_error']}")


def cycle(db, writer=None):
    """One pass: publish what's due, check due sources, refresh weather. Returns auto-written story ids."""
    pipeline.publish_due(db)
    pipeline.release_stale(db)
    created, failing, new_items = [], [], {}
    for s in pipeline.sources_due(db):
        n, ids, problem = pipeline.process_source(db, s, writer=writer)
        created += ids
        fresh = db.one("SELECT * FROM sources WHERE id=?", (s["id"],))
        if fresh["error_count"] == 3:
            failing.append(fresh)
        if n:
            new_items[s["name"]] = n
            log.info("%s: %d new item(s)%s%s", s["name"], n, f", {len(ids)} auto-written" if ids else "",
                     f" — {problem}" if problem else "")
    try:
        from . import weather
        weather.refresh_if_due(db)
    except Exception:
        log.exception("weather refresh failed")
    try:
        from . import social
        social.refresh_threads(db)
        social.post_pending(db)
    except Exception:
        log.exception("social posting failed")
    try:
        from . import callit
        callit.settle_due(db)
    except Exception:
        log.exception("settling Call It failed")
    notify(db, new_items, failing)
    return created


def run():
    dbm.init()
    log.info("worker started")
    last_backup_day = last_crowns_day = None
    while True:
        db = DB()
        try:
            cycle(db)
            local_now = datetime.now(util.tz(db))
            if local_now.hour == 4 and last_crowns_day != local_now.date():
                from . import crowns
                crowns.refresh_all(db)          # a new week or season can move a crown
                last_crowns_day = local_now.date()
            if local_now.hour == 3 and last_backup_day != local_now.date():
                make_backup("nightly")
                prune_backups(int(settings.get(db, "backup_keep") or 7))
                last_backup_day = local_now.date()
        except Exception:
            log.exception("worker cycle failed")
        finally:
            db.close()
        time.sleep(60)
