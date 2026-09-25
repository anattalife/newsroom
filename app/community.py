"""Members' points, badges, privileges and notices.

Points are recorded in points_log with a unique (member, reason, ref), so the same thing can never earn points twice,
and members.points is always recalculated from the log.
"""
import json
from datetime import datetime, timedelta, timezone

from . import settings, util
from .db import loads, now

# ── badges ────────────────────────────────────────────
# Most badges level up: Bronze → Silver → Gold → Legend at the thresholds in `tiers`. Secret badges hide their
# name until you earn one; everyone sees the hint. Rarity is how special it is meant to feel.
TIER_NAMES = ["Bronze", "Silver", "Gold", "Legend"]
RARITIES = [("common", "Common"), ("rare", "Rare"), ("epic", "Epic"), ("legendary", "Legendary")]
#  slug, name, icon, chapter, description, rule, tiers, rarity, secret, hint
BADGES = [
    # 📰 Reporter
    ("first-tip", "Tipster", "💡", "reporting", "Tips that became published stories", "tips_published", [1, 5, 15, 40], "common", 0, ""),
    ("first-byline", "Byline", "✍️", "reporting", "Stories you wrote that were published", "articles_published", [1, 5, 15, 40], "common", 0, ""),
    ("scoop", "Front Page", "📰", "reporting", "Your stories that were Featured", "featured", [1, 3, 10, 25], "rare", 0, ""),
    ("beat-reporter", "Beat Reporter", "🗂️", "reporting", "Published stories in one section", "category_max", [5, 10, 25, 50], "rare", 0, ""),
    ("photographer", "Shutterbug", "📷", "reporting", "Your photos used in published stories", "photos_used", [1, 5, 15, 40], "common", 0, ""),
    ("stringer", "Stringer", "🧵", "reporting", "Stories published with your name, of any kind", "published_total", [10, 25, 50, 100], "epic", 0, ""),
    # 💬 Neighbor
    ("first-comment", "Town Talker", "💬", "community", "Comments you've posted", "comments", [1, 25, 100, 500], "common", 0, ""),
    ("helpful", "Helpful", "👍", "community", "Upvotes on your best comment", "comment_upvotes_max", [5, 10, 25, 50], "rare", 0, ""),
    ("conversation-starter", "Conversation Starter", "🗣️", "community", "Replies to your best comment", "comment_replies_max", [5, 10, 20, 40], "rare", 0, ""),
    ("good-eye", "Good Eye", "🔎", "community", "Flags that led to a correction", "good_flags", [1, 3, 10, 25], "rare", 0, ""),
    ("welcome-wagon", "Welcome Wagon", "👋", "community", "First to reply to a new member", "welcomes", [5, 15, 40, 100], "rare", 0, ""),
    # 🏈 Sports
    ("sideline-reporter", "Sideline Reporter", "📣", "sports", "Live updates posted from games", "live_updates", [1, 25, 100, 500], "common", 0, ""),
    ("broadcaster", "Broadcaster", "🎥", "sports", "Games you live streamed", "streams", [1, 5, 15, 40], "rare", 0, ""),
    ("scorekeeper", "Scorekeeper", "🏁", "sports", "Final scores you posted", "final_scores", [1, 10, 25, 75], "common", 0, ""),
    ("stat-checker", "Stat Checker", "✅", "sports", "Scores you confirmed", "confirms", [5, 25, 75, 200], "common", 0, ""),
    ("highlight-reel", "Highlight Reel", "🎬", "sports", "Game videos you shared", "videos", [1, 5, 15, 40], "rare", 0, ""),
    # 🎯 Call It
    ("called-it", "Called It", "🎯", "callit", "Call It wins: the closest guess, or an exact score", "callit_wins", [1, 3, 10, 25], "rare", 0, ""),
    ("crystal-ball", "Crystal Ball", "🔮", "callit", "Call It guesses made", "callit_guesses", [5, 25, 100, 250], "common", 0, ""),
    # 👑 Crowns
    ("crowned", "Crowned", "👑", "crowns", "Different crowns you've worn", "crowns_held", [1, 3, 5, 10], "epic", 0, ""),
    # 🔥 Loyalty
    ("member-1m", "Neighbor Since", "🌳", "loyalty", "Days as a member", "days", [30, 180, 365, 1095], "common", 0, ""),
    ("weekly-reader", "Regular", "📅", "loyalty", "Weeks in a row you visited", "week_streak", [4, 12, 26, 52], "rare", 0, ""),
    ("founding-member", "Founding Member", "🏛️", "loyalty", "Joined in the community's first 90 days", "founding", [1], "epic", 0, ""),
    # 🕵️ Secret
    ("cold-bleachers", "Cold Bleachers", "🥶", "secret", "Posted live from a game when it was below freezing", "cold_updates", [1], "legendary", 1,
     "Brrr. Some fans show up no matter what."),
    ("night-owl", "Night Owl", "🌙", "secret", "Posted a live update after 11 p.m.", "late_updates", [1], "epic", 1,
     "Past bedtime, and still posting."),
    ("every-gym", "Every Gym in the County", "🗺️", "secret", "Posted live from games at 5 different places", "venues", [5], "rare", 1,
     "The whole county, one gym at a time."),
    ("bullseye", "Bullseye", "🏹", "secret", "Called It exactly: the exact score or the exact number", "callit_exact", [1], "epic", 1,
     "Nailed it. Exactly."),
    ("friday-lights", "Friday Night Lights", "🔥", "secret", "Live updates from football games on 5 different Fridays", "friday_football", [5], "rare", 1,
     "Football. Friday. Five times over."),
    ("triple-crown", "Triple Crown", "🤴", "secret", "Wore three crowns at the same time", "crowns_now", [3], "legendary", 1,
     "Heavy is the head…"),
    # ⭐ Special, awarded by hand
    ("staff", "Staff", "🛡️", "special", "Works in the newsroom", "manual", [1], "common", 0, ""),
    ("partner", "Community Partner", "🤝", "special", "Shares news for a local organization", "manual", [1], "common", 0, ""),
    ("editors-pick", "Editor's Pick", "⭐", "special", "Picked by the editor", "manual", [1], "epic", 0, ""),
    ("hometown-hero", "Hometown Hero", "🏅", "special", "Went above and beyond for the community", "manual", [1], "legendary", 0, ""),
]
# badges folded into a tiered one (their holders get the matching tier instead)
RETIRED = ("correspondent", "member-6m", "member-1y")
# earlier default names, replaced on upgrade only if the newsroom never renamed them
OLD_NAMES = {"first-tip": "First Tip", "first-byline": "First Byline", "scoop": "Scoop", "photographer": "Photographer",
             "first-comment": "First Comment", "member-1m": "One Month", "weekly-reader": "Weekly Reader"}
