"""The Content page: one place to find any story or event, filtered every way you'd want.
Registered on the admin blueprint."""
from datetime import datetime, timedelta, timezone

from flask import g, render_template, request

from .. import settings, util
from ..db import now
from ..security import login_required
from .admin import bp

STATUSES = [("", "All"), ("developing", "In development"), ("draft", "Drafts"), ("scheduled", "Scheduled"),
            ("published", "Published"), ("rejected", "Rejected")]
ORIGINS = [("", "Anywhere"), ("member", "Members"), ("partner", "Community partners"), ("tip", "Reader tips"),
           ("manual", "Manual add"), ("source", "Sources"), ("wire", "News wire"), ("staff", "Newsroom")]
# How we tell where a story came from, as SQL on stories st / sources so
ORIGIN_SQL = {
    "partner": "st.org_id IS NOT NULL",
    "member": "st.member_id IS NOT NULL AND st.org_id IS NULL",
    "tip": "st.tip_id IS NOT NULL AND st.member_id IS NULL",
    "wire": "st.wire=1",
    "manual": "so.type='manual'",
    "source": "st.source_id IS NOT NULL AND so.builtin=0 AND st.wire=0",
    "staff": "st.source_id IS NULL AND st.member_id IS NULL AND st.org_id IS NULL AND st.tip_id IS NULL",
}
SORTS = {"updated": ("Recently changed", "st.updated_at DESC"),
         "published": ("Newest published", "COALESCE(st.published_at, st.publish_at, st.created_at) DESC"),
         "oldest": ("Oldest first", "st.created_at"),
         "upvotes": ("Most upvoted", "st.upvotes DESC, st.published_at DESC"),
         "comments": ("Most comments", "st.comment_count DESC, st.published_at DESC")}
PER_PAGE = 50


def origin_of(r):
    if r["org_id"]:
        return "Partner" + (f": {r['org_name']}" if r.get("org_name") else "")
    if r["member_id"]:
        return f"@{r['username']}" if r.get("username") else "Member"
    if r["tip_id"]:
        return "Reader tip"
    if r["wire"]:
        return "News wire"
    if r.get("source_type") == "manual":
        return "Manual add"
    if r["source_id"]:
        return r.get("source_name") or "Source"
    return "Newsroom"


def _day_bound(db, value, end=False):
    try:
        d = datetime.fromisoformat(value.strip()[:10]).replace(tzinfo=util.tz(db))
    except (ValueError, AttributeError):
        return None
    if end:
        d += timedelta(days=1)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


@bp.route("/content")
@login_required()
def content():
    db = g.db
    a = request.args
    f = {k: a.get(k, "").strip()[:100] for k in ("q", "status", "cat", "origin", "author", "from", "to", "kind", "sort",
                                                 "sub")}
    if f["status"] not in dict(STATUSES):
        f["status"] = ""
    if f["origin"] not in dict(ORIGINS):
        f["origin"] = ""
    if f["sort"] not in SORTS:
        f["sort"] = "updated"
    where, args = ["1=1"], []
    if f["q"]:
        where.append("(st.headline LIKE ? OR st.summary LIKE ? OR st.body LIKE ?)")
        args += [f"%{f['q']}%"] * 3
    if f["cat"]:
        where.append("st.category=?")
        args.append(f["cat"])
    if f["sub"]:
        where.append("st.subcategory=?")
        args.append(f["sub"])
    if f["origin"]:
        where.append(ORIGIN_SQL[f["origin"]])
    if f["author"]:
        where.append("(m.username LIKE ? OR m.display_name LIKE ? OR st.byline LIKE ? OR o.name LIKE ?)")
        args += [f"%{f['author'].lstrip('@')}%"] * 4
    if f["kind"] in ("story", "event"):
        where.append("st.kind=?")
        args.append(f["kind"])
    for key, end in (("from", False), ("to", True)):
        bound = _day_bound(db, f[key], end) if f[key] else None
        if bound:
            where.append(f"COALESCE(st.published_at, st.created_at) {'<' if end else '>='} ?")
            args.append(bound)
    base = ("FROM stories st LEFT JOIN sources so ON so.id=st.source_id LEFT JOIN members m ON m.id=st.member_id "
            "LEFT JOIN orgs o ON o.id=st.org_id WHERE " + " AND ".join(where))
    counts = {r["status"]: r["n"] for r in db.q(f"SELECT st.status, COUNT(*) AS n {base} GROUP BY st.status", args)}
    counts[""] = sum(counts.values())
    if f["status"]:
        base += " AND st.status=?"
        args.append(f["status"])
    page = max(1, a.get("page", 1, type=int))
    rows = db.q(f"SELECT st.id, st.slug, st.headline, st.status, st.category, st.kind, st.source_id, st.member_id, "
                f"st.org_id, st.tip_id, st.wire, st.subcategory, st.byline, st.upvotes, st.comment_count, st.featured, st.created_at, "
                f"st.updated_at, st.published_at, st.publish_at, so.name AS source_name, so.type AS source_type, "
                f"m.username, o.name AS org_name {base} ORDER BY {SORTS[f['sort']][1]} LIMIT ? OFFSET ?",
                (*args, PER_PAGE + 1, (page - 1) * PER_PAGE))
    for r in rows:
        r["origin"] = origin_of(r)
    filtered = any(f[k] for k in ("q", "cat", "origin", "author", "from", "to", "kind", "sub"))
    return render_template("admin/content.html", rows=rows[:PER_PAGE], has_more=len(rows) > PER_PAGE, page=page,
                           f=f, counts=counts, statuses=STATUSES, origins=ORIGINS, sorts=SORTS, filtered=filtered,
                           categories=settings.get(db, "categories") or [], subcats=settings.subcategories(db))


@bp.route("/callit")
@login_required()
def callit_admin():
    """Every Call It: the ones waiting for the real answer first."""
    from .. import callit
    db = g.db
    rows = db.q("SELECT p.*, s.headline, s.slug, (SELECT COUNT(*) FROM guesses WHERE prediction_id=p.id) AS n "
                "FROM predictions p JOIN stories s ON s.id=p.story_id WHERE p.status!='void' "
                "ORDER BY p.status='open' AND p.closes_at <= ? DESC, p.status='open' DESC, p.id DESC LIMIT 100",
                (now(),))
    for r in rows:
        callit.decorate(db, r)
    games = db.q("SELECT COUNT(DISTINCT p.id) AS games, COUNT(gu.id) AS guesses FROM predictions p "
                 "LEFT JOIN guesses gu ON gu.prediction_id=p.id WHERE p.kind='score'")
    return render_template("admin/callit.html", rows=rows, games=games[0] if games else {})
