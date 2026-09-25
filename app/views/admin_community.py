"""Dashboard pages for the community: member submissions, members, comments and flags, badges, partners,
and social accounts. Registered on the admin blueprint."""
import json
import secrets as pysecrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import community, pipeline, settings, social, sources, specials, util
from ..db import loads, now
from ..security import hash_password, login_required
from .admin import bp, start_and_analyze, uid

SUB_TABS = [("waiting", "Waiting"), ("sent_back", "Sent back"), ("trusted", "Published by trusted members"),
            ("published", "Published"), ("declined", "Declined")]


# ── member submissions ──────────────────────────────────
@bp.route("/members/submissions")
@login_required()
def submissions():
    db = g.db
    tab = request.args.get("tab") if request.args.get("tab") in dict(SUB_TABS) else "waiting"
    base = ("SELECT s.*, m.username, m.trusted, m.points FROM submissions s JOIN members m ON m.id=s.member_id ")
    if tab == "trusted":
        rows = db.q(base + "JOIN stories st ON st.id=s.story_id WHERE st.trusted_publish=1 ORDER BY s.updated_at DESC "
                           "LIMIT 200")
    else:
        rows = db.q(base + "WHERE s.status=? ORDER BY s.submitted_at DESC, s.id DESC LIMIT 200", (tab,))
    counts = {k: db.val("SELECT COUNT(*) FROM submissions WHERE status=?", (k,)) for k, _ in SUB_TABS}
    counts["trusted"] = db.val("SELECT COUNT(*) FROM stories WHERE trusted_publish=1")
    return render_template("admin/submissions.html", rows=rows, tab=tab, tabs=SUB_TABS, counts=counts)


@bp.route("/members/submissions/<int:sub_id>", methods=["GET", "POST"])
@login_required()
def submission(sub_id):
    db = g.db
    sub = db.one("SELECT * FROM submissions WHERE id=?", (sub_id,)) or abort(404)
    m = db.one("SELECT * FROM members WHERE id=?", (sub["member_id"],))
    if request.method == "POST":
        if not g.user or g.user["role"] == "reviewer":
            abort(403)
        action = request.form.get("action")
        note = util.text_only(request.form.get("note", ""), 1500)
        if sub["status"] not in ("waiting", "sent_back"):
            flash("This submission was already handled.", "error")
            return redirect(url_for(".submission", sub_id=sub_id))
        if action == "publish" and sub["kind"] == "article":
            sid = pipeline.submission_story(db, sub, user_id=uid())
            db.update("submissions", sub_id, status="published", note="")
            pipeline.publish(db, sid, user_id=uid(), note=f"member article by @{m['username']}")
            flash("Published.")
            return redirect(url_for(".story", sid=sid))
        if action == "edit" and sub["kind"] == "article":
            sid = pipeline.submission_story(db, sub, user_id=uid(), edited=True)
            db.update("submissions", sub_id, status="developing")
            flash("Edit it, then publish. It will show the “Edited by the newsroom” note.")
            return redirect(url_for(".story", sid=sid))
        if action == "develop":
            sid = pipeline.develop_submission(db, sub, user_id=uid())
            return start_and_analyze(sid)
        if action in ("send_back", "decline"):
            if action == "send_back" and not note:
                flash("Add a note so they know what to change.", "error")
                return redirect(url_for(".submission", sub_id=sub_id))
            db.update("submissions", sub_id, status="sent_back" if action == "send_back" else "declined", note=note,
                      updated_at=now())
            what = sub["headline"][:80] or "your submission"
            if action == "send_back":
                community.notice(db, m["id"], f"The newsroom sent back “{what}”: {note}", f"/submit/{sub_id}",
                                 kind="sent_back")
            else:
                community.notice(db, m["id"], f"The newsroom won't be running “{what}”." + (f" {note}" if note else ""),
                                 "/me", kind="declined")
            util.send_email(db, m["email"], f"About your submission to {settings.get(db, 'site_name')}",
                            (f"The newsroom sent back “{what}” with this note:\n\n{note}\n\nEdit and resend it from "
                             f"My submissions." if action == "send_back" else
                             f"Thanks for sending “{what}”. We won't be running it this time." +
                             (f"\n\n{note}" if note else "")))
            util.activity(db, uid(), action.replace("_", " "), f"submission:{sub_id}", what)
            flash("Sent back with your note." if action == "send_back" else "Declined.")
            return redirect(url_for(".submissions"))
        abort(400)
    story = db.one("SELECT id, slug, status FROM stories WHERE id=?", (sub["story_id"],)) if sub["story_id"] else None
    return render_template("admin/submission.html", sub=sub, m=m, facts=loads(sub["facts"], {}),
                           photos=loads(sub["photos"], []), links=loads(sub["links"], []), labels=pipeline.FACT_LABELS,
                           story=story, org=db.one("SELECT * FROM orgs WHERE id=?", (sub["org_id"],)) if sub["org_id"] else None)