GROUPS = [("reporting", "📰 Reporter"), ("community", "💬 Neighbor"), ("sports", "🏈 Sports"), ("callit", "🎯 Call It"),
          ("crowns", "👑 Crowns"), ("loyalty", "🔥 Loyalty"), ("secret", "🕵️ Secret"),
          ("special", "⭐ Special (you award by hand)")]


def seed_badges(db):
    """Add new badges; on first upgrade, give existing ones their tiers, rarity and new names."""
    for i, (slug, name, icon, grp, desc, rule, tiers, rarity, secret, hint) in enumerate(BADGES):
        old = db.one("SELECT * FROM badges WHERE slug=?", (slug,))
        if not old:
            db.run("INSERT INTO badges(slug,name,icon,grp,description,rule,n,sort,tiers,rarity,secret,hint) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (slug, name, icon, grp, desc, rule, tiers[0], i,
                                                      json.dumps(tiers), rarity, secret, hint))
        elif not old["tiers"]:
            fields = {"tiers": json.dumps(tiers), "rarity": rarity, "secret": secret, "hint": hint, "sort": i,
                      "grp": grp, "rule": rule, "n": tiers[0]}
            if OLD_NAMES.get(slug) == old["name"]:
                fields.update(name=name, icon=icon, description=desc)
            elif old["description"] in ("", None) or slug in OLD_NAMES:
                fields.update(description=desc)
            db.update("badges", old["id"], **fields)
    for slug in RETIRED:
        db.run("UPDATE badges SET active=0 WHERE slug=?", (slug,))


def badge_tiers(b):
    try:
        t = json.loads(b.get("tiers") or "[]")
    except ValueError:
        t = []
    return [int(x) for x in t if str(x).lstrip("-").isdigit()] or [int(b.get("n") or 1)]


def tier_name(b, tier):
    """'Gold', or '' for a badge with one level."""
    return TIER_NAMES[min(tier, 4) - 1] if len(badge_tiers(b)) > 1 and tier else ""


# ── badge rules: fn(db, member_id, n) → a number (how far along you are) ─────────
def _count(db, sql, args):
    return db.val(sql, args) or 0


