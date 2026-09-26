"""Member accounts on the public site: sign up, log in, profiles, My submissions, submitting, comments, votes,
flags, leaderboard, badges and community partners. Members never reach the dashboard or the AI."""
import json
import re
import time
from functools import wraps

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import community, pipeline, settings, specials, util
from ..db import loads, now
from ..disposable_domains import is_disposable
from ..security import (check_password, flask_secret_key, hash_password, ip_hash, password_problem,
                        rate_limited)

bp = Blueprint("members", __name__)


def _public_ctx():
    from .public import public_ctx
    return public_ctx()


bp.context_processor(_public_ctx)

JOIN_MIN_SECONDS = 3   # real people take longer than this to fill in the form; bots don't
USERNAME = re.compile(r"^[A-Za-z0-9_]{3,20}$")
RESERVED = {"admin", "staff", "newsroom", "editor", "owner", "support", "help", "root", "system", "moderator"}
FLAG_REASONS = [("facts", "Wrong facts"), ("offensive", "Offensive"), ("spam", "Spam"), ("other", "Other")]


# ── helpers ─────────────────────────────────────────────
def base_url():
    return (settings.get(g.db, "site_url") or request.host_url).rstrip("/")


def serializer(salt):
    return URLSafeTimedSerializer(flask_secret_key(), salt=salt)


def safe_next(target, default="/me"):
    if target and target.startswith("/") and not target.startswith(("//", "/admin")):
        return target
    return default


def log_in(m):
    session["mid"] = m["id"]
    session["mpv"] = m["password_hash"][-12:]
    session.permanent = True


def member_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.get("member"):
            flash("Log in or create a free account first.")
            return redirect(url_for("members.login", next=request.full_path))
        return fn(*a, **kw)
    return wrapper


def can_act(json_reply=False):
    """None if the member may post/vote/submit now, else a reason."""
    m = g.get("member")
    if not m:
        return "Log in to do that."
    if not community.active(m):
        return "Your account can't do that right now." + (
            f" Suspended until {util.fmt_date(util.local(g.db, m['suspended_until']))}." if m["status"] == "suspended"
            and m.get("suspended_until") else "")
    if community.needs_confirm(g.db, m):
        return "Confirm your email address first: check your inbox for our link."
    return None


def back(default="/"):
    from urllib.parse import urlparse
    ref = request.form.get("back") or request.referrer or default
    u = urlparse(ref)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "") + (f"#{u.fragment}" if u.fragment else "")
    return redirect(safe_next(path, default))


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json"


def send_confirm(m):
    tok = serializer("member-confirm").dumps({"m": m["id"]})
    return util.send_email(g.db, m["email"], f"Confirm your {settings.get(g.db, 'site_name')} account",
                           f"Hi @{m['username']},\n\nConfirm your email by opening this link:\n\n"
                           f"{base_url()}/confirm/{tok}\n\nIf you didn't sign up, ignore this email.")


def notify_owner(subject, body):
    util.send_email(g.db, util.notify_address(g.db), subject, body)


# ── join / log in ───────────────────────────────────────
@bp.route("/join", methods=["GET", "POST"])
def join():
    db = g.db
    if g.get("member") and not g.member.get("staff_user_id"):
        return redirect(url_for(".me"))
    error = None
    f = request.form
    if not settings.get(db, "members_open"):
        return render_template("public/page.html", title="Join", body="<p>We're not taking new sign-ups right now.</p>")
    if request.method == "POST":
        username, email, pw = f.get("username", "").strip(), f.get("email", "").strip(), f.get("password", "")
        if f.get("website"):
            return redirect(url_for("public.home"))
        try:
            started = serializer("join-form").loads(f.get("ft", ""), max_age=86400).get("t", 0)
        except (BadSignature, SignatureExpired):
            started = 0
        if not started or time.time() - started < JOIN_MIN_SECONDS:
            error = "Please try that again."  # filled in too fast, or not from our page: a bot
        elif rate_limited(db, "join:" + ip_hash(), 5, 3600):
            error = "Too many sign-ups from here. Try again in an hour."
        elif not USERNAME.match(username) or username.lower() in RESERVED:
            error = "Usernames are 3 to 20 letters, numbers or underscores."
        elif "@" not in email or "." not in email.split("@")[-1]:
            error = "Enter a valid email address."
        elif is_disposable(email, settings.get(db, "blocked_email_domains") or []):
            error = "Please use your regular email address, not a temporary one."
        elif password_problem(pw):
            error = password_problem(pw)
        elif db.val("SELECT 1 FROM members WHERE username=?", (username,)):
            error = "That username is taken."
        elif db.val("SELECT 1 FROM members WHERE email=?", (email,)):
            error = "There's already an account with that email. Log in instead, or reset your password."
        else:
            confirm = bool(settings.get(db, "members_confirm_email") and settings.get(db, "smtp_host"))
            mid = db.insert("members", email=email, username=username,
                            display_name=util.text_only(f.get("display_name", ""), 60), password_hash=hash_password(pw),
                            confirmed=0 if confirm else 1, created_at=now(), last_seen=None)
            m = db.one("SELECT * FROM members WHERE id=?", (mid,))
            guest = [int(t) for t in session.get("guest_tips") or [] if str(t).isdigit()]
            moved = 0
            if guest:
                marks = ",".join("?" * len(guest))
                # anonymous tips stay anonymous; the others join the account without public credit until
                # the member chooses it in My submissions
                moved = db.run(f"UPDATE tips SET member_id=?, credit=0, name=CASE WHEN name='' THEN ? ELSE name END "
                               f"WHERE id IN ({marks}) AND member_id IS NULL AND anonymous=0",
                               (mid, "@" + username, *guest)).rowcount
                session.pop("guest_tips", None)
            log_in(m)
            community.check_badges(db, mid)
            if confirm:
                send_confirm(m)
            msg = f"Welcome, @{username}!"
            if moved:
                msg += f" Your tip{'s are' if moved != 1 else ' is'} now in My submissions."
            if confirm:
                msg += " We've emailed you a link to confirm your address."
            flash(msg)
            return redirect(safe_next(request.args.get("next")))
    return render_template("public/join.html", error=error, f=f,
                           ft=serializer("join-form").dumps({"t": time.time()}))