# ── members ─────────────────────────────────────────────
@bp.route("/community/members")
@login_required("editor")
def members():
    db = g.db
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "all")
    sorts = {"new": "m.id DESC", "points": "m.points DESC", "work": "m.work_points DESC", "social": "m.social_points DESC"}
    sort = request.args.get("sort") if request.args.get("sort") in sorts else "new"
    order = sorts[sort]
    where, args = ["1=1"], []
    if q:
        where.append("(username LIKE ? OR email LIKE ? OR display_name LIKE ?)")
        args += [f"%{q}%"] * 3
    if show == "trusted":
        where.append("trusted=1")
    elif show == "suggested":
        where.append("trusted=0 AND points >= ?")
        args.append(community.pts(db, "priv_suggest"))
    elif show == "restricted":
        where.append("status!='active'")
    rows = db.q(f"SELECT m.*, (SELECT COUNT(*) FROM stories s WHERE s.member_id=m.id AND s.status='published') AS pub "
                f"FROM members m WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 300", args)
    return render_template("admin/members.html", rows=rows, q=q, show=show, sort=sort,
                           totals=db.one("SELECT COALESCE(SUM(work_points),0) AS work, COALESCE(SUM(social_points),0) "
                                         "AS social FROM members"),
                           suggest_at=community.pts(db, "priv_suggest"))


@bp.route("/community/members/<int:mid>", methods=["GET", "POST"])
@login_required("editor")
def member(mid):
    db = g.db
    m = db.one("SELECT * FROM members WHERE id=?", (mid,)) or abort(404)
    temp = None
    if request.method == "POST":
        f = request.form
        a = f.get("action")
        note = util.text_only(f.get("note", ""), 1000)
        if m["staff_user_id"] and a in ("reset", "ban", "suspend", "hold", "restore", "confirm", "trusted"):
            flash("This is a staff member's public profile. It's managed from their own Account page.", "error")
            return redirect(url_for(".member", mid=mid))
        if a == "trusted":
            db.update("members", mid, trusted=0 if m["trusted"] else 1)
            if not m["trusted"]:
                community.notice(db, mid, "The newsroom trusts you to publish your own articles straight away. "
                                          "Thank you!", "/submit", kind="trusted")
        elif a == "warn":
            community.notice(db, mid, "A note from the newsroom: " + (note or "please keep comments respectful."),
                             kind="warning")
            util.send_email(db, m["email"], f"A note from {settings.get(db, 'site_name')}", note or
                            "Please keep your comments respectful and on topic.")
        elif a == "hold":
            db.update("members", mid, status="held" if m["status"] != "held" else "active")
        elif a == "suspend":
            days = max(1, min(365, f.get("days", type=int) or 7))
            until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
            db.update("members", mid, status="suspended", suspended_until=until)
        elif a == "ban":
            db.update("members", mid, status="banned")
            db.run("UPDATE comments SET status='hidden' WHERE member_id=? AND status IN ('visible','held')", (mid,))
            for sid in {r["story_id"] for r in db.q("SELECT story_id FROM comments WHERE member_id=?", (mid,))}:
                community.recount_comments(db, sid)
        elif a == "restore":
            db.update("members", mid, status="active", suspended_until=None)
        elif a == "badge":
            b = db.one("SELECT * FROM badges WHERE id=?", (f.get("badge_id", type=int),))
            if b:
                community.give_badge(db, mid, b, by=uid())
        elif a == "unbadge":
            db.run("DELETE FROM member_badges WHERE member_id=? AND badge_id=?", (mid, f.get("badge_id", type=int)))
        elif a == "reset":
            temp = pysecrets.token_urlsafe(9)
            db.update("members", mid, password_hash=hash_password(temp))
        elif a == "confirm":
            db.update("members", mid, confirmed=1)
        elif a == "note":
            db.update("members", mid, note=note)
        util.activity(db, uid(), f"member {a}", f"member:{mid}", m["username"])
        if not temp:
            flash("Done.")
            return redirect(url_for(".member", mid=mid))
        m = db.one("SELECT * FROM members WHERE id=?", (mid,))
    return render_template(
        "admin/member.html", m=m, temp=temp, badges=community.member_badges(db, mid),
        all_badges=db.q("SELECT * FROM badges WHERE active=1 ORDER BY sort"),
        subs=db.q("SELECT * FROM submissions WHERE member_id=? ORDER BY id DESC LIMIT 30", (mid,)),
        tips=db.q("SELECT * FROM tips WHERE member_id=? ORDER BY id DESC LIMIT 30", (mid,)),
        comments=db.q("SELECT c.*, s.headline, s.slug FROM comments c JOIN stories s ON s.id=c.story_id "
                      "WHERE c.member_id=? ORDER BY c.id DESC LIMIT 30", (mid,)),
        points=[{**p, "kind": community.kind_of(p["reason"])} for p in
                db.q("SELECT * FROM points_log WHERE member_id=? ORDER BY id DESC LIMIT 30", (mid,))],
        suggest_at=community.pts(db, "priv_suggest"))