def _pub(db, mid, extra=""):
    return _count(db, f"SELECT COUNT(*) FROM stories WHERE status='published' AND member_id=? AND org_id IS NULL "
                      f"{extra}", (mid,))


def _week_streak(db, mid):
    weeks = {r["week"] for r in db.q("SELECT week FROM member_visits WHERE member_id=?", (mid,))}
    d, n = datetime.now(timezone.utc), 0
    while week_key(d) in weeks:
        n += 1
        d -= timedelta(days=7)
    return n


def _founding(db, mid, n):
    start = util.parse_iso(db.val("SELECT value FROM meta WHERE key='community_start'"))
    joined = util.parse_iso(db.val("SELECT created_at FROM members WHERE id=?", (mid,)))
    return 1 if start and joined and joined <= start + timedelta(days=90) else 0


def _points_count(db, mid, reason):
    return _count(db, "SELECT COUNT(*) FROM points_log WHERE member_id=? AND reason=?", (mid, reason))


def _venues(db, mid):
    rows = db.q("SELECT DISTINCT CASE WHEN g.home='away' THEN 'o:' || lower(g.opponent) ELSE 's:' || t.school_id END "
                "AS v FROM game_updates u JOIN games g ON g.id=u.game_id JOIN sports_teams t ON t.id=g.team_id "
                "WHERE u.member_id=? AND u.status='visible'", (mid,))
    return len(rows)


def _local_updates(db, mid):
    tzinfo = util.tz(db)
    for r in db.q("SELECT u.created_at, t.sport FROM game_updates u JOIN games g ON g.id=u.game_id "
                  "JOIN sports_teams t ON t.id=g.team_id WHERE u.member_id=? AND u.status='visible'", (mid,)):
        d = util.parse_iso(r["created_at"])
        if d:
            yield d.astimezone(tzinfo), r["sport"]


