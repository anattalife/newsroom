"""The Community Board: lost & found, garage sales, things for sale or free, wanted, and questions for neighbors.
Members post for free; posts come down on their own after LIFE_DAYS. Posting earns no points."""
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from . import settings
from .db import loads, now

log = logging.getLogger("newsroom.board")

# key, icon, label, what "done" means for this kind
KINDS = [("lost", "🐾", "Lost & found", "Reunited"), ("garage", "🏷️", "Garage sale", "Done"),
         ("sale", "💲", "For sale", "Sold"), ("free", "🎁", "Free", "Taken"),
         ("wanted", "🔎", "Wanted", "Found it"), ("ask", "🙋", "Ask the neighbors", "Answered")]
KIND = {k: {"key": k, "icon": i, "label": label, "done": d} for k, i, label, d in KINDS}
LIFE_DAYS = 14        # a post comes down on its own after this
PIN_DAYS = 3          # lost & found posts stay at the top this long
DONE_HOURS = 24       # a finished post stays up this long, marked done
MAX_PER_DAY = 5


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _in(days=0, hours=0):
    return _iso(datetime.now(timezone.utc) + timedelta(days=days, hours=hours))


def live_sql(alias="p"):
    """Posts people can see on the board: open and not expired, or finished in the last day."""
    return (f"(({alias}.status='open' AND {alias}.expires_at > ?) OR ({alias}.status='done' AND {alias}.done_at > ?))",
            (now(), _in(hours=-DONE_HOURS)))


def decorate(p):
    if not p:
        return p
    p["k"] = KIND.get(p["kind"], KIND["ask"])
    t = now()
    p["pinned"] = bool(p["status"] == "open" and p.get("pinned_until") and p["pinned_until"] > t)
    p["expired"] = p["status"] == "open" and p["expires_at"] <= t
    p["live"] = (p["status"] == "open" and not p["expired"]) or (
        p["status"] == "done" and (p.get("done_at") or "") > _in(hours=-DONE_HOURS))
    p["can_renew"] = p["status"] == "open" and not p["renewed"]
    return p


def listing(db, kind=None, limit=60):
    where, args = live_sql()
    if kind in KIND:
        where += " AND p.kind=?"
        args = (*args, kind)
    rows = db.q(f"SELECT p.*, m.username FROM board_posts p JOIN members m ON m.id=p.member_id WHERE {where} "
                f"ORDER BY (p.status='open' AND p.pinned_until > ?) DESC, p.status='done', p.created_at DESC LIMIT ?",
                (*args, now(), limit))
    return [decorate(r) for r in rows]


def get(db, pid):
    return decorate(db.one("SELECT p.*, m.username FROM board_posts p JOIN members m ON m.id=p.member_id "
                           "WHERE p.id=?", (pid,)))


def create(db, member, kind, title, body, town="", when_text="", photo=None, phone=""):
    t = now()
    fields = dict(member_id=member["id"], kind=kind, title=title, body=body, town=town, when_text=when_text,
                  photo=photo, phone=phone, status="open", expires_at=_in(days=LIFE_DAYS), created_at=t, updated_at=t)
    if kind == "lost":
        fields["pinned_until"] = _in(days=PIN_DAYS)
        fields["social"] = json.dumps({"facebook": "pending"})  # lost pets go out on the Facebook page
    return db.insert("board_posts", **fields)


def recount(db, pid):
    db.run("UPDATE board_posts SET reply_count=(SELECT COUNT(*) FROM board_replies WHERE post_id=? AND "
           "status='visible') WHERE id=?", (pid, pid))


def share_pending(db, poster=None):
    """Post lost & found notices to the Facebook page (when it's connected). Returns attempts."""
    from . import social
    base = social.site_url(db)
    if not base or not social.connected(db, "facebook"):
        return 0
    n = 0
    where, args = live_sql()
    for p in db.q(f"SELECT * FROM board_posts p WHERE {where} AND p.status='open' AND p.social LIKE '%pending%' "
                  f"LIMIT 10", args):
        n += 1
        text = f"{KIND[p['kind']]['icon']} {p['title']}" + (f" ({p['town']})" if p["town"] else "") + \
            f"\n\n{p['body'][:600]}\n\nSeen something? Reply on the Hillsdale Watch Community Board."
        link = f"{base}/board/{p['id']}"
        try:
            url = (poster or social.POSTERS["facebook"])(db, {**p, "image": p["photo"]}, text, link)
            result = {"facebook": "posted", "url": url}
        except (social.SocialError, requests.RequestException, KeyError, OSError) as e:
            log.warning("board share failed: %s", e)
            result = {"facebook": "failed", "error": str(e)[:200]}
        db.update("board_posts", p["id"], social=json.dumps(result))
    return n


def mine(db, member_id):
    rows = db.q("SELECT p.*, '' AS username FROM board_posts p WHERE p.member_id=? AND p.status IN ('open','done') "
                "ORDER BY p.id DESC LIMIT 20", (member_id,))
    return [decorate(r) for r in rows]


def social_state(p):
    return loads(p.get("social"), {}) if p else {}


def towns(db):
    """Suggestions for the Town box: the home town plus school towns."""
    out = [settings.get(db, "town") or ""]
    out += [r["town"] for r in db.q("SELECT DISTINCT town FROM sports_schools WHERE town!='' ORDER BY town")]
    return [t for i, t in enumerate(out) if t and t not in out[:i]]