# ── comments and flags ──────────────────────────────────
@bp.route("/community/flags", methods=["GET", "POST"])
@login_required("editor")
def moderation():
    db = g.db
    if request.method == "POST":
        f = request.form
        a, cid = f.get("action"), f.get("id", type=int)
        if a in ("approve", "hide", "delete") and cid:
            c = db.one("SELECT * FROM comments WHERE id=?", (cid,)) or abort(404)
            db.update("comments", cid, status={"approve": "visible", "hide": "hidden", "delete": "deleted"}[a],
                      **({"body": ""} if a == "delete" else {}))
            if a == "approve" and c["status"] == "held":
                community.check_badges(db, c["member_id"])
                pm = db.val("SELECT member_id FROM comments WHERE id=?", (c["parent_id"],)) if c["parent_id"] else None
                if pm and pm != c["member_id"]:
                    who = db.val("SELECT username FROM members WHERE id=?", (c["member_id"],))
                    s = db.one("SELECT headline, slug FROM stories WHERE id=?", (c["story_id"],))
                    community.notice(db, pm, f"@{who} replied to your comment on “{s['headline'][:80]}”.",
                                     f"/story/{s['slug']}#c{cid}", kind="reply")
                    community.check_badges(db, c["member_id"])
            db.run("UPDATE flags SET status='resolved', outcome=? WHERE target='comment' AND target_id=? AND status='open'",
                   ("kept" if a == "approve" else "removed", cid))
            community.recount_comments(db, c["story_id"])
        elif a in ("dismiss", "correction") and f.get("target") in ("story", "comment"):
            db.run("UPDATE flags SET status='resolved', outcome=? WHERE target=? AND target_id=? AND status='open'",
                   ("correction" if a == "correction" else "dismissed", f["target"], f.get("target_id", type=int)))
            if a == "correction":
                for r in db.q("SELECT member_id FROM flags WHERE target=? AND target_id=? AND outcome='correction'",
                              (f["target"], f.get("target_id", type=int))):
                    community.notice(db, r["member_id"], "Your flag led to a correction. Thanks for the good eye!",
                                     kind="thanks")
                    community.check_badges(db, r["member_id"])
        elif a in ("lock", "off", "open") and f.get("story_id", type=int):
            db.update("stories", f.get("story_id", type=int), comments_mode={"lock": "locked", "off": "off",
                                                                            "open": "open"}[a])
        util.activity(db, uid(), f"moderation {a}", f"comment:{cid}" if cid else "")
        return redirect(request.referrer or url_for(".moderation"))
    tab = request.args.get("tab", "held")
    held = db.q("SELECT c.*, m.username, m.points, s.headline, s.slug FROM comments c JOIN members m ON m.id=c.member_id "
                "JOIN stories s ON s.id=c.story_id WHERE c.status='held' ORDER BY c.id LIMIT 200")
    flags = db.q("SELECT f.target, f.target_id, COUNT(*) AS n, SUM(f.weight) AS weight, GROUP_CONCAT(DISTINCT f.reason) "
                 "AS reasons, GROUP_CONCAT(f.note, ' | ') AS notes, MIN(f.created_at) AS first_at FROM flags f "
                 "WHERE f.status='open' GROUP BY f.target, f.target_id ORDER BY weight DESC LIMIT 200")
    for fl in flags:
        if fl["target"] == "comment":
            fl["c"] = db.one("SELECT c.*, m.username, s.headline, s.slug FROM comments c JOIN members m ON "
                             "m.id=c.member_id JOIN stories s ON s.id=c.story_id WHERE c.id=?", (fl["target_id"],))
        else:
            fl["s"] = db.one("SELECT id, headline, slug FROM stories WHERE id=?", (fl["target_id"],))
    recent = db.q("SELECT c.*, m.username, s.headline, s.slug FROM comments c JOIN members m ON m.id=c.member_id "
                  "JOIN stories s ON s.id=c.story_id WHERE c.status='visible' ORDER BY c.id DESC LIMIT 100") \
        if tab == "recent" else []
    return render_template("admin/moderation.html", tab=tab, held=held, flags=flags, recent=recent)


