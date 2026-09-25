"""Newsroom sports pages: schools, pasting schedules, and keeping an eye on members' scores.
Registered on the admin blueprint."""
from flask import abort, flash, g, redirect, render_template, request, url_for

from .. import settings, sports, util
from ..db import now
from ..security import login_required
from .admin import bp, uid

TABS = [("changes", "Latest scores"), ("schools", "Schools & teams"), ("games", "Games"),
        ("subcats", "Story subcategories")]


@bp.route("/sports", methods=["GET", "POST"])
@login_required()
def sports_admin():
    db = g.db
    tab = request.args.get("tab") if request.args.get("tab") in dict(TABS) else "changes"
    if request.method == "POST":
        f = request.form
        a, _, arg = (f.get("action") or "").partition(":")
        i = f.get("id", type=int) or (int(arg) if arg.isdigit() else None)
        if a == "add_school":
            try:
                sports.add_school(db, f.get("name", ""), f.get("short", ""), f.get("mascot", ""),
                                  f.get("level", ""), f.get("town", ""), f.get("color", ""))
                flash("School added.")
            except ValueError as e:
                flash(str(e), "error")
        elif a == "edit_school":
            s = db.one("SELECT * FROM sports_schools WHERE id=?", (i,)) or abort(404)
            color = f.get("color", "")
            db.update("sports_schools", s["id"], name=util.text_only(f.get("name", ""), 120) or s["name"],
                      short=util.text_only(f.get("short", ""), 40), mascot=util.text_only(f.get("mascot", ""), 60),
                      town=util.text_only(f.get("town", ""), 80),
                      level=f.get("level") if f.get("level") in sports.SCHOOL_LEVELS else s["level"],
                      color=color if len(color) == 7 and color.startswith("#") else s["color"])
            flash("Saved.")
        elif a == "delete_school":
            s = db.one("SELECT * FROM sports_schools WHERE id=?", (i,)) or abort(404)
            with db.tx():
                for t in db.q("SELECT id FROM sports_teams WHERE school_id=?", (i,)):
                    _delete_team(db, t["id"])
                db.run("DELETE FROM sports_schools WHERE id=?", (i,))
            flash(f"Removed {s['name']} and its games.")
        elif a in ("hide_team", "show_team"):
            db.run("UPDATE sports_teams SET active=? WHERE id=?", (1 if a == "show_team" else 0, i))
        elif a == "save_subcats":
            subs = settings.get(db, "subcategories") or {}
            names = []
            for x in f.getlist("subcats") + f.get("subcats_new", "").split(","):
                x = util.text_only(x, 40)
                if x and x not in names:
                    names.append(x)
            settings.put(db, "subcategories", {**subs, "Sports": names})
            flash("Subcategories saved.")
        elif a == "team_info":
            db.run("UPDATE sports_teams SET info=?, info_at=?, info_by=NULL, info_locked=? WHERE id=?",
                   (f.get("info", "").strip()[:sports.INFO_MAX], now(), 1 if f.get("info_locked") else 0, i))
            flash("Saved.")
        elif a == "delete_team":
            with db.tx():
                _delete_team(db, i)
            flash("Team and its games removed.")
        elif a == "revert":
            flash("Undone." if sports.revert(db, i, uid()) else "Nothing to undo.")
        elif a in ("lock", "unlock"):
            db.run("UPDATE games SET locked=? WHERE id=?", (1 if a == "lock" else 0, i))
            sports._settle(db, i)
        return redirect(request.full_path)
    ctx = {}
    if tab == "changes":
        ctx["rows"] = sports.decorate(db, db.q(
            f"{sports.GAME_SQL} WHERE g.reported_by IS NOT NULL ORDER BY g.reported_at DESC LIMIT 100"))
        for r in ctx["rows"]:
            r["confirms"] = db.val("SELECT COUNT(*) FROM game_confirms WHERE game_id=?", (r["id"],))
            r["changes"] = db.val("SELECT COUNT(*) FROM game_log WHERE game_id=? AND before!='{}'", (r["id"],))
    elif tab == "schools":
        ctx["schools"] = db.q("SELECT * FROM sports_schools ORDER BY name COLLATE NOCASE")
        for s in ctx["schools"]:
            s["teams"] = db.q("SELECT t.*, (SELECT COUNT(*) FROM games WHERE team_id=t.id) AS games FROM sports_teams t "
                              "WHERE school_id=? ORDER BY sport, gender, level", (s["id"],))
            for t in s["teams"]:
                t["label"] = sports.team_label(t, with_school=False)
                t["record"] = sports.record(db, t["id"])
    elif tab == "subcats":
        ctx["subcats"] = settings.subcategories(db, "Sports")
        ctx["counts"] = {r["subcategory"]: r["n"] for r in db.q(
            "SELECT subcategory, COUNT(*) AS n FROM stories WHERE category='Sports' GROUP BY subcategory")}
    else:
        school = request.args.get("school", type=int)
        where, args = "1=1", ()
        if school:
            where, args = "t.school_id=?", (school,)
        ctx["rows"] = sports.decorate(db, db.q(f"{sports.GAME_SQL} WHERE {where} ORDER BY g.starts_at DESC LIMIT 400",
                                               args))
        ctx["schools"] = db.q("SELECT id, name FROM sports_schools ORDER BY name COLLATE NOCASE")
        ctx["school"] = school
    return render_template("admin/sports.html", tab=tab, tabs=TABS, levels=sports.SCHOOL_LEVELS, **ctx)