RULES = {
    "tips_published": ("Tips published", lambda db, m, n: _pub(db, m, "AND credit='tip'")),
    "articles_published": ("Articles written and published", lambda db, m, n: _pub(db, m, "AND credit='byline'")),
    "published_total": ("Stories published (any kind)", lambda db, m, n: _pub(db, m)),
    "featured": ("Stories marked Featured", lambda db, m, n: _pub(db, m, "AND featured=1")),
    "category_max": ("Published stories in one section", lambda db, m, n: db.val(
        "SELECT MAX(c) FROM (SELECT COUNT(*) AS c FROM stories WHERE status='published' AND member_id=? "
        "AND org_id IS NULL GROUP BY category)", (m,)) or 0),
    "photos_used": ("Photos used", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM stories WHERE status='published' AND photo_member_id=? AND org_id IS NULL", (m,))),
    "comments": ("Comments posted", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM comments WHERE member_id=? AND status='visible'", (m,))),
    "comment_upvotes_max": ("Upvotes on one comment", lambda db, m, n: db.val(
        "SELECT MAX(upvotes) FROM comments WHERE member_id=? AND status='visible'", (m,)) or 0),
    "comment_replies_max": ("Replies to one comment", lambda db, m, n: db.val(
        "SELECT MAX(c) FROM (SELECT COUNT(*) AS c FROM comments r JOIN comments p ON p.id=r.parent_id "
        "WHERE p.member_id=? AND r.status='visible' AND r.member_id!=p.member_id GROUP BY p.id)", (m,)) or 0),
    "good_flags": ("Flags that led to a correction", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM flags WHERE member_id=? AND outcome='correction'", (m,))),
    "welcomes": ("First reply to a new member's first comment", lambda db, m, n: _count(
        db, "SELECT COUNT(DISTINCT p.member_id) FROM comments c JOIN comments p ON p.id=c.parent_id "
            "WHERE c.member_id=? AND p.member_id!=c.member_id AND c.status='visible' "
            "AND p.id=(SELECT MIN(id) FROM comments WHERE member_id=p.member_id) "
            "AND c.id=(SELECT MIN(id) FROM comments WHERE parent_id=p.id)", (m,))),
    "days": ("Days as a member", lambda db, m, n: (datetime.now(timezone.utc) - (util.parse_iso(db.val(
        "SELECT created_at FROM members WHERE id=?", (m,))) or datetime.now(timezone.utc))).days),
    "week_streak": ("Weeks visited in a row", lambda db, m, n: _week_streak(db, m)),
    "founding": ("Joined in the first 90 days", _founding),
    "live_updates": ("Live game updates", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM game_updates WHERE member_id=? AND status='visible'", (m,))),
    "streams": ("Games live streamed", lambda db, m, n: _count(
        db, "SELECT COUNT(DISTINCT game_id) FROM game_videos WHERE member_id=? AND kind='stream' AND status='visible'",
        (m,))),
    "videos": ("Game videos shared", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM game_videos WHERE member_id=? AND kind='video' AND status='visible'", (m,))),
    "final_scores": ("Final scores posted", lambda db, m, n: _points_count(db, m, "score")),
    "confirms": ("Scores confirmed", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM game_confirms WHERE member_id=?", (m,))),
    "callit_wins": ("Call It wins", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM guesses WHERE member_id=? AND won=1", (m,))),
    "callit_guesses": ("Call It guesses", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM guesses WHERE member_id=?", (m,))),
    "callit_exact": ("Exact Call It guesses", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM guesses WHERE member_id=? AND exact=1", (m,))),
    "crowns_held": ("Different crowns worn", lambda db, m, n: _count(
        db, "SELECT COUNT(DISTINCT crown_id) FROM crown_log WHERE member_id=?", (m,))),
    "crowns_now": ("Crowns worn right now", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM crowns WHERE member_id=?", (m,))),
    "cold_updates": ("Live updates below freezing", lambda db, m, n: _count(
        db, "SELECT COUNT(*) FROM game_updates WHERE member_id=? AND status='visible' AND temp_f < 32", (m,))),
    "late_updates": ("Live updates after 11 p.m.", lambda db, m, n: sum(
        1 for d, _ in _local_updates(db, m) if d.hour >= 23)),
    "venues": ("Different places posted live from", lambda db, m, n: _venues(db, m)),
    "friday_football": ("Football Fridays posted live", lambda db, m, n: len(
        {d.date() for d, sport in _local_updates(db, m) if sport == "Football" and d.weekday() == 4})),
    "manual": ("Awarded by hand", lambda db, m, n: 0),
}


def notice(db, mid, text, link="", kind="info"):
    if mid:
        db.insert("notices", member_id=mid, kind=kind, text=text[:500], link=link, created_at=now())


def shoutout(db, text, link="", kind="info", member_id=None):
    """A moment the whole site gets to see (a secret discovered, a crown taken, a Call It winner)."""
    db.insert("shoutouts", kind=kind, text=text[:300], link=link, member_id=member_id, created_at=now())


def give_badge(db, mid, badge, by=None, quiet=False, tier=1):
    """Award a badge (or raise its tier). Returns True if something new happened."""
    held = db.one("SELECT * FROM member_badges WHERE member_id=? AND badge_id=?", (mid, badge["id"]))
    tname = tier_name(badge, tier)
    if held and held["tier"] >= tier:
        return False
    if held:
        db.run("UPDATE member_badges SET tier=?, tier_at=?, shown=0 WHERE member_id=? AND badge_id=?",
               (tier, now(), mid, badge["id"]))
        if not quiet:
            notice(db, mid, f"{badge['icon']} {badge['name']} leveled up to {tname}!", "/me/badges", kind="badge")
        return True
    db.insert("member_badges", member_id=mid, badge_id=badge["id"], awarded_at=now(), awarded_by=by, tier=tier,
              tier_at=now(), shown=0 if not quiet else 1)
    if not quiet:
        notice(db, mid, f"You earned {badge['icon']} {badge['name']}{' (' + tname + ')' if tname else ''}: "
                        f"{badge['description']}.", "/me/badges", kind="badge")
        if badge.get("secret"):
            m = db.one("SELECT username FROM members WHERE id=?", (mid,))
            n = db.val("SELECT COUNT(*) FROM member_badges WHERE badge_id=?", (badge["id"],))
            who = f"@{m['username']}" if m else "Someone"
            shoutout(db, f"{who} just discovered a secret badge: {badge['icon']} {badge['name']}. "
                         + ("First in the county!" if n == 1 else f"Only {n} people have found it."),
                     f"/u/{m['username']}" if m else "", kind="secret", member_id=mid)
    return True


def give_badge_slug(db, mid, slug, by=None):
    b = db.one("SELECT * FROM badges WHERE slug=?", (slug,))
    return give_badge(db, mid, b, by=by) if b else False


def badge_value(db, mid, b):
    rule = RULES.get(b["rule"])
    if not rule:
        return 0
    try:
        return int(rule[1](db, mid, b["n"]) or 0)
    except Exception:
        util.log.exception("badge rule %s failed", b["rule"])
        return 0


def check_badges(db, mid, quiet=False):
    """Award new badges and raise tiers. Returns the badges that changed."""
    if not mid:
        return []
    got = []
    held = {r["badge_id"]: r["tier"] for r in db.q("SELECT badge_id, tier FROM member_badges WHERE member_id=?", (mid,))}
    for b in db.q("SELECT * FROM badges WHERE active=1 AND rule!='manual'"):
        tiers = badge_tiers(b)
        value = badge_value(db, mid, b)
        tier = sum(1 for t in tiers if value >= t)
        if tier and tier > held.get(b["id"], 0):
            if give_badge(db, mid, b, tier=tier, quiet=quiet):
                got.append(b)
    return got


def member_badges(db, mid):
    """What a member has, best first: highest tier, then rarest."""
    rows = db.q("SELECT b.*, mb.tier, mb.awarded_at, mb.tier_at FROM member_badges mb JOIN badges b ON b.id=mb.badge_id "
                "WHERE mb.member_id=? AND b.active=1 ORDER BY b.sort", (mid,))
    rank = {k: i for i, (k, _) in enumerate(RARITIES)}
    for r in rows:
        r["tier_name"] = tier_name(r, r["tier"])
    rows.sort(key=lambda r: (-r["tier"], -rank.get(r["rarity"], 0), r["sort"]))
    return rows


def badge_book(db, mid=None):
    """Every active badge with holder counts, and (for a member) their tier and progress to the next one."""
    rows = db.q("SELECT b.*, (SELECT COUNT(*) FROM member_badges mb WHERE mb.badge_id=b.id) AS holders "
                "FROM badges b WHERE active=1 ORDER BY sort")
    mine = {r["badge_id"]: r["tier"] for r in db.q("SELECT badge_id, tier FROM member_badges WHERE member_id=?",
                                                   (mid,))} if mid else {}
    for b in rows:
        tiers = badge_tiers(b)
        b["tier"] = mine.get(b["id"], 0)
        b["tier_name"] = tier_name(b, b["tier"])
        b["tiered"] = len(tiers) > 1
        b["max"] = b["tier"] >= len(tiers)
        b["value"] = badge_value(db, mid, b) if mid and b["rule"] != "manual" and not (b["secret"] and not b["tier"]) else 0
        nxt = tiers[b["tier"]] if b["tier"] < len(tiers) else None
        b["next"] = nxt
        b["next_name"] = TIER_NAMES[b["tier"]] if nxt and b["tiered"] else ""
        b["pct"] = min(100, int(b["value"] * 100 / nxt)) if nxt else 100
    return rows


# ── levels: everyone's rank on the site, from total points ─────────
LEVELS = [(0, "Newcomer"), (20, "Neighbor"), (75, "Regular"), (200, "Insider"), (500, "Local Legend"),
          (1200, "Hall of Famer")]


def level_of(points):
    points = points or 0
    idx = max(i for i, (t, _) in enumerate(LEVELS) if points >= t)
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    lo = LEVELS[idx][0]
    return {"n": idx + 1, "name": LEVELS[idx][1], "next": nxt[1] if nxt else None, "next_at": nxt[0] if nxt else None,
            "to_go": (nxt[0] - points) if nxt else 0,
            "pct": int((points - lo) * 100 / (nxt[0] - lo)) if nxt else 100}


# ── points ─────────────────────────────────────────────
# Two kinds of points. Members see one total; the newsroom sees both.
#   Work: what you do (submissions, comments, scores, live updates).
#   Social: how others respond to you (upvotes, and replies to your stories and comments).
SOCIAL_REASONS = ("upvote", "comment_upvote", "reply")


def kind_of(reason):
    return "social" if reason in SOCIAL_REASONS else "work"


def _recalc(db, mid):
    marks = ",".join("?" * len(SOCIAL_REASONS))
    db.run(f"UPDATE members SET "
           f"points=(SELECT COALESCE(SUM(points),0) FROM points_log WHERE member_id=?), "
           f"social_points=(SELECT COALESCE(SUM(points),0) FROM points_log WHERE member_id=? AND reason IN ({marks})), "
           f"work_points=(SELECT COALESCE(SUM(points),0) FROM points_log WHERE member_id=? AND reason NOT IN ({marks})) "
           f"WHERE id=?", (mid, mid, *SOCIAL_REASONS, mid, *SOCIAL_REASONS, mid))


def recalc_all(db):
    for r in db.q("SELECT DISTINCT member_id FROM points_log"):
        _recalc(db, r["member_id"])


def _today_utc():
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def sync_comment_points(db, c, backfill=False):
    """A visible comment earns its writer Work points (a few a day, so it isn't worth spamming), and earns
    Social points for whoever it answers: the comment it replies to, or the member who wrote the story.
    If the comment is hidden, held or deleted, those points go away again."""
    story = db.one("SELECT member_id, org_id FROM stories WHERE id=?", (c["story_id"],)) or {}
    if story.get("org_id"):
        story = {}  # replies to an organization's story earn no one points
    answered = (db.val("SELECT member_id FROM comments WHERE id=?", (c["parent_id"],)) if c["parent_id"]
                else story.get("member_id"))
    work_ref, reply_ref = f"comment:{c['id']}", f"reply:{c['id']}:by:{c['member_id']}"
    if c["status"] != "visible":
        revoke(db, c["member_id"], "comment", work_ref)
        if answered:
            revoke(db, answered, "reply", reply_ref)
        return
    p = pts(db, "pts_comment")
    if p and not db.val("SELECT 1 FROM points_log WHERE member_id=? AND reason='comment' AND ref=?",
                        (c["member_id"], work_ref)):
        today = db.val("SELECT COALESCE(SUM(points),0) FROM points_log WHERE member_id=? AND reason='comment' "
                       "AND created_at >= ?", (c["member_id"], _today_utc())) or 0
        award(db, c["member_id"], p if backfill or today + p <= pts(db, "pts_comment_daily_cap") else 0, "comment",
              work_ref)
    r = pts(db, "pts_reply")
    if r and answered and answered != c["member_id"] and not db.val(
            "SELECT 1 FROM points_log WHERE member_id=? AND reason='reply' AND ref=?", (answered, reply_ref)):
        given = db.val("SELECT COALESCE(SUM(points),0) FROM points_log WHERE member_id=? AND reason IN "
                       "('upvote','comment_upvote','reply') AND ref LIKE ? AND created_at >= ?",
                       (answered, f"%:by:{c['member_id']}", _today_utc())) or 0
        award(db, answered, r if backfill or given + r <= pts(db, "pts_daily_cap") else 0, "reply", reply_ref)


def award(db, mid, points, reason, ref):
    """Give points once per (member, reason, ref). Returns True if newly recorded."""
    if not mid:
        return False
    cur = db.run("INSERT OR IGNORE INTO points_log(member_id,points,reason,ref,created_at) VALUES(?,?,?,?,?)",
                 (mid, int(points), reason, ref, now()))
    if cur.rowcount:
        _recalc(db, mid)
        if points:
            from . import crowns
            crowns.after_points(db, reason, ref)
    return bool(cur.rowcount)


def revoke(db, mid, reason, ref):
    if mid:
        gone = db.run("DELETE FROM points_log WHERE member_id=? AND reason=? AND ref=?", (mid, reason, ref)).rowcount
        _recalc(db, mid)
        if gone:
            from . import crowns
            crowns.after_points(db, reason, ref)


def pts(db, key):
    try:
        return int(settings.get(db, key) or 0)
    except (TypeError, ValueError):
        return 0


def month_start():
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(
        timespec="seconds")


def leaderboard(db, month=False, limit=50):
    if month:
        return db.q("SELECT m.*, SUM(p.points) AS period FROM points_log p JOIN members m ON m.id=p.member_id "
                    "WHERE p.created_at >= ? AND m.status!='banned' GROUP BY m.id HAVING period > 0 "
                    "ORDER BY period DESC, m.id LIMIT ?", (month_start(), limit))
    return db.q("SELECT *, points AS period FROM members WHERE status!='banned' AND points > 0 "
                "ORDER BY points DESC, id LIMIT ?", (limit,))


# ── what happens when stories change ───────────────────
def on_published(db, story):
    """Credit the member behind a story: points, notice, badges."""
    mid = story.get("member_id")
    if story.get("submission_id"):
        db.run("UPDATE submissions SET status='published', story_id=? WHERE id=?", (story["id"], story["submission_id"]))
    link = f"/story/{story['slug']}" if story.get("slug") else ""
    if story.get("org_id"):
        # posted for a Community Partner: the organization gets the credit (its name and author box), and the
        # person earns no points, badges or crowns for it, so members aren't racing an organization's news
        if mid and story.get("submission_id"):
            told = db.val("SELECT 1 FROM notices WHERE member_id=? AND kind='published' AND link=?", (mid, link))
            if not told:
                notice(db, mid, f"Published for your organization: “{story['headline']}”. Thank you!", link,
                       kind="published")
        return
    if mid:
        if story.get("credit") == "byline":
            new = award(db, mid, pts(db, "pts_article"), "article", f"story:{story['id']}")
        else:
            new = award(db, mid, pts(db, "pts_facts"), "tip", f"story:{story['id']}")
        told = db.val("SELECT 1 FROM notices WHERE member_id=? AND kind='published' AND link=?", (mid, link))
        if new and not told:  # not again when a story is republished
            notice(db, mid, f"Published: “{story['headline']}”. Thank you!", link, kind="published")
        if story.get("featured"):
            on_featured(db, story, quiet=True)
    if story.get("photo_member_id"):
        award(db, story["photo_member_id"], pts(db, "pts_photo"), "photo", f"story:{story['id']}")
    for m in {mid, story.get("photo_member_id")} - {None}:
        check_badges(db, m)


def on_featured(db, story, quiet=False):
    if story.get("org_id"):
        return
    mid = story.get("member_id")
    if mid and story.get("status") == "published":
        if award(db, mid, pts(db, "pts_featured"), "featured", f"story:{story['id']}") and not quiet:
            notice(db, mid, f"Your story “{story['headline']}” is now Featured on the homepage.",
                   f"/story/{story['slug']}", kind="featured")
        check_badges(db, mid)


# ── votes ──────────────────────────────────────────────
def author_of(db, target, tid):
    if target == "story":
        r = db.one("SELECT member_id, photo_member_id FROM stories WHERE id=? AND status='published'", (tid,))
        return (r or {}).get("member_id"), r is not None
    r = db.one("SELECT member_id FROM comments WHERE id=? AND status='visible'", (tid,))
    return (r or {}).get("member_id"), r is not None


def toggle_vote(db, voter, target, tid):
    """Upvote or take it back. Returns (voted_now, new_count) or (None, 0) if not allowed."""
    if target not in ("story", "comment"):
        return None, 0
    author, exists = author_of(db, target, tid)
    if not exists or author == voter["id"]:
        return None, 0
    if target == "story" and db.val("SELECT org_id FROM stories WHERE id=?", (tid,)):
        author = None  # an organization's story: votes count, but earn no one points
    table = "stories" if target == "story" else "comments"
    ref = f"{target}:{tid}:by:{voter['id']}"
    reason = "upvote" if target == "story" else "comment_upvote"
    with db.tx():
        had = db.val("SELECT 1 FROM votes WHERE member_id=? AND target=? AND target_id=?", (voter["id"], target, tid))
        if had:
            db.run("DELETE FROM votes WHERE member_id=? AND target=? AND target_id=?", (voter["id"], target, tid))
        else:
            db.run("INSERT INTO votes(member_id,target,target_id,created_at) VALUES(?,?,?,?)",
                   (voter["id"], target, tid, now()))
        db.run(f"UPDATE {table} SET upvotes=(SELECT COUNT(*) FROM votes WHERE target=? AND target_id=?) WHERE id=?",
               (target, tid, tid))
    if author:
        if had:
            revoke(db, author, reason, ref)
        else:
            day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
                timespec="seconds")
            given = db.val("SELECT COALESCE(SUM(points),0) FROM points_log WHERE member_id=? AND reason IN "
                           "('upvote','comment_upvote','reply') AND ref LIKE ? AND created_at >= ?",
                           (author, f"%:by:{voter['id']}", day)) or 0
            p = pts(db, "pts_upvote" if target == "story" else "pts_comment_upvote")
            award(db, author, p if given + p <= pts(db, "pts_daily_cap") else 0, reason, ref)
            check_badges(db, author)
    return not had, db.val(f"SELECT upvotes FROM {table} WHERE id=?", (tid,))


# ── privileges and status ──────────────────────────────
def level(db, m, key):
    return bool(m) and (m["points"] >= pts(db, key) or bool(m.get("staff_user_id")))


def active(m):
    """Can this member post, vote and submit right now?"""
    if not m or m["status"] == "banned":
        return False
    if m["status"] == "suspended":
        until = util.parse_iso(m.get("suspended_until"))
        return bool(until and until <= datetime.now(timezone.utc))
    return True


def needs_confirm(db, m):
    return bool(m and not m["confirmed"] and settings.get(db, "members_confirm_email") and settings.get(db, "smtp_host")
                and not m.get("staff_user_id"))


def max_photos(db, m):
    return pts(db, "max_photos_plus") if level(db, m, "priv_links") else pts(db, "max_photos")


def hold_reason(db, m, body):
    """Why a new comment should wait for approval, or None to post it straight away."""
    if m.get("staff_user_id"):
        return None
    if m["status"] == "held":
        return "All this member's comments are held"
    low = body.lower()
    for w in settings.get(db, "blocked_words") or []:
        if w.strip() and w.strip().lower() in low:
            return f"Contains a blocked word ({w.strip()})"
    first = pts(db, "hold_first_comments")
    if first and not level(db, m, "priv_unhold"):
        approved = db.val("SELECT COUNT(*) FROM comments WHERE member_id=? AND status='visible'", (m["id"],))
        if approved < first:
            return "New member's first comments"
    return None


def flag_weight(db, m):
    return 2 if level(db, m, "priv_flag_double") else 1


def apply_flag_threshold(db, target, tid):
    """Hide a comment once enough (weighted) flags arrive, for the newsroom to look at."""
    limit = pts(db, "flag_hide")
    if target != "comment" or not limit:
        return False
    total = db.val("SELECT COALESCE(SUM(weight),0) FROM flags WHERE target='comment' AND target_id=? AND status='open'",
                   (tid,))
    if total >= limit:
        db.run("UPDATE comments SET status='held', hold_reason='Flagged by readers' WHERE id=? AND status='visible'",
               (tid,))
        return True
    return False


def week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def touch(db, m):
    """Record a visit (for Weekly Reader) at most once a day."""
    last = util.parse_iso(m.get("last_seen"))
    t = datetime.now(timezone.utc)
    if last and (t - last).total_seconds() < 3600 * 6:
        return
    db.run("UPDATE members SET last_seen=? WHERE id=?", (now(), m["id"]))
    db.run("INSERT OR IGNORE INTO member_visits(member_id, week) VALUES(?,?)", (m["id"], week_key(t)))
    check_badges(db, m["id"])


def recount_comments(db, story_id):
    """Called whenever comments on a story change, so the count and everyone's comment points stay right."""
    db.run("UPDATE stories SET comment_count=(SELECT COUNT(*) FROM comments WHERE story_id=? AND status='visible') "
           "WHERE id=?", (story_id, story_id))
    for c in db.q("SELECT * FROM comments WHERE story_id=?", (story_id,)):
        sync_comment_points(db, c)


def comment_tree(db, story_id, viewer=None, sort="top"):
    """Visible comments (plus the viewer's own held ones) as a nested list."""
    rows = db.q("SELECT c.*, m.username, m.display_name, m.points AS author_points, m.staff_user_id, m.photo "
                "FROM comments c JOIN members m ON m.id=c.member_id WHERE c.story_id=? AND "
                "(c.status IN ('visible','deleted') OR (c.status='held' AND c.member_id=?)) ORDER BY c.id",
                (story_id, viewer["id"] if viewer else -1))
    voted = set()
    if viewer:
        voted = {r["target_id"] for r in db.q("SELECT target_id FROM votes WHERE member_id=? AND target='comment'",
                                              (viewer["id"],))}
    by_parent = {}
    for r in rows:
        r["voted"] = r["id"] in voted
        r["children"] = []
        by_parent.setdefault(r["parent_id"], []).append(r)
    key = (lambda r: (-r["upvotes"], r["id"])) if sort == "top" else (lambda r: -r["id"])

    def build(pid, depth=0):
        kids = sorted(by_parent.get(pid, []), key=key)
        out = []
        for k in kids:
            k["depth"] = depth
            k["children"] = build(k["id"], depth + 1)
            if k["status"] == "deleted" and not k["children"]:
                continue
            out.append(k)
        return out
    return build(None)


def staff_member(db, user):
    return db.one("SELECT * FROM members WHERE staff_user_id=?", (user["id"],)) if user else None


def unread_notices(db, mid):
    return db.q("SELECT * FROM notices WHERE member_id=? AND seen=0 ORDER BY id DESC LIMIT 5", (mid,))


def safe_json(s, d):
    return loads(s, d) if isinstance(s, str) else (s if s is not None else d)


def dumps(o):
    return json.dumps(o)