# ── badges ──────────────────────────────────────────────
@bp.route("/community/badges", methods=["GET", "POST"])
@login_required("owner")
def badges():
    db = g.db
    if request.method == "POST":
        f = request.form
        fields = {"name": util.text_only(f.get("name", ""), 40), "icon": f.get("icon", "").strip()[:8] or "★",
                  "description": util.text_only(f.get("description", ""), 200),
                  "grp": f.get("grp") if f.get("grp") in dict(community.GROUPS) else "special",
                  "rule": f.get("rule") if f.get("rule") in community.RULES else "manual",
                  "active": 1 if f.get("active") else 0,
                  "rarity": f.get("rarity") if f.get("rarity") in dict(community.RARITIES) else "common",
                  "secret": 1 if f.get("secret") else 0, "hint": util.text_only(f.get("hint", ""), 120)}
        levels = sorted({int(x) for x in f.get("tiers", "").replace(",", " ").split() if x.isdigit() and int(x) > 0})[:4]
        levels = levels or [1]
        fields.update(tiers=json.dumps(levels), n=levels[0])
        bid = f.get("id", type=int)
        if not fields["name"]:
            flash("Give the badge a name.", "error")
        elif bid:
            db.update("badges", bid, **fields)
            flash("Badge saved.")
        else:
            db.insert("badges", slug=util.slugify(fields["name"], 30) + "-" + pysecrets.token_hex(2), sort=200, **fields)
            flash("Badge added.")
        return redirect(url_for(".badges"))
    rows = db.q("SELECT b.*, (SELECT COUNT(*) FROM member_badges mb WHERE mb.badge_id=b.id) AS holders FROM badges b "
                "ORDER BY sort, id")
    return render_template("admin/badges.html", rows=rows, groups=community.GROUPS, rules=community.RULES,
                           rarities=community.RARITIES)


# ── partners ────────────────────────────────────────────
@bp.route("/community/partners", methods=["GET", "POST"])
@login_required("editor")
def partners_admin():
    db = g.db
    if request.method == "POST":
        f = request.form
        o = db.one("SELECT * FROM orgs WHERE id=?", (f.get("id", type=int),)) or abort(404)
        a = f.get("action")
        if a == "approve":
            db.update("orgs", o["id"], status="approved", slug=o["slug"] or util.slugify(o["name"], 50) + f"-{o['id']}")
            if o["member_id"]:
                community.give_badge_slug(db, o["member_id"], "partner", by=uid())
                community.notice(db, o["member_id"], f"{o['name']} is now a Community Partner. Thank you! You can "
                                 f"change your logo and the few words about you any time.",
                                 f"/partners/{o['id']}/manage", kind="partner")
            flash(f"{o['name']} approved.")
        elif a == "decline":
            db.update("orgs", o["id"], status="declined")
            community.notice(db, o["member_id"], f"We couldn't approve {o['name']} as a partner right now.")
        elif a == "calendar" and o["calendar_url"]:
            db.insert("sources", name=f"{o['name']} calendar", type="calendar", trust="official", category=o["category"],
                      interval_min=360, config=json.dumps({"url": o["calendar_url"]}), approval="always",
                      created_at=now())
            flash("Added their calendar as a source.")
        elif a == "edit":
            from .members import org_contact_values
            cats = settings.get(db, "categories") or []
            vals = dict(name=util.text_only(f.get("name", ""), 120) or o["name"],
                        category=f.get("category") if f.get("category") in cats else o["category"],
                        description=util.text_only(f.get("description", ""), 1000), needs_look=0,
                        **org_contact_values(f))
            up = request.files.get("logo")
            if up and up.filename:
                vals["logo"] = util.save_image(up, max_side=600)
            elif f.get("remove_logo"):
                vals["logo"] = None
            db.update("orgs", o["id"], **vals)
            flash("Saved.")
        elif a == "looked":
            db.update("orgs", o["id"], needs_look=0)
        elif a == "remove_special":
            db.run("UPDATE specials SET status='removed' WHERE id=? AND org_id=?", (f.get("special_id", type=int), o["id"]))
            flash("Special removed.")
        elif a == "remove":
            db.update("orgs", o["id"], status="declined")
        util.activity(db, uid(), f"partner {a}", f"org:{o['id']}", o["name"])
        return redirect(url_for(".partners_admin"))
    return render_template("admin/partners.html",
                           pending=db.q("SELECT o.*, m.username FROM orgs o LEFT JOIN members m ON m.id=o.member_id "
                                        "WHERE o.status='pending' ORDER BY o.id"),
                           approved=db.q("SELECT o.*, m.username FROM orgs o LEFT JOIN members m ON m.id=o.member_id "
                                         "WHERE o.status='approved' ORDER BY o.needs_look DESC, o.category, o.name"),
                           specials=specials.upcoming_all(db),
                           categories=settings.get(db, "categories"))


