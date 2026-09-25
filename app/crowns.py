"""Crowns: one holder at a time, shown next to their name everywhere, until someone passes them.

  #1 Fan of a team        most sports points on that team's games this season
  Top Reporter · section  most reporting points on that section's stories in the last 90 days
  Oracle of the Week      most Call It points in the last 7 days
"""
from datetime import datetime, timedelta, timezone

from . import community, util
from .db import now

MIN_SCORE = 5          # nobody wears a crown for less
SPORTS_REASONS = ("score", "live", "stream", "video")
REPORT_REASONS = ("article", "tip", "featured", "photo")


# ── seasons (for the sports crowns and board) ─────────────
def season(db, at=None):
    """(label, start_utc_iso, end_utc_iso): Fall Aug–Nov, Winter Dec–mid March, Spring mid March–July."""
    d = (at or datetime.now(util.tz(db))).astimezone(util.tz(db))
    y = d.year
    if (d.month, d.day) >= (8, 1) and d.month <= 11:
        name, start, end = f"Fall {y}", datetime(y, 8, 1), datetime(y, 12, 1)
    elif d.month == 12 or (d.month, d.day) < (3, 15):
        y0 = y if d.month == 12 else y - 1
        name, start, end = f"Winter {y0}–{str(y0 + 1)[2:]}", datetime(y0, 12, 1), datetime(y0 + 1, 3, 15)
    else:
        name, start, end = f"Spring {y}", datetime(y, 3, 15), datetime(y, 8, 1)
    tz = util.tz(db)
    return (name, start.replace(tzinfo=tz).astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.replace(tzinfo=tz).astimezone(timezone.utc).isoformat(timespec="seconds"))


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _marks(xs):
    return ",".join("?" * len(xs))


# ── who's leading ─────────────────────────────────────────
def team_scores(db, team_id, limit=10):
    _, start, end = season(db)
    return db.q(f"SELECT p.member_id, SUM(p.points) AS score FROM points_log p JOIN games g "
                f"ON (p.ref = 'game:' || g.id OR p.ref LIKE 'game:' || g.id || ':%') "
                f"JOIN members m ON m.id=p.member_id "
                f"WHERE g.team_id=? AND p.reason IN ({_marks(SPORTS_REASONS)}) AND p.created_at >= ? AND p.created_at < ? "
                f"AND m.status='active' GROUP BY p.member_id HAVING score > 0 ORDER BY score DESC, MIN(p.id) LIMIT ?",
                (team_id, *SPORTS_REASONS, start, end, limit))


def section_scores(db, category, limit=10):
    # only stories the member agreed to be named on: a crown must never give away an anonymous writer
    return db.q(f"SELECT p.member_id, SUM(p.points) AS score FROM points_log p JOIN stories s "
                f"ON p.ref = 'story:' || s.id JOIN members m ON m.id=p.member_id "
                f"WHERE s.category=? AND p.reason IN ({_marks(REPORT_REASONS)}) AND p.created_at >= ? "
                f"AND (s.credit_public=1 OR (p.reason='photo' AND s.photo_member_id=p.member_id)) "
                f"AND m.status='active' GROUP BY p.member_id HAVING score > 0 ORDER BY score DESC, MIN(p.id) LIMIT ?",
                (category, *REPORT_REASONS, _ago(90), limit))


def oracle_scores(db, limit=10):
    return db.q("SELECT p.member_id, SUM(p.points) AS score FROM points_log p JOIN members m ON m.id=p.member_id "
                "WHERE p.reason='callit' AND p.created_at >= ? AND m.status='active' GROUP BY p.member_id "
                "HAVING score > 0 ORDER BY score DESC, MIN(p.id) LIMIT ?", (_ago(7), limit))