def _delete_team(db, team_id):
    ids = [r["id"] for r in db.q("SELECT id FROM games WHERE team_id=?", (team_id,))]
    for gid in ids:
        db.run("DELETE FROM game_log WHERE game_id=?", (gid,))
        db.run("DELETE FROM game_confirms WHERE game_id=?", (gid,))
    db.run("DELETE FROM games WHERE team_id=?", (team_id,))
    db.run("DELETE FROM sports_teams WHERE id=?", (team_id,))


@bp.route("/sports/import", methods=["GET", "POST"])
@login_required()
def sports_import():
    from .sports import paste_flow
    ctx, school = paste_flow(user_id=uid())
    if school:
        return redirect(url_for(".sports_admin", tab="games", school=school["id"]))
    return render_template("admin/sports_import.html", **ctx)


@bp.route("/sports/game/<int:gid>", methods=["GET", "POST"])
@login_required()
def sports_game(gid):
    db = g.db
    gm = db.one(f"{sports.GAME_SQL} WHERE g.id=?", (gid,)) or abort(404)
    if request.method == "POST":
        f = request.form
        a = f.get("action")
        if a == "delete":
            with db.tx():
                db.run("DELETE FROM game_log WHERE game_id=?", (gid,))
                db.run("DELETE FROM game_confirms WHERE game_id=?", (gid,))
                db.run("DELETE FROM games WHERE id=?", (gid,))
            flash("Game removed.")
            return redirect(url_for(".sports_admin", tab="games"))
        if a == "revert":
            flash("Undone." if sports.revert(db, gid, uid()) else "Nothing to undo.")
            return redirect(url_for(".sports_game", gid=gid))
        if a == "hide_update":
            sports.hide_update(db, f.get("item", type=int))
            flash("Update hidden.")
            return redirect(url_for(".sports_game", gid=gid) + "#feed")
        if a == "hide_video":
            sports.hide_video(db, f.get("item", type=int))
            flash("Video removed.")
            return redirect(url_for(".sports_game", gid=gid) + "#videos")
        if a == "add_video":
            url = f.get("url", "").strip()[:500]
            if util.video_embed(url):
                db.insert("game_videos", game_id=gid, user_id=uid(), url=url,
                          kind="stream" if f.get("kind") == "stream" else "video", created_at=now())
                flash("Video added.")
            else:
                flash("That isn't a YouTube or Facebook video link.", "error")
            return redirect(url_for(".sports_game", gid=gid) + "#videos")
        starts, tbd = sports.when_utc(db, f.get("date", ""), f.get("time", ""))
        if not starts:
            flash("Give the game a date.", "error")
            return redirect(url_for(".sports_game", gid=gid))
        ours, theirs = f.get("our_score", "").strip(), f.get("their_score", "").strip()
        status = f.get("status") if f.get("status") in sports.STATUSES else gm["status"]
        before = {k: gm[k] for k in ("our_score", "their_score", "status", "detail", "reported_by")}
        db.update("games", gid, opponent=util.text_only(f.get("opponent", ""), 120) or gm["opponent"],
                  home=f.get("home") if f.get("home") in ("home", "away", "neutral") else gm["home"],
                  starts_at=starts, time_tbd=1 if tbd else 0, location=util.text_only(f.get("location", ""), 160),
                  note=util.text_only(f.get("note", ""), 160), status=status,
                  our_score=int(ours) if ours.isdigit() else None, their_score=int(theirs) if theirs.isdigit() else None,
                  detail=util.text_only(f.get("detail", ""), 40), locked=1 if f.get("locked") else 0, updated_at=now())
        import json
        db.insert("game_log", game_id=gid, user_id=uid(), change="edited by the newsroom", before=json.dumps(before),
                  created_at=now())
        sports._settle(db, gid)
        flash("Saved.")
        return redirect(url_for(".sports_game", gid=gid))
    gm = sports.decorate(db, [gm])[0]
    gm["date"], gm["time"] = sports.local_parts(db, gm["starts_at"])
    log = db.q("SELECT l.*, m.username, u.name AS staff FROM game_log l LEFT JOIN members m ON m.id=l.member_id "
               "LEFT JOIN users u ON u.id=l.user_id WHERE l.game_id=? ORDER BY l.id DESC", (gid,))
    return render_template("admin/sports_game.html", gm=gm, log=log, statuses=sports.STATUSES,
                           feed=sports.updates(db, gid, include_hidden=True),
                           videos=db.q("SELECT v.*, m.username FROM game_videos v LEFT JOIN members m ON "
                                       "m.id=v.member_id WHERE v.game_id=? ORDER BY v.id DESC", (gid,)))
