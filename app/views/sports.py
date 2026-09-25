"""Public sports pages: the scoreboard, school and team pages, and each game, where any member can post or fix
the score. Members can also paste a schedule and let the AI sort it out."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import community, settings, sports, util
from ..db import now
from ..security import rate_limited
from .members import can_act

bp = Blueprint("sports", __name__)


def _public_ctx():
    from .public import public_ctx
    return public_ctx()


bp.context_processor(_public_ctx)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def day_bounds(db, offset_days=0):
    start = datetime.now(util.tz(db)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
    return _iso(start), _iso(start + timedelta(days=1))


def games(db, where, args=(), order="g.starts_at", limit=300):
    rows = sports.decorate(db, db.q(f"{sports.GAME_SQL} WHERE {where} ORDER BY {order} LIMIT ?", (*args, limit)))
    vids = sports.video_counts(db, [r["id"] for r in rows])
    for r in rows:
        r["video"] = vids.get(r["id"])
    return rows


def sports_stories(limit=6, like=None):
    from .public import published
    if like:
        return published("category='Sports' AND (headline LIKE ? OR summary LIKE ?)", (f"%{like}%", f"%{like}%"),
                         limit=limit)
    return published("category='Sports'", limit=limit)


@bp.route("/sports")
def scoreboard():
    db = g.db
    sport = request.args.get("sport", "")[:40]
    school = db.one("SELECT * FROM sports_schools WHERE slug=?", (request.args.get("school", ""),))
    where, args = ["1=1"], []
    if sport:
        where.append("t.sport=?")
        args.append(sport)
    if school:
        where.append("t.school_id=?")
        args.append(school["id"])
    w = " AND ".join(where)
    t0, t1 = day_bounds(db)
    week_ago, week_on = day_bounds(db, -7)[0], day_bounds(db, 8)[0]
    today = sports.dedupe(db, games(db, f"{w} AND g.starts_at >= ? AND g.starts_at < ?", (*args, t0, t1)))
    results = sports.dedupe(db, games(db, f"{w} AND g.status='final' AND g.starts_at >= ? AND g.starts_at < ?",
                                      (*args, week_ago, t0), order="g.starts_at DESC", limit=60))
    coming = sports.dedupe(db, games(db, f"{w} AND g.status IN ('scheduled','postponed') AND g.starts_at >= ? "
                                        "AND g.starts_at < ?", (*args, t1, week_on), limit=120))
    schools = db.q("SELECT s.*, (SELECT COUNT(*) FROM sports_teams t WHERE t.school_id=s.id) AS teams "
                   "FROM sports_schools s ORDER BY s.level='College' DESC, s.name COLLATE NOCASE")
    used = [r["sport"] for r in db.q("SELECT DISTINCT t.sport FROM sports_teams t JOIN games g ON g.team_id=t.id "
                                     "ORDER BY t.sport")]
    return render_template("public/sports.html", today=today, results=results, coming=coming, schools=schools,
                           sports_used=used, sport=sport, school=school, stories=sports_stories(),
                           tab="sports")


@bp.route("/sports/<slug>")
def school_page(slug):
    db = g.db
    s = db.one("SELECT * FROM sports_schools WHERE slug=?", (slug,)) or abort(404)
    teams = db.q(f"{sports.TEAM_SQL} WHERE t.school_id=? AND t.active=1 ORDER BY t.sport, t.gender, t.level",
                 (s["id"],))
    for t in teams:
        t["record"] = sports.record(db, t["id"])
        t["label"] = sports.team_label(t, with_school=False)
    week_ago, week_on = day_bounds(db, -7)[0], day_bounds(db, 8)[0]
    t0 = day_bounds(db)[0]
    recent = games(db, "t.school_id=? AND g.status='final' AND g.starts_at >= ? AND g.starts_at < ?",
                   (s["id"], week_ago, t0), order="g.starts_at DESC", limit=40)
    coming = games(db, "t.school_id=? AND g.status IN ('scheduled','live','postponed') AND g.starts_at >= ? "
                       "AND g.starts_at < ?", (s["id"], t0, week_on), limit=60)
    groups = [(lvl, [t for t in teams if t["level"] == lvl]) for lvl in sports.LEVELS]
    groups += [("Other", [t for t in teams if t["level"] not in sports.LEVELS])]
    return render_template("public/sports_school.html", s=s, teams=teams, groups=[x for x in groups if x[1]],
                           recent=recent, coming=coming,
                           stories=sports_stories(5, like=s["short"] or s["name"]), tab="sports")


@bp.route("/sports/team/<slug>", methods=["GET", "POST"])
def team_page(slug):
    db = g.db
    t = db.one(f"{sports.TEAM_SQL} WHERE t.slug=?", (slug,)) or abort(404)
    t["label"] = sports.team_label(t)
    error = None
    if request.method == "POST" and request.form.get("action") == "info":  # the "Good to know" box
        error = can_act() or ("The newsroom looks after this box for this team." if t["info_locked"] else None)
        if not error and rate_limited(db, f"team-info:{g.member['id']}", 20, 86400):
            error = "That's a lot of edits for one day. Try again tomorrow."
        if not error:
            sports.save_info(db, t, request.form.get("info", ""), member=g.member)
            flash("Saved. Thanks for keeping it up to date!")
            return redirect(url_for(".team_page", slug=slug) + "#info")
    elif request.method == "POST":  # a member adds one game that's missing
        error = can_act()
        f = request.form
        if not error and rate_limited(db, f"game-add:{g.member['id']}", 30, 86400):
            error = "That's a lot of games for one day. Try again tomorrow, or ask the newsroom."
        if not error:
            rows = [{"date": f.get("date", ""), "time": f.get("time", "")[:5], "opponent": util.text_only(f.get("opponent", ""), 120),
                     "home": f.get("home") if f.get("home") in ("home", "away", "neutral") else "home",
                     "location": util.text_only(f.get("location", ""), 160), "note": util.text_only(f.get("note", ""), 160)}]
            if not rows[0]["opponent"] or not sports.when_utc(db, rows[0]["date"], "")[0]:
                error = "Give the date and the opponent."
            else:
                added, updated = sports.save_rows(db, t["school_id"], rows, team_id=t["id"],
                                                  member_id=g.member["id"], source="member-one")
                flash("Game added. Thanks!" if added else "That game was already on the schedule; we updated it.")
                return redirect(url_for(".team_page", slug=slug))
    rows = games(db, "g.team_id=?", (t["id"],), limit=200)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    upcoming = [r for r in rows if r["live"] != "over" and r["status"] in ("scheduled", "live")][:3]
    info_by = db.val("SELECT username FROM members WHERE id=?", (t["info_by"],)) if t["info_by"] else None
    from .. import crowns
    fans = crowns.team_scores(db, t["id"], 5)
    for f_ in fans:
        f_["username"] = db.val("SELECT username FROM members WHERE id=?", (f_["member_id"],))
    king = db.one("SELECT c.*, m.username FROM crowns c LEFT JOIN members m ON m.id=c.member_id WHERE c.kind='team' "
                  "AND c.key=?", (str(t["id"]),))
    return render_template("public/sports_team.html", t=t, rows=rows, record=sports.record(db, t["id"]),
                           upcoming=upcoming, info_by=info_by, info_max=sports.INFO_MAX, now_iso=now_iso,
                           fans=fans, king=king if king and king["member_id"] else None,
                           season=crowns.season(db)[0], crown_min=crowns.MIN_SCORE,
                           error=error, f=request.form, tab="sports")


@bp.route("/sports/game/<int:gid>", methods=["GET", "POST"])
def game(gid):
    db = g.db
    rows = games(db, "g.id=?", (gid,))
    if not rows:
        abort(404)
    gm = rows[0]
    error = None
    if request.method == "POST" and request.form.get("action") in ("update", "video"):
        return _game_night(gm)
    if request.method == "POST":
        error = can_act()
        a = request.form.get("action")
        if not error and gm["locked"]:
            error = "The newsroom has locked this game. If something's wrong, let us know on the contact page."
        if not error and a == "confirm":
            if gm["has_score"] and gm["reported_by"] != g.member["id"]:
                sports.confirm(db, gm, g.member)
                flash("Thanks for confirming!")
            return redirect(url_for(".game", gid=gid))
        if not error and a == "report":
            f = request.form
            status = f.get("status") if f.get("status") in sports.STATUSES else "final"
            ours, theirs = f.get("our_score", "").strip(), f.get("their_score", "").strip()
            if status in ("final", "live") and not (ours.isdigit() and theirs.isdigit()):
                error = "Enter both scores as numbers."
            elif rate_limited(db, f"score:{g.member['id']}", 30, 86400):
                error = "You've posted a lot of scores today. Try again tomorrow."
            else:
                sports.report(db, gm, g.member, int(ours) if ours.isdigit() else None,
                              int(theirs) if theirs.isdigit() else None, status,
                              util.text_only(f.get("detail", ""), 40))
                flash("Score posted. Thanks for keeping everyone up to date!")
                return redirect(url_for(".game", gid=gid))
    log = db.q("SELECT l.*, m.username FROM game_log l LEFT JOIN members m ON m.id=l.member_id WHERE l.game_id=? "
               "ORDER BY l.id DESC LIMIT 20", (gid,))
    confirms = db.q("SELECT c.*, m.username FROM game_confirms c JOIN members m ON m.id=c.member_id WHERE c.game_id=?",
                    (gid,))
    mine = g.get("member") and any(c["member_id"] == g.member["id"] for c in confirms)
    feed = sports.updates(db, gid)
    from .. import callit
    from .public import callit_view
    call = callit_view(callit.for_game(db, gm, g.get("member")))
    return render_template("public/sports_game.html", gm=gm, log=log, confirms=confirms, mine=mine, error=error,
                           statuses=sports.STATUSES, feed=feed, videos=sports.videos(db, gid), callit=call,
                           pts_stream=community.pts(db, "pts_stream"), pts_video=community.pts(db, "pts_video"),
                           has_stream=db.val("SELECT 1 FROM game_videos WHERE game_id=? AND kind='stream' AND "
                                             "status='visible'", (gid,)), tab="sports")


def _game_night(gm):
    """A live update for the feed, or a live stream / video link."""
    db, f = g.db, request.form
    here = url_for(".game", gid=gm["id"])
    problem = can_act()
    if problem:
        flash(problem, "error")
        return redirect(here)
    if f.get("action") == "update":
        body = util.text_only(f.get("body", ""), 280)
        ours, theirs = f.get("our_score", "").strip(), f.get("their_score", "").strip()
        if gm["live"] != "live":
            flash("The game feed opens 30 minutes before the start.", "error")
        elif len(body) < 2 and not (ours.isdigit() and theirs.isdigit()):
            flash("Write what happened, or add the score.", "error")
        elif rate_limited(db, f"live-fast:{g.member['id']}", 1, 15):
            flash("Easy, coach! Wait a few seconds between updates.", "error")
        elif rate_limited(db, f"live:{g.member['id']}", 80, 86400):
            flash("That's a lot of updates for one day.", "error")
        else:
            both = ours.isdigit() and theirs.isdigit() and not gm["locked"]
            sports.post_update(db, gm, g.member, body or f"Score update: {ours}–{theirs}",
                               int(ours) if both else None, int(theirs) if both else None)
        return redirect(here + "#feed")
    try:
        if rate_limited(db, f"video:{g.member['id']}", 10, 86400):
            raise ValueError("That's a lot of videos for one day.")
        _, first = sports.add_video(db, gm, g.member, f.get("url", ""), f.get("kind", "video"))
        flash(f"Thanks! Your stream is on the page, and you earned {community.pts(db, 'pts_stream')} points."
              if first else "Thanks! Your video is on the page.")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(here + "#video")


@bp.route("/sports/game/<int:gid>/feed.json")
def game_feed(gid):
    """What's new since the last check, so the live page updates itself."""
    db = g.db
    rows = games(db, "g.id=?", (gid,))
    if not rows:
        abort(404)
    gm = rows[0]
    after = request.args.get("after", 0, type=int)
    new = [{"id": u["id"], "who": u["username"] or "member", "body": u["body"], "ago": util.ago(u["created_at"]),
            "score": f"{u['our_score']}–{u['their_score']}" if u["our_score"] is not None else ""}
           for u in sports.updates(db, gid, after=after)]
    return jsonify(live=gm["live"], status=gm["status_label"], detail=gm["detail"] or "",
                   ours=gm["our_score"], theirs=gm["their_score"], updates=new)