def _label(db, kind, key):
    if kind == "team":
        from . import sports
        t = db.one(f"{sports.TEAM_SQL} WHERE t.id=?", (int(key),))
        return f"#1 Fan · {sports.team_label(t)}" if t else "#1 Fan"
    if kind == "section":
        return f"Top Reporter · {key}"
    return "Oracle of the Week"


def _link(db, kind, key):
    if kind == "team":
        slug = db.val("SELECT slug FROM sports_teams WHERE id=?", (int(key),))
        return f"/sports/team/{slug}#fans" if slug else "/crowns"
    return "/crowns"


def update(db, kind, key):
    """Recompute one crown. If it changes hands, tell both people and the whole site."""
    key = str(key)
    scores = {"team": lambda: team_scores(db, int(key), 2), "section": lambda: section_scores(db, key, 2),
              "oracle": lambda: oracle_scores(db, 2)}[kind]()
    row = db.one("SELECT * FROM crowns WHERE kind=? AND key=?", (kind, key))
    label = _label(db, kind, key)
    leader = scores[0] if scores and scores[0]["score"] >= MIN_SCORE else None
    old = row["member_id"] if row else None
    if leader and old and leader["member_id"] != old:
        mine = next((s["score"] for s in scores if s["member_id"] == old), None)
        if mine is not None and mine >= leader["score"]:
            leader = {"member_id": old, "score": mine}          # a tie keeps the crown where it is
    new = leader["member_id"] if leader else None
    t = now()
    if not row:
        if not new:
            return None
        cid = db.insert("crowns", kind=kind, key=key, label=label, member_id=new, score=leader["score"], since=t,
                        updated_at=t)
    else:
        cid = row["id"]
        db.update("crowns", cid, label=label, member_id=new, score=leader["score"] if leader else 0,
                  since=t if new != old else row["since"], updated_at=t)
    if new != old:
        link = _link(db, kind, key)
        if new:
            db.insert("crown_log", crown_id=cid, member_id=new, created_at=t)
            winner = db.val("SELECT username FROM members WHERE id=?", (new,))
            community.notice(db, new, f"👑 You're wearing the crown: {label}! Hold on to it.", link, kind="crown")
            if old:
                community.notice(db, old, f"👑 @{winner} just took your crown: {label}. Go get it back!", link,
                                 kind="crown")
            community.shoutout(db, f"👑 @{winner} is the new {label}!", link, kind="crown", member_id=new)
            community.check_badges(db, new)
        elif old:
            community.notice(db, old, f"👑 Your crown ({label}) is up for grabs again.", link, kind="crown")
    return new


def after_points(db, reason, ref):
    """Called whenever points are given or taken back, to keep the right crowns up to date."""
    try:
        if reason in SPORTS_REASONS and ref.startswith("game:"):
            gid = int(ref.split(":")[1])
            tid = db.val("SELECT team_id FROM games WHERE id=?", (gid,))
            if tid:
                update(db, "team", tid)
        elif reason in REPORT_REASONS and ref.startswith("story:"):
            cat = db.val("SELECT category FROM stories WHERE id=?", (int(ref.split(":")[1]),))
            if cat:
                update(db, "section", cat)
        elif reason == "callit":
            update(db, "oracle", "week")
    except (ValueError, IndexError):
        pass


def refresh_all(db):
    """Once a day: windows move (a new week, a new season), so crowns can change without new points."""
    for r in db.q("SELECT kind, key FROM crowns"):
        update(db, r["kind"], r["key"])
    update(db, "oracle", "week")


# ── showing them ──────────────────────────────────────────
def all_by_member(db):
    out = {}
    for r in db.q("SELECT * FROM crowns WHERE member_id IS NOT NULL ORDER BY kind='team' DESC, id"):
        out.setdefault(r["member_id"], []).append(r)
    return out


def listing(db):
    return db.q("SELECT c.*, m.username FROM crowns c LEFT JOIN members m ON m.id=c.member_id "
                "ORDER BY c.kind='oracle' DESC, c.kind, c.label")