# ── social accounts ─────────────────────────────────────
@bp.route("/social", methods=["GET", "POST"])
@login_required("owner")
def social_accounts():
    db = g.db
    if request.method == "POST":
        f = request.form
        p = f.get("platform")
        if p == "_defaults":
            cats = settings.get(db, "categories") or []
            settings.put(db, "social_defaults", {c: [k for k, _ in settings.PLATFORMS if f.get(f"d|{c}|{k}")]
                                                 for c in cats})
            flash("Defaults saved.")
        elif p in social.FIELDS:
            for key, _label, secret, _help in social.FIELDS[p]:
                v = f.get(key, "").strip()
                if f.get(key + "__remove"):
                    social.save(db, key, "")
                elif v or not secret:
                    social.save(db, key, v)
            util.activity(db, uid(), "social account saved", p)
            flash("Saved. Use Test to check it works.")
        return redirect(url_for(".social_accounts") + f"#{p}")
    vals = {k: (social.val(db, k) if not s else bool(social.val(db, k)))
            for fl in social.FIELDS.values() for k, _l, s, _h in fl}
    return render_template("admin/social.html", platforms=settings.PLATFORMS, fields=social.FIELDS, vals=vals,
                           connected={p: social.connected(db, p) for p in social.FIELDS},
                           defaults=settings.get(db, "social_defaults") or {},
                           categories=settings.get(db, "categories"), site_url=settings.get(db, "site_url"),
                           sections=settings.SECTIONS, later=settings.LATER, section="social")


@bp.route("/social/test", methods=["POST"])
@login_required("owner")
def social_test():
    ok, msg = social.test(g.db, request.form.get("platform"))
    return jsonify(ok=ok, error=None if ok else msg, message=msg)


# ── staff public profile ────────────────────────────────
@bp.route("/account/profile", methods=["POST"])
@login_required()
def staff_profile():
    db = g.db
    import re
    from .members import RESERVED, USERNAME
    uname = request.form.get("username", "").strip()
    existing = community.staff_member(db, g.user)
    if existing:
        db.update("members", existing["id"], display_name=util.text_only(request.form.get("display_name", ""), 60),
                  bio=util.text_only(request.form.get("bio", ""), 600))
        flash("Public profile saved.")
    elif not USERNAME.match(uname) or uname.lower() in RESERVED or re.fullmatch(r"\d+", uname):
        flash("Usernames are 3 to 20 letters, numbers or underscores.", "error")
    elif db.val("SELECT 1 FROM members WHERE username=?", (uname,)):
        flash("That username is taken.", "error")
    else:
        email = g.user["email"]
        if db.val("SELECT 1 FROM members WHERE email=?", (email,)):
            email = f"staff+{g.user['id']}.{pysecrets.token_hex(3)}@{email.split('@')[-1]}"
        mid = db.insert("members", email=email, username=uname, display_name=g.user["name"],
                        password_hash=hash_password(pysecrets.token_urlsafe(24)), confirmed=1,
                        staff_user_id=g.user["id"], created_at=now())
        community.give_badge_slug(db, mid, "staff")
        flash(f"Your public profile @{uname} is ready. When you're logged in here, you comment and vote as @{uname} "
              f"with a Staff badge.")
    return redirect(url_for(".account"))