# ── pasting a schedule (members and the newsroom share this) ──
def paste_flow(member=None, user_id=None):
    """Returns (context, redirect_or_None). Step 1: paste. Step 2: check the table. Step 3: saved."""
    db = g.db
    f = request.form
    ctx = {"schools": db.q("SELECT * FROM sports_schools ORDER BY name COLLATE NOCASE"), "rows": None, "error": None,
           "f": f, "sports_list": sports.SPORTS, "genders": sports.GENDERS, "levels": sports.LEVELS,
           "school_levels": sports.SCHOOL_LEVELS, "teams": []}
    ctx["pick_school"] = request.values.get("school_id", type=int)
    ctx["pick_team"] = request.values.get("team_id", type=int)
    ctx["teams"] = db.q(f"{sports.TEAM_SQL} WHERE t.active=1 ORDER BY s.name COLLATE NOCASE, t.sport, t.gender, t.level")
    for t in ctx["teams"]:
        t["label"] = sports.team_label(t, with_school=False)
    if request.method != "POST":
        return ctx, None
    a = f.get("action")
    try:
        team = db.one(f"{sports.TEAM_SQL} WHERE t.id=?", (f.get("team_id", type=int),))
        school_id = team["school_id"] if team else f.get("school_id", type=int)
        if not school_id and f.get("new_school", "").strip():
            school_id = sports.add_school(db, f.get("new_school"), town=f.get("new_town", ""),
                                          level=f.get("new_level", "High school"))
        school = db.one("SELECT * FROM sports_schools WHERE id=?", (school_id,)) if school_id else None
        if not school:
            raise ValueError("Pick the school, or type a new one.")
        if team:
            team["label"] = sports.team_label(team)
        ctx.update(school=school, team=team)
        if a == "read":
            text = f.get("text", "").strip()
            if len(text) < 10:
                raise ValueError("Paste the schedule into the box first.")
            if member and rate_limited(db, f"schedule:{member['id']}", 8, 86400):
                raise ValueError("That's a lot of schedules for one day. Try again tomorrow.")
            rows = sports.parse_schedule(db, text, school, team, f.get("season", ""))
            if not rows:
                raise ValueError("We couldn't find any games in that. Try copying just the schedule part.")
            ctx["rows"] = rows
        elif a == "save":
            rows = sports.rows_from_form(f)
            if not rows:
                raise ValueError("Tick at least one game to add.")
            added, updated = sports.save_rows(db, school["id"], rows, team_id=team["id"] if team else None,
                                              user_id=user_id, member_id=member["id"] if member else None,
                                              source="member" if member else "import")
            flash(f"Added {added} game{'s' if added != 1 else ''}" +
                  (f" and updated {updated} already on the schedule." if updated else ".") + " Thanks!")
            return ctx, school
    except ValueError as e:
        ctx["error"] = str(e)
        if a == "save":
            ctx["rows"] = sports.rows_from_form(f) or None
    except Exception as e:  # the AI or the network hiccuped
        from .. import sources
        ctx["error"] = sources.friendly_error(e)
    return ctx, None


@bp.route("/sports/add", methods=["GET", "POST"])
def add_schedule():
    reason = can_act()
    if reason:
        flash(reason)
        return redirect(url_for("members.login", next=request.full_path))
    ctx, school = paste_flow(member=g.member)
    if school:
        return redirect(url_for(".school_page", slug=school["slug"]))
    return render_template("public/sports_add.html", tab="sports", **ctx)