@bp.route("/login", methods=["GET", "POST"])
def login():
    db = g.db
    error = None
    if request.method == "POST":
        who = request.form.get("email", "").strip()
        if rate_limited(db, "mlogin:" + ip_hash(), 8, 900):
            error = "Too many attempts. Wait 15 minutes and try again."
        else:
            m = db.one("SELECT * FROM members WHERE email=? OR username=?", (who, who.lstrip("@")))
            if m and check_password(m["password_hash"], request.form.get("password", "")):
                if m["status"] == "banned":
                    error = "This account has been closed."
                else:
                    log_in(m)
                    return redirect(safe_next(request.args.get("next"), "/"))
            else:
                error = "Wrong email, username or password."
    return render_template("public/login.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop("mid", None)
    session.pop("mpv", None)
    return redirect(url_for("public.home"))


@bp.route("/confirm/<token>")
def confirm(token):
    try:
        d = serializer("member-confirm").loads(token, max_age=7 * 86400)
    except (BadSignature, SignatureExpired):
        return render_template("public/page.html", title="Link expired",
                               body="<p>That link has expired. Log in and ask for a new one.</p>")
    g.db.run("UPDATE members SET confirmed=1 WHERE id=?", (d.get("m"),))
    flash("Thanks, your email is confirmed.")
    return redirect(url_for(".me") if g.get("member") else url_for(".login"))


@bp.route("/confirm/resend", methods=["POST"])
@member_required
def confirm_resend():
    if rate_limited(g.db, f"confirm:{g.member['id']}", 3, 3600):
        flash("We've sent a few already. Check your spam folder.", "error")
    else:
        send_confirm(g.member)
        flash("Sent. Check your inbox.")
    return back("/me")


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    db = g.db
    sent = False
    if request.method == "POST":
        sent = True
        if not rate_limited(db, "forgot:" + ip_hash(), 5, 3600):
            m = db.one("SELECT * FROM members WHERE email=?", (request.form.get("email", "").strip(),))
            if m and m["status"] != "banned":
                tok = serializer("member-reset").dumps({"m": m["id"], "p": m["password_hash"][-12:]})
                util.send_email(db, m["email"], "Reset your password",
                                f"Open this link within an hour to choose a new password:\n\n{base_url()}/reset/{tok}\n\n"
                                f"If you didn't ask for this, ignore this email.")
    return render_template("public/forgot.html", sent=sent, email_on=bool(settings.get(db, "smtp_host")))


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    db = g.db
    try:
        d = serializer("member-reset").loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        return render_template("public/page.html", title="Link expired", body="<p>That link has expired or was "
                                                                              "already used. Ask for a new one.</p>")
    m = db.one("SELECT * FROM members WHERE id=?", (d.get("m"),))
    if not m or m["password_hash"][-12:] != d.get("p"):
        return render_template("public/page.html", title="Link expired", body="<p>That link was already used.</p>")
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        error = password_problem(pw)
        if not error:
            db.update("members", m["id"], password_hash=hash_password(pw), confirmed=1)
            log_in(db.one("SELECT * FROM members WHERE id=?", (m["id"],)))
            flash("Password changed. You're logged in.")
            return redirect(url_for(".me"))
    return render_template("public/reset.html", error=error)


# ── profiles, leaderboard, badges ───────────────────────
@bp.route("/u/<username>")
def profile(username):
    db = g.db
    m = db.one("SELECT * FROM members WHERE username=? AND status!='banned'", (username,)) or abort(404)
    stories = db.q("SELECT * FROM stories WHERE status='published' AND member_id=? AND credit_public=1 "
                   "ORDER BY published_at DESC LIMIT 50", (m["id"],))
    from .. import crowns
    return render_template("public/profile.html", m=m, badges=community.member_badges(db, m["id"]), stories=stories,
                           lvl=community.level_of(m["points"]),
                           worn=db.q("SELECT * FROM crowns WHERE member_id=? ORDER BY kind='team' DESC, id", (m["id"],)),
                           book_total=db.val("SELECT COUNT(*) FROM badges WHERE active=1 AND rule!='manual'"),
                           called=db.one("SELECT COUNT(*) AS n, SUM(won) AS won FROM guesses WHERE member_id=?",
                                         (m["id"],)),
                           comments=db.val("SELECT COUNT(*) FROM comments WHERE member_id=? AND status='visible'",
                                           (m["id"],)),
                           orgs=db.q("SELECT * FROM orgs WHERE member_id=? AND status='approved'", (m["id"],)),
                           recent=db.q("SELECT c.*, s.headline, s.slug FROM comments c JOIN stories s ON s.id=c.story_id "
                                       "WHERE c.member_id=? AND c.status='visible' ORDER BY c.id DESC LIMIT 5",
                                       (m["id"],)))


BOARDS = [("week", "This week"), ("month", "This month"), ("all", "All time"), ("sports", "Sports season"),
          ("callit", "Call It")]


@bp.route("/leaderboard")
def leaderboard():
    from .. import crowns
    db = g.db
    board = request.args.get("board") or request.args.get("period") or "week"
    board = board if board in dict(BOARDS) else "week"
    season_name, start, end = crowns.season(db)
    base = ("SELECT m.*, SUM(p.points) AS period FROM points_log p JOIN members m ON m.id=p.member_id "
            "WHERE m.status!='banned' AND {w} GROUP BY m.id HAVING period > 0 ORDER BY period DESC, m.id LIMIT 50")
    marks = ",".join("?" * len(crowns.SPORTS_REASONS))
    if board == "all":
        rows = community.leaderboard(db, month=False)
    elif board == "month":
        rows = community.leaderboard(db, month=True)
    elif board == "week":
        rows = db.q(base.format(w="p.created_at >= ?"), (crowns._ago(7),))
    elif board == "sports":
        rows = db.q(base.format(w=f"p.reason IN ({marks}) AND p.created_at >= ? AND p.created_at < ?"),
                    (*crowns.SPORTS_REASONS, start, end))
    else:
        rows = db.q(base.format(w="p.reason='callit' AND p.created_at >= ? AND p.created_at < ?"), (start, end))
    blurbs = {"week": "Points earned in the last 7 days. A fresh race every week.",
              "month": "Points earned since the 1st.", "all": "Everything, ever.",
              "sports": f"{season_name}: scores, live updates, streams and videos. It starts fresh next season.",
              "callit": f"{season_name}: points from Call It predictions."}
    return render_template("public/leaderboard.html", board=board, boards=BOARDS, rows=rows, blurb=blurbs[board],
                           shouts=db.q("SELECT * FROM shoutouts ORDER BY id DESC LIMIT 8"), tab="leaderboard",
                           crown_count=db.val("SELECT COUNT(*) FROM crowns WHERE member_id IS NOT NULL"))


@bp.route("/badges")
def badges():
    db = g.db
    m = g.get("member")
    rows = community.badge_book(db, m["id"] if m else None)
    return render_template("public/badges.html", rows=rows, groups=community.GROUPS, tiers=community.TIER_NAMES,
                           have=sum(1 for b in rows if b["tier"]), total=sum(1 for b in rows if b["rule"] != "manual"))


@bp.route("/me/badges")
def my_badges():
    return redirect(url_for(".badges"))


@bp.route("/crowns")
def crowns_page():
    from .. import crowns
    db = g.db
    rows = crowns.listing(db)
    for r in rows:
        r["chaser"] = None
        top = {"team": lambda k: crowns.team_scores(db, int(k), 2), "section": lambda k: crowns.section_scores(db, k, 2),
               "oracle": lambda k: crowns.oracle_scores(db, 2)}[r["kind"]](r["key"])
        if len(top) > 1 and r["member_id"]:
            other = top[1] if top[0]["member_id"] == r["member_id"] else top[0]
            r["chaser"] = {"username": db.val("SELECT username FROM members WHERE id=?", (other["member_id"],)),
                           "gap": max(1, r["score"] - other["score"] + 1)}
    return render_template("public/crowns.html", rows=rows, min_score=crowns.MIN_SCORE, tab="leaderboard",
                           season=crowns.season(db)[0])


@bp.route("/u/<username>/badge/<slug>")
def badge_share(username, slug):
    """A badge someone earned, to share: the page has a picture made for Facebook and friends."""
    db = g.db
    m = db.one("SELECT * FROM members WHERE username=? AND status!='banned'", (username,)) or abort(404)
    b = db.one("SELECT b.*, mb.tier, mb.awarded_at FROM member_badges mb JOIN badges b ON b.id=mb.badge_id "
               "WHERE mb.member_id=? AND b.slug=? AND b.active=1", (m["id"], slug)) or abort(404)
    b["tier_name"] = community.tier_name(b, b["tier"])
    b["holders"] = db.val("SELECT COUNT(*) FROM member_badges WHERE badge_id=?", (b["id"],))
    return render_template("public/badge_share.html", m=m, b=b, tab="leaderboard",
                           share_url=base_url() + url_for(".badge_share", username=username, slug=slug))


@bp.route("/u/<username>/badge/<slug>.png")
def badge_image(username, slug):
    from flask import Response
    from .. import graphics
    db = g.db
    m = db.one("SELECT * FROM members WHERE username=? AND status!='banned'", (username,)) or abort(404)
    b = db.one("SELECT b.*, mb.tier FROM member_badges mb JOIN badges b ON b.id=mb.badge_id "
               "WHERE mb.member_id=? AND b.slug=? AND b.active=1", (m["id"], slug)) or abort(404)
    png = graphics.badge_card(db, m["username"], b["name"], community.tier_name(b, b["tier"]), b["rarity"],
                              (f"“{b['hint']}” Can you find it?" if b["secret"] else b["description"]),
                              secret=bool(b["secret"]))
    return Response(png, mimetype="image/png", headers={"Cache-Control": "public, max-age=3600"})


# ── my area ─────────────────────────────────────────────
@bp.route("/me")
@member_required
def me():
    db = g.db
    m = g.member
    subs = db.q("SELECT * FROM submissions WHERE member_id=? AND status!='withdrawn' ORDER BY updated_at DESC", (m["id"],))
    for s in subs:
        if s["story_id"]:
            s["story"] = db.one("SELECT slug, status FROM stories WHERE id=?", (s["story_id"],))
    tips = db.q("SELECT t.*, s.slug, s.status AS story_status FROM tips t LEFT JOIN stories s ON s.id=t.story_id "
                "WHERE t.member_id=? ORDER BY t.id DESC", (m["id"],))
    notices = db.q("SELECT * FROM notices WHERE member_id=? ORDER BY id DESC LIMIT 20", (m["id"],))
    db.run("UPDATE notices SET seen=1 WHERE member_id=?", (m["id"],))
    orgs = db.q("SELECT * FROM orgs WHERE member_id=? ORDER BY id DESC", (m["id"],))
    return render_template("public/me.html", subs=subs, tips=tips, notices=notices, orgs=orgs,
                           badges=community.member_badges(db, m["id"]))


@bp.route("/me/profile", methods=["GET", "POST"])
@member_required
def my_profile():
    db = g.db
    m = g.member
    error = None
    if request.method == "POST":
        f = request.form
        fields = {"display_name": util.text_only(f.get("display_name", ""), 60),
                  "bio": util.text_only(f.get("bio", ""), 600), "town": util.text_only(f.get("town", ""), 80)}
        if f.get("remove_photo"):
            fields["photo"] = None
        up = request.files.get("photo")
        if up and up.filename:
            name = util.save_image(up, max_side=400)
            if not name:
                error = "That photo isn't an image we can use (JPG or PNG under 12 MB)."
            else:
                fields["photo"] = name
        if f.get("new_password"):
            if not check_password(m["password_hash"], f.get("current_password", "")):
                error = "Your current password is wrong."
            elif password_problem(f["new_password"]):
                error = password_problem(f["new_password"])
            else:
                fields["password_hash"] = hash_password(f["new_password"])
        if not error:
            db.update("members", m["id"], **fields)
            if "password_hash" in fields:
                log_in(db.one("SELECT * FROM members WHERE id=?", (m["id"],)))
            flash("Saved.")
            return redirect(url_for(".my_profile"))
    return render_template("public/my_profile.html", error=error)


@bp.route("/me/tip/<int:tid>/credit", methods=["POST"])
@member_required
def tip_credit(tid):
    t = g.db.one("SELECT * FROM tips WHERE id=? AND member_id=?", (tid, g.member["id"])) or abort(404)
    if t["status"] == "new":
        g.db.update("tips", tid, credit=0 if t["credit"] else 1)
    return redirect(url_for(".me"))


@bp.route("/me/notices/seen", methods=["POST"])
@member_required
def notices_seen():
    g.db.run("UPDATE notices SET seen=1 WHERE member_id=?", (g.member["id"],))
    return ("", 204) if wants_json() else back()


# ── submitting ──────────────────────────────────────────
def my_orgs():
    return g.db.q("SELECT * FROM orgs WHERE member_id=? AND status='approved'", (g.member["id"],))


@bp.route("/submit")
def submit_choose():
    """One way to post: the newsroom gets its editor (with the AI assistant); members get the same kind of form."""
    if g.get("user"):
        return redirect(url_for("admin.new_post"))
    return redirect(url_for(".submit_form", kind="article"))


@bp.route("/submit/new/<kind>", methods=["GET", "POST"])
@bp.route("/submit/<int:sub_id>", methods=["GET", "POST"])
@member_required
def submit_form(kind=None, sub_id=None):
    db = g.db
    m = g.member
    sub = None
    if sub_id:
        sub = db.one("SELECT * FROM submissions WHERE id=? AND member_id=?", (sub_id, m["id"])) or abort(404)
        kind = sub["kind"]
        if sub["status"] not in ("draft", "waiting", "sent_back"):
            flash("This one can't be changed any more.", "error")
            return redirect(url_for(".me"))
    if kind == "facts" and not sub:
        return redirect(url_for("public.tip"))  # quick facts go through the tip form now
    if kind not in ("article", "facts"):
        abort(404)
    error = None
    f = request.form
    if request.method == "POST":
        problem = can_act()
        action = f.get("action", "draft")
        photos = loads(sub["photos"], []) if sub else []
        photos = [p for p in photos if p not in f.getlist("remove_photo")]
        uploads = [u for u in request.files.getlist("photos") if u and u.filename]
        facts = {k: util.text_only(f.get("fact_" + k, ""), 3000) for k, _ in pipeline.FACT_LABELS}
        links = [x.strip() for x in f.get("links", "").splitlines() if x.strip().startswith(("http://", "https://"))][:5]
        headline = util.text_only(f.get("headline", ""), 200)
        body = util.clean_html(f.get("body", ""))[:60000]
        cats = settings.get(db, "categories") or []
        org_ids = [o["id"] for o in my_orgs()]
        by = f.get("by", "")
        if by:  # one Byline choice: my name, a community member, or for my organization
            want_org = int(by[4:]) if by.startswith("org:") and by[4:].isdigit() else None
            org_id = want_org if want_org in org_ids else None
            credit = 0 if by == "anon" else 1
        else:
            org_id = f.get("org_id", type=int) if f.get("org_id", type=int) in org_ids else None
            credit = 0 if f.get("credit") == "0" else 1
        if problem:
            error = problem
        elif uploads and not f.get("photo_ok"):
            error = "Tick the box to confirm you took the photos or have permission to share them."
        elif f.get("video", "").strip() and not util.video_embed(f.get("video", "")):
            error = "That video link isn't one we can show. Use a YouTube link, or a Facebook video or reel link."
        elif len(photos) + len(uploads) > community.max_photos(db, m):
            error = f"You can attach up to {community.max_photos(db, m)} photos."
        elif action == "submit" and kind == "article" and (len(headline) < 5 or len(util.text_only(body)) < 80):
            error = "Add a headline and at least a few sentences before sending it."
        elif action == "submit" and kind == "facts" and len(facts["what"]) < 10:
            error = "Tell us what happened before sending it."
        elif rate_limited(db, f"submit:{m['id']}", 30, 86400):
            error = "You've sent a lot today. Try again tomorrow."
        else:
            for up in uploads:
                name = util.save_image(up)
                if name:
                    photos.append(name)
            fields = {"headline": headline if kind == "article" else util.text_only(facts["what"], 100),
                      "body": body if kind == "article" else "", "facts": json.dumps(facts if kind == "facts" else {}),
                      "links": json.dumps(links), "photos": json.dumps(photos),
                      "video": f.get("video", "").strip()[:500] or None,
                      "category": f.get("category") if f.get("category") in cats else "",
                      "subcategory": f.get("subcategory", "") if f.get("subcategory", "") in
                      settings.subcategories(db, f.get("category", "")) else "",
                      "summary": util.text_only(f.get("summary", ""), 400),
                      "photo_credit": util.text_only(f.get("photo_credit", ""), 120),
                      "credit": credit, "org_id": org_id, "updated_at": now()}
            if not sub:
                sub_id = db.insert("submissions", member_id=m["id"], kind=kind, status="draft", created_at=now(), **fields)
            else:
                db.update("submissions", sub_id, **fields)
            if action == "submit":
                sub = db.one("SELECT * FROM submissions WHERE id=?", (sub_id,))
                if kind == "article" and m["trusted"]:
                    sid = pipeline.submission_story(db, sub, trusted=True)
                    db.update("submissions", sub_id, status="published", submitted_at=now(), note="")
                    pipeline.publish(db, sid, note=f"published by trusted member @{m['username']}")
                    flash("Published! It's live on the site now.")
                    slug = db.val("SELECT slug FROM stories WHERE id=?", (sid,))
                    return redirect(url_for("public.article", slug=slug))
                db.update("submissions", sub_id, status="waiting", submitted_at=now())
                if settings.get(db, "notify_submissions"):
                    notify_owner(f"New from @{m['username']}: {fields['headline'][:60]}",
                                 f"@{m['username']} sent {'an article' if kind == 'article' else 'some facts'}.\n\n"
                                 f"Review it at {base_url()}/admin/members/submissions")
                flash("Sent! The newsroom will look at it. You'll see its status in My submissions.")
                return redirect(url_for(".me"))
            flash("Draft saved.")
            return redirect(url_for(".submit_form", sub_id=sub_id))
    data = sub or {}
    draft_body = util.clean_html(f.get("body", "")) if request.method == "POST" else (sub["body"] if sub else "")
    return render_template("public/submit_form.html" if kind == "article" else "public/submit_facts.html",
                           kind=kind, sub=sub, draft_body=draft_body, facts=loads(data.get("facts"), {}),
                           subcats=settings.subcategories(db),
                           photos=loads(data.get("photos"), []), links="\n".join(loads(data.get("links"), [])),
                           error=error, f=f, orgs=my_orgs(), labels=pipeline.FACT_LABELS,
                           max_photos=community.max_photos(db, m))


@bp.route("/submit/<int:sub_id>/withdraw", methods=["POST"])
@member_required
def submit_withdraw(sub_id):
    g.db.run("UPDATE submissions SET status='withdrawn', updated_at=? WHERE id=? AND member_id=? AND "
             "status IN ('draft','waiting','sent_back')", (now(), sub_id, g.member["id"]))
    flash("Withdrawn.")
    return redirect(url_for(".me"))


@bp.route("/me/photo/<name>")
@member_required
def my_photo(name):
    """Members can see their own unpublished submission photos."""
    from flask import send_file
    from ..db import UPLOADS
    ok = g.db.val("SELECT 1 FROM submissions WHERE member_id=? AND photos LIKE ?", (g.member["id"], f'%"{name}"%')) \
        or g.db.val("SELECT 1 FROM tips WHERE member_id=? AND photos LIKE ?", (g.member["id"], f'%"{name}"%'))
    p = (UPLOADS / name).resolve()
    if not ok or "/" in name or not str(p).startswith(str(UPLOADS.resolve())) or not p.exists():
        abort(404)
    return send_file(p)


# ── votes, flags, comments ──────────────────────────────
@bp.route("/vote", methods=["POST"])
def vote():
    problem = can_act()
    if problem:
        return (jsonify(ok=False, error=problem), 403) if wants_json() else (flash(problem, "error") or back())
    if rate_limited(g.db, f"vote:{g.member['id']}", 60, 60):
        return (jsonify(ok=False, error="Slow down a little."), 429) if wants_json() else back()
    target = request.form.get("target")
    voted, count = community.toggle_vote(g.db, g.member, target, request.form.get("id", type=int) or 0)
    if voted is None:
        msg = "You can't upvote your own." if target else "Not found."
        return (jsonify(ok=False, error=msg), 400) if wants_json() else (flash(msg, "error") or back())
    return jsonify(ok=True, voted=voted, count=count) if wants_json() else back()


@bp.route("/flag", methods=["POST"])
def flag():
    db = g.db
    problem = can_act()
    f = request.form
    target, tid = f.get("target"), f.get("id", type=int) or 0
    reasons = dict(FLAG_REASONS)
    if problem:
        flash(problem, "error")
        return back()
    if target not in ("story", "comment") or f.get("reason") not in reasons:
        abort(400)
    exists = db.val("SELECT 1 FROM stories WHERE id=? AND status='published'" if target == "story"
                    else "SELECT 1 FROM comments WHERE id=?", (tid,))
    if not exists:
        abort(404)
    if rate_limited(db, f"flag:{g.member['id']}", 20, 86400):
        flash("You've flagged a lot today. The newsroom will catch up.", "error")
        return back()
    db.run("INSERT OR IGNORE INTO flags(member_id,target,target_id,reason,note,weight,created_at) VALUES(?,?,?,?,?,?,?)",
           (g.member["id"], target, tid, f["reason"], util.text_only(f.get("note", ""), 500),
            community.flag_weight(db, g.member), now()))
    community.apply_flag_threshold(db, target, tid)
    if target == "comment":
        community.recount_comments(db, db.val("SELECT story_id FROM comments WHERE id=?", (tid,)))
    if settings.get(db, "notify_flags"):
        notify_owner(f"Flagged: {reasons[f['reason']]}", f"@{g.member['username']} flagged a {target}.\n\n"
                                                          f"{base_url()}/admin/community/flags")
    flash("Thanks. The newsroom will take a look.")
    return back()


@bp.route("/comment", methods=["POST"])
def comment():
    db = g.db
    f = request.form
    s = db.one("SELECT * FROM stories WHERE id=? AND status='published'", (f.get("story_id", type=int),)) or abort(404)
    problem = can_act()
    body = (f.get("body") or "").strip()
    here = url_for("public.article", slug=s["slug"])
    if problem:
        flash(problem, "error")
        return redirect(here + "#comments")
    if not settings.get(db, "comments_on") or s["comments_mode"] != "open":
        flash("Comments are closed on this story.", "error")
        return redirect(here + "#comments")
    parent = f.get("parent_id", type=int)
    if parent and not db.val("SELECT 1 FROM comments WHERE id=? AND story_id=?", (parent, s["id"])):
        abort(400)
    if len(body) < 2 or len(body) > 5000:
        flash("Comments are 2 to 5,000 characters.", "error")
        return redirect(here + "#comments")
    if rate_limited(db, f"comment:{g.member['id']}", 10, 600):
        flash("You're commenting very fast. Wait a few minutes.", "error")
        return redirect(here + "#comments")
    hold = community.hold_reason(db, g.member, body)
    cid = db.insert("comments", story_id=s["id"], member_id=g.member["id"], parent_id=parent, body=body,
                    status="held" if hold else "visible", hold_reason=hold, created_at=now())
    community.recount_comments(db, s["id"])
    if hold:
        flash("Thanks! A new member's first comments wait for a quick check before they appear."
              if hold.startswith("New member") else "Thanks. Your comment will appear after a quick check.")
    else:
        community.check_badges(db, g.member["id"])
        if parent:
            pm = db.val("SELECT member_id FROM comments WHERE id=?", (parent,))
            if pm != g.member["id"]:
                community.notice(db, pm, f"@{g.member['username']} replied to your comment on “{s['headline'][:80]}”.",
                                 here + f"#c{cid}", kind="reply")
                community.check_badges(db, g.member["id"])
    return redirect(here + f"#c{cid}")


@bp.route("/comment/<int:cid>/edit", methods=["POST"])
def comment_edit(cid):
    db = g.db
    c = db.one("SELECT * FROM comments WHERE id=?", (cid,)) or abort(404)
    if not g.get("member") or c["member_id"] != g.member["id"] or c["status"] == "deleted":
        abort(403)
    slug = db.val("SELECT slug FROM stories WHERE id=?", (c["story_id"],))
    problem = can_act()
    if problem:
        flash(problem, "error")
        return redirect(url_for("public.article", slug=slug) + f"#c{cid}")
    age = (util.parse_iso(now()) - util.parse_iso(c["created_at"])).total_seconds() / 60
    body = (request.form.get("body") or "").strip()
    if age > community.pts(db, "comment_edit_minutes"):
        flash("The time to edit this comment has passed. You can still delete it.", "error")
    elif 2 <= len(body) <= 5000:
        hold = community.hold_reason(db, g.member, body) if c["status"] == "visible" else None
        db.update("comments", cid, body=body, edited_at=now(), **({"status": "held", "hold_reason": hold} if hold else {}))
        community.recount_comments(db, c["story_id"])
    return redirect(url_for("public.article", slug=slug) + f"#c{cid}")


@bp.route("/comment/<int:cid>/delete", methods=["POST"])
def comment_delete(cid):
    db = g.db
    c = db.one("SELECT * FROM comments WHERE id=?", (cid,)) or abort(404)
    if not g.get("member") or c["member_id"] != g.member["id"]:
        abort(403)
    db.update("comments", cid, status="deleted", body="", edited_at=now())
    community.recount_comments(db, c["story_id"])
    slug = db.val("SELECT slug FROM stories WHERE id=?", (c["story_id"],))
    return redirect(url_for("public.article", slug=slug) + "#comments")


# ── community partners ──────────────────────────────────
ABOUT_MAX = 300  # the author box's "few words": about three lines


def org_contact_values(f):
    """Address, website and calendar link from a form, cleaned up. Shared by the application and the manage page."""
    web, cal = f.get("website", "").strip(), f.get("calendar_url", "").strip()
    return {"address": util.text_only(f.get("address", ""), 200),
            "website": web[:300] if web.startswith(("http://", "https://")) else "",
            "calendar_url": cal[:500] if cal.startswith(("http://", "https://", "webcal://")) else ""}


@bp.route("/partners/<int:oid>/manage", methods=["GET", "POST"])
@member_required
def partner_manage(oid):
    """The partner's own page for their author box: logo, a few words, address, website, calendar."""
    db = g.db
    o = db.one("SELECT * FROM orgs WHERE id=? AND status='approved'", (oid,)) or abort(404)
    if o["member_id"] != g.member["id"]:
        abort(403)
    error = None
    if request.method == "POST" and request.form.get("action") in ("special", "delete_special"):
        return _partner_special(o)
    if request.method == "POST":
        f = request.form
        error = can_act()
        about = util.text_only(f.get("description", ""), 2000)
        if not error and len(about) > ABOUT_MAX:
            error = f"Keep “About us” to {ABOUT_MAX} characters so it fits the box (it's {len(about)} now)."
        if not error and rate_limited(db, f"org-edit:{oid}", 20, 86400):
            error = "That's a lot of changes for one day. Try again tomorrow."
        if not error:
            vals = {"description": about, "updated_at": now(), "needs_look": 1, **org_contact_values(f)}
            up = request.files.get("logo")
            if up and up.filename:
                try:
                    vals["logo"] = util.save_image(up, max_side=600)
                except Exception:
                    error = "That logo couldn't be read. Try a JPG or PNG."
            elif f.get("remove_logo"):
                vals["logo"] = None
        if not error:
            cal_changed = vals["calendar_url"] != (o["calendar_url"] or "")
            db.update("orgs", oid, **vals)
            util.activity(db, None, "partner updated their author box", f"org:{oid}", o["name"])
            if settings.get(db, "notify_submissions"):
                notify_owner(f"{o['name']} updated their author box",
                             f"@{g.member['username']} changed {o['name']}'s author box."
                             + (" Their calendar link changed too, so check their calendar source." if cal_changed else "")
                             + f"\n\n{base_url()}/admin/community/partners")
            flash("Saved. Your author box is updated on all your stories.")
            return redirect(url_for(".partner_manage", oid=oid))
        o = {**o, **{k: v for k, v in request.form.items() if k in ("description", "address", "website", "calendar_url")}}
    stories = db.q("SELECT headline, slug, published_at FROM stories WHERE org_id=? AND status='published' "
                   "ORDER BY published_at DESC LIMIT 5", (oid,))
    return render_template("public/partner_manage.html", o=o, error=error, stories=stories, about_max=ABOUT_MAX,
                           specials=specials.upcoming_for(db, oid), days=specials.days_ahead(db),
                           special_error=request.args.get("special_error"))


def _partner_special(o):
    """Post or take down one of the partner's specials. Live right away."""
    db, f = g.db, request.form
    here = url_for(".partner_manage", oid=o["id"]) + "#specials"
    problem = can_act()
    if problem:
        flash(problem, "error")
        return redirect(here)
    if f.get("action") == "delete_special":
        db.run("UPDATE specials SET status='removed' WHERE id=? AND org_id=?", (f.get("id", type=int), o["id"]))
        flash("Special taken down.")
        return redirect(here)
    title = util.text_only(f.get("title", ""), 120)
    day = f.get("day", "")
    if len(title) < 2:
        flash("Give the special a name.", "error")
    elif day not in dict(specials.days_ahead(db)):
        flash("Pick the day it's on.", "error")
    elif rate_limited(db, f"special:{o['id']}", specials.MAX_PER_DAY, 86400):
        flash("That's a lot of specials for one day. Try again tomorrow.", "error")
    else:
        image = None
        up = request.files.get("image")
        if up and up.filename:
            try:
                image = util.save_image(up, max_side=1200)
            except Exception:
                flash("That photo couldn't be read, so the special was posted without it.", "error")
        db.insert("specials", org_id=o["id"], member_id=g.member["id"], title=title, day=day, image=image,
                  price=util.text_only(f.get("price", ""), 30), description=util.text_only(f.get("description", ""), 600),
                  created_at=now())
        flash("Posted! It's on Today's specials" + (" and the homepage." if day == specials.today(db) else " on that day."))
    return redirect(here)


@bp.route("/partners", methods=["GET", "POST"])
def partners():
    db = g.db
    error = None
    f = request.form
    if request.method == "POST":
        problem = can_act()
        cats = settings.get(db, "categories") or []
        if problem:
            error = problem
        elif len(f.get("name", "").strip()) < 2 or f.get("category") not in cats:
            error = "Add your organization's name and pick a category."
        elif db.val("SELECT 1 FROM orgs WHERE member_id=? AND status='pending'", (g.member["id"],)):
            error = "You already have an application waiting. We'll be in touch."
        elif rate_limited(db, f"partner:{g.member['id']}", 3, 86400):
            error = "Try again tomorrow."
        else:
            logo = None
            up = request.files.get("logo")
            if up and up.filename:
                logo = util.save_image(up, max_side=600)
            db.insert("orgs", member_id=g.member["id"], name=util.text_only(f["name"], 120), category=f["category"],
                      logo=logo, description=util.text_only(f.get("description", ""), ABOUT_MAX),
                      contact=util.text_only(f.get("contact", ""), 200), created_at=now(), **org_contact_values(f))
            if settings.get(db, "notify_submissions"):
                notify_owner(f"Partner application: {f['name'][:60]}", f"{base_url()}/admin/community/partners")
            flash("Thanks! We'll review your application and let you know.")
            return redirect(url_for(".me"))
    rows = db.q("SELECT * FROM orgs WHERE status='approved' ORDER BY category, name")
    return render_template("public/partners.html", rows=rows, error=error, f=f,
                           intro=util.render_markdown(util.fill_page(db, settings.get(db, "partner_intro"))))
