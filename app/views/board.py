"""Community Board pages: the board, posting, a single post with replies, and the poster's own controls."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import board, community, util
from ..db import now
from ..security import rate_limited
from .members import can_act

bp = Blueprint("board", __name__)


@bp.context_processor
def _public_ctx():
    from .public import public_ctx
    return public_ctx()


@bp.route("/board")
def board_page():
    db = g.db
    kind = request.args.get("kind", "")
    return render_template("public/board.html", posts=board.listing(db, kind or None), kind=kind, kinds=board.KINDS,
                           mine=board.mine(db, g.member["id"]) if g.get("member") else [], tab="board")


@bp.route("/board/new", methods=["GET", "POST"])
def board_new():
    db = g.db
    if not g.get("member"):
        return redirect(url_for("members.login", next=request.path))
    f = request.form
    error = None
    if request.method == "POST":
        kind = f.get("kind", "")
        title = util.text_only(f.get("title", ""), 120)
        body = util.text_only(f.get("body", ""), 3000)
        phone = util.text_only(f.get("phone", ""), 30) if f.get("show_phone") else ""
        upload = request.files.get("photo")
        error = can_act()
        if not error and kind not in board.KIND:
            error = "Pick what kind of post this is."
        elif not error and len(title) < 4:
            error = "Give your post a short title."
        elif not error and f.get("show_phone") and len(phone) < 7:
            error = "Add the phone number to show, or untick that box."
        elif not error and rate_limited(db, f"board:{g.member['id']}", board.MAX_PER_DAY, 86400):
            error = "That's the most posts for one day. Try again tomorrow."
        if not error:
            photo = None
            if upload and upload.filename:
                photo = util.save_image(upload)
                if not photo:
                    error = "That photo didn't work. Try a JPEG or PNG under 12 MB."
            if not error:
                pid = board.create(db, g.member, kind, title, body, town=util.text_only(f.get("town", ""), 60),
                                   when_text=util.text_only(f.get("when_text", ""), 80), photo=photo, phone=phone)
                flash("Your post is up. It comes down on its own in 14 days.")
                return redirect(url_for(".board_post", pid=pid))
    return render_template("public/board_new.html", f=f, error=error, kinds=board.KINDS, towns=board.towns(db),
                           kind=f.get("kind") or request.args.get("kind", ""), tab="board")


@bp.route("/board/<int:pid>", methods=["GET", "POST"])
def board_post(pid):
    db = g.db
    p = board.get(db, pid) or abort(404)
    staff = bool(g.get("user"))
    owner = bool(g.get("member") and g.member["id"] == p["member_id"])
    if p["status"] in ("held", "removed") and not (staff or owner):
        abort(404)
    if not p["live"] and not (staff or owner):
        return render_template("public/board_gone.html", p=p, tab="board"), 410
    if request.method == "POST":  # a reply
        problem = can_act()
        body = util.text_only(request.form.get("body", ""), 2000)
        if problem:
            flash(problem, "error")
        elif not p["live"] or p["status"] != "open":
            flash("This post isn't taking replies any more.", "error")
        elif len(body) < 2:
            flash("Write a reply first.", "error")
        elif rate_limited(db, f"breply:{g.member['id']}", 30, 3600):
            flash("Slow down a little.", "error")
        else:
            reason = community.hold_reason(db, g.member, body)
            rid = db.insert("board_replies", post_id=pid, member_id=g.member["id"], body=body,
                            status="held" if reason else "visible", hold_reason=reason, created_at=now())
            board.recount(db, pid)
            if reason:
                flash("Thanks! Your reply will show once the newsroom has a quick look.")
            elif p["member_id"] != g.member["id"]:
                community.notice(db, p["member_id"], f"@{g.member['username']} replied to your board post "
                                                     f"“{p['title'][:80]}”.", f"/board/{pid}#r{rid}", kind="reply")
            return redirect(url_for(".board_post", pid=pid) + (f"#r{rid}" if not reason else ""))
    replies = db.q("SELECT r.*, m.username, m.staff_user_id FROM board_replies r JOIN members m ON m.id=r.member_id "
                   "WHERE r.post_id=? AND (r.status='visible' OR (r.status='held' AND r.member_id=?)) ORDER BY r.id",
                   (pid, g.member["id"] if g.get("member") else 0))
    voted = set()
    if g.get("member") and replies:
        voted = {r["target_id"] for r in db.q("SELECT target_id FROM votes WHERE member_id=? AND target='breply'",
                                              (g.member["id"],))}
    return render_template("public/board_post.html", p=p, replies=replies, owner=owner, staff=staff, voted=voted,
                           tab="board")


@bp.route("/board/<int:pid>/manage", methods=["POST"])
def board_manage(pid):
    """The poster: mark done, renew, take it down. Staff: remove or put back."""
    db = g.db
    p = board.get(db, pid) or abort(404)
    a = request.form.get("action")
    owner = bool(g.get("member") and g.member["id"] == p["member_id"])
    if g.get("user") and a in ("remove", "restore"):
        db.update("board_posts", pid, status="removed" if a == "remove" else "open", updated_at=now())
        db.run("UPDATE flags SET status='resolved', outcome=? WHERE target='board' AND target_id=? AND status='open'",
               ("removed" if a == "remove" else "kept", pid))
        util.activity(db, g.user["id"], f"board {a}", f"board:{pid}", p["title"])
        flash("Post removed from the board." if a == "remove" else "Post is back on the board.")
        return redirect(url_for(".board_page") if a == "remove" else url_for(".board_post", pid=pid))
    if not owner:
        abort(403)
    if a == "done" and p["status"] == "open":
        db.update("board_posts", pid, status="done", done_at=now(), pinned_until=None, updated_at=now())
        flash(f"Marked as {p['k']['done']}. It stays up for a day so people know, then comes down.")
    elif a == "renew" and p["can_renew"]:
        db.update("board_posts", pid, expires_at=board._in(days=board.LIFE_DAYS), renewed=1, updated_at=now())
        flash("Renewed for another 14 days.")
    elif a == "delete":
        db.update("board_posts", pid, status="removed", updated_at=now())
        flash("Your post has been taken down.")
        return redirect(url_for(".board_page"))
    return redirect(url_for(".board_post", pid=pid))
