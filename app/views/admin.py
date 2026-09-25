"""The newsroom dashboard: setup wizard, login, sources, approval pages, stories, tips, settings."""
import json
import os
from datetime import datetime, timezone
import secrets as pysecrets

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, session,
                   url_for)

from .. import ai, community, folders, pipeline, settings, social, sources, util, weather, writing
from .. import writing_defaults as WD
from ..db import BACKUPS, loads, now, story_row
from ..security import (ROLES, can, check_password, has_secret, hash_password, ip_hash,
                        login_required, password_problem, rate_limited, set_secret)

bp = Blueprint("admin", __name__)


# ── helpers ─────────────────────────────────────────────
def nav_counts():
    """Per source: new items to look at + drafts waiting for review."""
    db = g.db
    rows = db.q("SELECT s.id, s.name, s.type, s.enabled, s.builtin, s.folder_id, s.last_error, "
                "(SELECT COUNT(*) FROM items i WHERE i.source_id=s.id AND i.status='new') AS items, "
                "(SELECT COUNT(*) FROM stories st WHERE st.source_id=s.id AND st.status='draft') AS drafts "
                "FROM sources s ORDER BY s.builtin DESC, s.name")
    tips_new = db.val("SELECT COUNT(*) FROM tips WHERE status='new'")
    for r in rows:
        r["waiting"] = tips_new if r["type"] == "tips" else r["items"] + r["drafts"]
    subs = db.val("SELECT COUNT(*) FROM submissions WHERE status='waiting'")
    total = sum(r["waiting"] for r in rows) + subs
    return {"sources": rows, "builtins": [r for r in rows if r["builtin"]], "total": total,
            "from_sources": sum(r["waiting"] for r in rows if not r["builtin"]),
            "source_problems": sum(1 for r in rows if not r["builtin"] and r["enabled"] and r["last_error"]), "tips_new": tips_new, "subs": subs,
            "developing": db.val("SELECT COUNT(*) FROM stories WHERE status='developing'"),
            "moderation": db.val("SELECT COUNT(*) FROM comments WHERE status='held'")
            + db.val("SELECT COUNT(DISTINCT target || target_id) FROM flags WHERE status='open'"),
            "partners": db.val("SELECT COUNT(*) FROM orgs WHERE status='pending' OR (status='approved' AND needs_look=1)"),
            "callit": db.val("SELECT COUNT(*) FROM predictions WHERE story_id IS NOT NULL AND status='open' "
                             "AND closes_at <= ?", (now(),))}


@bp.before_request
def remember_site_url():
    if g.get("user") and g.get("db") and not settings.get(g.db, "site_url") and request.host_url.startswith("https://"):
        settings.put(g.db, "site_url", request.host_url.rstrip("/"))


@bp.context_processor
def admin_ctx():
    if g.get("user"):
        return {"nav": nav_counts(), "TYPES": sources.TYPES, "TRUST": sources.TRUST}
    return {}


def login_user(u, remember=True):
    keep = {k: session[k] for k in ("mid", "mpv") if k in session}
    session.clear()
    session.update(keep)
    session["uid"] = u["id"]
    session["pwv"] = u["password_hash"][-12:]
    session.permanent = remember
    g.db.update("users", u["id"], last_login=now())


def uid():
    return g.user["id"] if g.get("user") else None


def safe_next(target):
    return target if target and target.startswith("/admin") and not target.startswith("//") else url_for(".overview")


# ── setup wizard ────────────────────────────────────────
SETUP_STARTERS = [
    ("police", "Police department news releases", "rss", "Public Safety"),
    ("city", "City hall news", "rss", "Government"),
    ("schools", "School district news", "rss", "Schools"),
    ("weather", "Weather alerts for your county", "weather", "Weather"),
    ("events", "Library or parks events calendar", "calendar", "Events"),
]


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    db = g.db
    step = int(request.args.get("step", 1))
    if g.setup_done and not g.user:
        return redirect(url_for(".login"))
    if g.setup_done and step == 1:
        step = 2
    if step > 1 and not (g.user and g.user["role"] == "owner"):
        return redirect(url_for(".setup", step=1))
    error = None
    code_required = bool(os.environ.get("SETUP_CODE"))

    if request.method == "POST":
        f = request.form
        if step == 1:
            if rate_limited(db, "setup:" + ip_hash(), 10, 3600):
                error = "Too many attempts. Wait an hour and try again."
            elif code_required and not pysecrets.compare_digest(f.get("setup_code", ""), os.environ["SETUP_CODE"]):
                error = "That setup code doesn't match the one in your install script."
            elif password_problem(f.get("password")):
                error = password_problem(f.get("password"))
            elif f.get("password") != f.get("password2"):
                error = "The passwords don't match."
            elif "@" not in f.get("email", ""):
                error = "Enter a valid email address."
            else:
                with db.tx():
                    if db.val("SELECT 1 FROM users WHERE role='owner'"):
                        abort(400, "Setup was already completed.")
                    new_id = db.insert("users", email=f["email"].strip(), name=f.get("name", "").strip() or "Owner",
                                       password_hash=hash_password(f["password"]), role="owner", created_at=now())
                login_user(db.one("SELECT * FROM users WHERE id=?", (new_id,)))
                util.activity(db, new_id, "created owner account")
                return redirect(url_for(".setup", step=2))
        elif step == 2:
            for k in ("site_name", "town", "county", "state", "timezone"):
                settings.put(db, k, settings.coerce(settings.FIELDS[k], f.get(k)))
            if not weather.cfg(db)["weather_locations"] and f.get("town") and f.get("state"):
                try:  # set up the weather page for the town straight away
                    hits = weather.geocode(", ".join(x for x in (f["town"], f.get("state")) if x))
                    if hits:
                        loc = {"slug": util.slugify(f["town"], 40), "name": f["town"].strip()[:60],
                               "lat": hits[0]["lat"], "lon": hits[0]["lon"]}
                        weather.save_cfg(db, weather_locations=[loc])
                except Exception as e:
                    util.log.info("weather setup skipped: %s", e)
            if request.files.get("logo") and request.files["logo"].filename:
                name = util.save_image(request.files["logo"], max_side=800)
                if name:
                    settings.put(db, "logo", name)
            return redirect(url_for(".setup", step=3))
        elif step == 3:
            if f.get("ai_key", "").strip():
                set_secret(db, "setting:ai_key", f["ai_key"].strip())
            settings.put(db, "ai_cap", settings.coerce(settings.FIELDS["ai_cap"], f.get("ai_cap")))
            return redirect(url_for(".setup", step=4))
        elif step == 4:
            for key, label, typ, cat in SETUP_STARTERS:
                if not f.get("use_" + key):
                    continue
                url = f.get("url_" + key, "").strip()
                if typ in ("rss", "calendar") and not url:
                    continue
                t = sources.TYPES[typ]
                db.insert("sources", name=label, type=typ, trust="official", category=cat,
                          interval_min=t["default_interval"], config=json.dumps({"url": url} if url else {}),
                          approval="always", created_at=now())
            util.activity(db, uid(), "finished setup")
            flash("Your newsroom is ready. Sources are checked within a few minutes.")
            return redirect(url_for(".overview"))
    return render_template("admin/setup.html", step=step, error=error, code_required=code_required,
                           starters=SETUP_STARTERS, settings_=settings, ai_set=has_secret(db, "setting:ai_key"),
                           bare=True)


# ── login ───────────────────────────────────────────────
@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        db = g.db
        if rate_limited(db, "login:" + ip_hash(), 8, 900):
            error = "Too many attempts. Wait 15 minutes and try again."
        else:
            u = db.one("SELECT * FROM users WHERE email=? AND active=1", (request.form.get("email", "").strip(),))
            if u and check_password(u["password_hash"], request.form.get("password", "")):
                login_user(u, remember=bool(request.form.get("remember")))
                return redirect(safe_next(request.args.get("next")))
            error = "Wrong email or password."
    return render_template("admin/login.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for(".login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required()
def account():
    db = g.db
    error = None
    if request.method == "POST":
        f = request.form
        if not check_password(g.user["password_hash"], f.get("current", "")):
            error = "Your current password is wrong."
        elif f.get("new") and password_problem(f.get("new")):
            error = password_problem(f.get("new"))
        else:
            fields = {"name": f.get("name", "").strip() or g.user["name"]}
            if f.get("new"):
                fields["password_hash"] = hash_password(f["new"])
            db.update("users", g.user["id"], **fields)
            if f.get("new"):
                login_user(db.one("SELECT * FROM users WHERE id=?", (g.user["id"],)))
            flash("Saved.")
            return redirect(url_for(".account"))
    return render_template("admin/account.html", error=error, staff=community.staff_member(db, g.user))


# ── overview ────────────────────────────────────────────
@bp.route("/")
@login_required()
def overview():
    db = g.db
    ready, ai_problem = ai.status(db)
    srcs = db.q("SELECT * FROM sources WHERE enabled=1 AND builtin=0")
    broken = [s for s in srcs if s["last_error"]]
    waiting = db.q("SELECT s.id, s.name, s.type, s.trust, s.last_checked, "
                   "(SELECT COUNT(*) FROM items i WHERE i.source_id=s.id AND i.status='new') AS items, "
                   "(SELECT COUNT(*) FROM stories st WHERE st.source_id=s.id AND st.status='draft') AS drafts "
                   "FROM sources s ORDER BY s.builtin DESC, s.name")
    for w in waiting:
        if w["type"] == "tips":
            w["items"] = db.val("SELECT COUNT(*) FROM tips WHERE status='new'")
    start_today = datetime.now(util.tz(db)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc).isoformat(timespec="seconds")
    return render_template(
        "admin/overview.html", waiting=waiting, broken=broken, sources_total=len(srcs),
        drafts=db.val("SELECT COUNT(*) FROM stories WHERE status='draft'"),
        developing=db.val("SELECT COUNT(*) FROM stories WHERE status='developing'"),
        subs=db.val("SELECT COUNT(*) FROM submissions WHERE status='waiting'"),
        flagged=db.val("SELECT COUNT(*) FROM stories WHERE status='draft' AND checklist != '[]'"),
        tips_new=db.val("SELECT COUNT(*) FROM tips WHERE status='new'"),
        tips_photos=db.val("SELECT COUNT(*) FROM tips WHERE status='new' AND photos != '[]'"),
        published_today=db.val("SELECT COUNT(*) FROM stories WHERE status='published' AND published_at >= ?",
                               (start_today,)),
        recent=db.q("SELECT * FROM stories WHERE status='published' ORDER BY published_at DESC LIMIT 5"),
        scheduled=db.val("SELECT COUNT(*) FROM stories WHERE status='scheduled'"),
        spend=ai.month_spend(db), cap=float(settings.get(db, "ai_cap") or 0), ai_ready=ready, ai_problem=ai_problem,
        last_check=db.val("SELECT MAX(last_checked) FROM sources"),
        new_items=db.val("SELECT COUNT(*) FROM items WHERE status='new'"))


@bp.route("/check-now", methods=["POST"])
@login_required("editor")
def check_now():
    g.db.run("UPDATE sources SET last_checked=NULL WHERE enabled=1")
    flash("Checking all sources now. New drafts appear here within a minute or two.")
    return redirect(request.referrer or url_for(".overview"))


# ── sources ─────────────────────────────────────────────
@bp.route("/sources")
@login_required()
def sources_list():
    db = g.db
    fid = request.args.get("folder", type=int)
    q = request.args.get("q", "").strip()[:80]
    tree = folders.flat(db)
    where, args = ["1=1"], []
    if fid is not None:
        if fid == folders.UNSORTED:
            where.append("s.folder_id IS NULL AND s.builtin=0")
        else:
            ids = folders.descendants(db, fid)
            where.append(f"s.folder_id IN ({','.join('?' * len(ids))})")
            args += ids
    if q:
        where.append("(s.name LIKE ? OR s.config LIKE ?)")
        args += [f"%{q}%"] * 2
    rows = db.q(f"SELECT s.* FROM sources s WHERE {' AND '.join(where)} ORDER BY s.builtin DESC, s.name COLLATE NOCASE",
                args)
    labels = {f["id"]: f["label"] for f in tree}
    for r in rows:
        r["folder_label"] = labels.get(r["folder_id"], "")
    counts = {}
    for r in db.q("SELECT folder_id, COUNT(*) AS n FROM sources WHERE builtin=0 GROUP BY folder_id"):
        counts[r["folder_id"]] = r["n"]
    for f in tree:  # count what's inside, subfolders included
        f["n"] = sum(counts.get(x, 0) for x in folders.descendants(db, f["id"]))
    current = next((f for f in tree if f["id"] == fid), None)
    return render_template("admin/sources.html", rows=rows, tree=tree, fid=fid, q=q, current=current,
                           unsorted=counts.get(None, 0), total=db.val("SELECT COUNT(*) FROM sources"))


@bp.route("/sources/organize", methods=["POST"])
@login_required("editor")
def sources_organize():
    db = g.db
    f = request.form
    a = f.get("action", "")
    back = request.referrer or url_for(".sources_list")
    try:
        if a == "new_folder":
            new = folders.create(db, f.get("name"), f.get("parent_id", type=int))
            flash(f"Folder “{f.get('name', '').strip()}” made.")
            back = url_for(".sources_list", folder=new)
        elif a == "rename_folder":
            name = f.get("name", "").strip()[:80]
            if name:
                db.run("UPDATE source_folders SET name=? WHERE id=?", (name, f.get("id", type=int)))
        elif a == "move_folder":
            folders.move_folder(db, f.get("id", type=int), f.get("parent_id", type=int))
            flash("Folder moved.")
        elif a == "delete_folder":
            folders.delete(db, f.get("id", type=int))
            flash("Folder removed. Anything that was in it moved up a level.")
            back = url_for(".sources_list")
        elif a in ("move", "pause", "resume"):
            ids = [int(x) for x in f.getlist("sid") if x.isdigit()]
            if not ids:
                raise ValueError("Tick the sources first.")
            marks = ",".join("?" * len(ids))
            if a == "move":
                to = f.get("to", type=int)
                if to is None or (to and not db.val("SELECT 1 FROM source_folders WHERE id=?", (to,))):
                    raise ValueError("Pick the folder to move them to.")
                db.run(f"UPDATE sources SET folder_id=? WHERE builtin=0 AND id IN ({marks})", (to or None, *ids))
                name = db.val("SELECT name FROM source_folders WHERE id=?", (to,)) if to else "Unsorted"
                flash(f"Moved {len(ids)} source{'s' if len(ids) != 1 else ''} to {name}.")
            else:
                db.run(f"UPDATE sources SET enabled=? WHERE id IN ({marks})", (1 if a == "resume" else 0, *ids))
                flash(f"{'Resumed' if a == 'resume' else 'Paused'} {len(ids)} source{'s' if len(ids) != 1 else ''}.")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(back)


def _source_form_values(src=None):
    f = request.form
    typ = src["type"] if src else f.get("type")
    t = sources.TYPES.get(typ)
    if not t or t["stage"] > 1 or t.get("builtin"):
        abort(400, "That source type isn't available yet.")
    cfg, secret = {}, None
    for fld in t["fields"]:
        v = f.get("cfg_" + fld["key"], "")
        if fld["type"] == "secret":
            secret = v.strip() or None
        elif fld["type"] == "chips":
            cfg[fld["key"]] = [x.strip() for x in v.replace(",", "\n").splitlines() if x.strip()]
        else:
            cfg[fld["key"]] = v.strip()
    vals = {
        "name": f.get("name", "").strip() or t["label"],
        "type": typ,
        "trust": f.get("trust") if f.get("trust") in sources.TRUST else "official",
        "category": f.get("category") or "Local News",
        "interval_min": max(5, int(f.get("interval_min") or t.get("default_interval", 30))),
        "approval": "auto_high" if f.get("approval") == "auto_high" else "always",
        "config": json.dumps(cfg),
        "folder_id": f.get("folder_id", type=int) or None,
    }
    if vals["trust"] != "official":
        vals["approval"] = "always"  # only official sources may auto-publish
    return vals, cfg, secret


@bp.route("/sources/new", methods=["GET", "POST"])
@bp.route("/sources/<int:sid>/edit", methods=["GET", "POST"])
@login_required("editor")
def source_edit(sid=None):
    db = g.db
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,)) if sid else None
    if sid and not src:
        abort(404)
    if src and src["builtin"]:
        return redirect(url_for(".queue", sid=sid))
    if request.method == "POST":
        vals, cfg, secret = _source_form_values(src)
        if src:
            db.update("sources", sid, **{k: v for k, v in vals.items() if k != "type"})
            util.activity(db, uid(), "edited source", f"source:{sid}", vals["name"])
        else:
            sid = db.insert("sources", created_at=now(), **vals)
            util.activity(db, uid(), "added source", f"source:{sid}", vals["name"])
        if secret:
            set_secret(db, f"source:{sid}:password", secret)
        flash("Source saved. It will be checked within a minute or two.")
        db.update("sources", sid, last_checked=None)
        return redirect(url_for(".queue", sid=sid))
    typ = src["type"] if src else request.args.get("type", "rss")
    if typ not in sources.TYPES:
        typ = "rss"
    cfg = loads(src["config"], {}) if src else {}
    return render_template("admin/source_edit.html", src=src, typ=typ, cfg=cfg, folder_list=folders.flat(db),
                           pick_folder=request.args.get("folder", type=int),
                           has_pw=bool(src) and has_secret(db, f"source:{sid}:password"),
                           categories=settings.get(db, "categories"))


@bp.route("/sources/test", methods=["POST"])
@login_required("editor")
def source_test():
    db = g.db
    sid = request.form.get("id", type=int)
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,)) if sid else None
    try:
        vals, cfg, secret = _source_form_values(src)
    except Exception as e:  # abort() from bad type
        return jsonify(ok=False, error=getattr(e, "description", "Choose a source type."))
    fake = {"id": sid, "type": vals["type"], "name": vals["name"], "trust": vals["trust"],
            "config": json.dumps(cfg), "error_count": 0}
    if secret is None and src:
        secret = sources.secret_for(db, src)
    try:
        found = sources.run_fetch(db, fake, config=sources.config_for(db, fake), secret=secret or "", test=True)
    except Exception as e:
        return jsonify(ok=False, error=sources.friendly_error(e))
    return jsonify(ok=True, count=len(found), newest=[i["title"] for i in found[:3]])


@bp.route("/sources/<int:sid>/toggle", methods=["POST"])
@login_required("editor")
def source_toggle(sid):
    s = g.db.one("SELECT * FROM sources WHERE id=?", (sid,)) or abort(404)
    g.db.update("sources", sid, enabled=0 if s["enabled"] else 1)
    util.activity(g.db, uid(), "paused source" if s["enabled"] else "resumed source", f"source:{sid}", s["name"])
    return redirect(request.referrer or url_for(".sources_list"))


@bp.route("/sources/<int:sid>/delete", methods=["POST"])
@login_required("editor")
def source_delete(sid):
    s = g.db.one("SELECT * FROM sources WHERE id=?", (sid,)) or abort(404)
    if s["builtin"]:
        abort(400, "Built-in sources can be paused but not deleted.")
    g.db.run("DELETE FROM secrets WHERE key=?", (f"source:{sid}:password",))
    g.db.run("DELETE FROM sources WHERE id=?", (sid,))
    util.activity(g.db, uid(), "deleted source", f"source:{sid}", s["name"])
    flash(f"Deleted “{s['name']}”. Its published stories stay on the site.")
    return redirect(url_for(".sources_list"))


# ── approval pages ──────────────────────────────────────
TABS = [("developing", "In development", "developing"), ("drafts", "Drafts", "draft"),
        ("scheduled", "Scheduled", "scheduled"),
        ("published", "Published", "published"), ("rejected", "Rejected", "rejected")]
ITEM_TABS = {"new": "new", "ignored": "ignored", "skipped": "skipped"}


@bp.route("/queue/all")
@bp.route("/queue/<int:sid>")
@bp.route("/queue/folder/<int:fid>")
@login_required()
def queue(sid=None, fid=None):
    db = g.db
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,)) if sid else None
    if sid and not src:
        abort(404)
    if src and src["type"] == "tips":
        return redirect(url_for(".tips"))
    folder = None
    if fid is not None:
        if fid == folders.UNSORTED:
            folder = {"id": 0, "name": "Unsorted", "path": [], "subfolders": []}
        else:
            f = db.one("SELECT * FROM source_folders WHERE id=?", (fid,)) or abort(404)
            folder = {**f, "path": folders.ancestors(db, fid)[:-1],
                      "subfolders": db.q("SELECT * FROM source_folders WHERE parent_id=? ORDER BY name COLLATE NOCASE",
                                         (fid,))}
        folder["sources"] = db.q("SELECT * FROM sources WHERE folder_id IS ? AND builtin=0 ORDER BY name COLLATE NOCASE",
                                 (fid or None,))
        ids = folders.source_ids(db, fid) or [0]
        where, args = f"source_id IN ({','.join('?' * len(ids))})", list(ids)
    else:
        where, args = ("source_id=?", [sid]) if sid else ("source_id IN (SELECT id FROM sources WHERE builtin=0)", [])
    counts = {k: db.val(f"SELECT COUNT(*) FROM stories WHERE {where} AND status=?", (*args, st)) for k, _, st in TABS}
    for k, st in ITEM_TABS.items():
        counts[k] = db.val(f"SELECT COUNT(*) FROM items WHERE {where} AND status=?", (*args, st))
    tab = request.args.get("tab") or ("new" if counts["new"] or not (counts["drafts"] or counts["developing"])
                                      else ("drafts" if counts["drafts"] else "developing"))
    rows, items = [], []
    if tab in ITEM_TABS:
        items = db.q(f"{pipeline.ITEM_SQL} WHERE {where.replace('source_id', 'i.source_id')} AND i.status=? "
                     f"ORDER BY i.id DESC LIMIT 300", (*args, ITEM_TABS[tab]))
        for it in items:
            it["extra_d"] = loads(it["extra"], {})
            it["trust"] = ai.item_trust(it)
    else:
        status = dict((k, st) for k, _, st in TABS).get(tab, "draft")
        order = "publish_at" if status == "scheduled" else ("published_at DESC" if status == "published" else "updated_at DESC")
        rows = [story_row(r) for r in db.q(
            f"SELECT st.*, so.name AS source_name FROM stories st LEFT JOIN sources so ON so.id=st.source_id "
            f"WHERE {where.replace('source_id', 'st.source_id')} AND st.status=? ORDER BY {order} LIMIT 200",
            (*args, status))]
    ready, ai_problem = ai.status(db)
    return render_template("admin/queue.html", src=src, folder=folder, tab=tab, rows=rows, items=items, counts=counts,
                           tabs=TABS, config=loads(src["config"], {}) if src else {},
                           ai_ready=ready, ai_problem=ai_problem)


def start_and_analyze(sid):
    """Run the first Develop step (the AI reads everything and asks questions), then show the Develop page."""
    try:
        pipeline.develop(g.db, sid)
    except ai.AIUnavailable as e:
        flash(f"{e} You can still add details and answers now, and write it when the AI is available.", "error")
    return redirect(url_for(".develop", sid=sid))


def research_and_write(sid):
    """Just write it: research, answer its own questions, and write, in one click. Lands on the draft."""
    try:
        if settings.get(g.db, "ai_research") and pipeline.needs_research(g.db, sid):
            pipeline.develop(g.db, sid)  # the source is thin: look things up first (cheap model)
        pipeline.write_story(g.db, sid, factcheck=bool(settings.get(g.db, "factcheck_default")))
    except ai.AIUnavailable as e:
        flash(f"{e} The story is saved In development.", "error")
        return redirect(url_for(".develop", sid=sid))
    flash("Researched and written. Read it through, tick the checklist, then publish.")
    return redirect(url_for(".story", sid=sid))


@bp.route("/items", methods=["POST"])
@login_required("editor")
def items_action():
    """Develop this story (one story from all ticked items), wire Publish as written, Ignore, or bring back."""
    db = g.db
    one = request.args.get("one", "")
    ids = [int(one)] if one.isdigit() else [int(i) for i in request.form.getlist("item") if i.isdigit()]
    action = request.args.get("action") or request.form.get("action")
    back = request.form.get("back") or url_for(".queue")
    if not back.startswith("/admin"):
        back = url_for(".queue")
    if not ids:
        flash("Tick at least one item first.", "error")
        return redirect(back)
    if action in ("develop", "write", "quick"):
        if len(ids) > 12:
            flash("Pick 12 items or fewer for one story.", "error")
            return redirect(back)
        sid, how = pipeline.start_development(db, ids, user_id=uid())
        if not sid:
            flash(how, "error")
            return redirect(back)
        if how == "event":
            return redirect(url_for(".story", sid=sid))
        util.activity(db, uid(), "developing", f"story:{sid}", f"from {len(ids)} item(s)")
        if action == "quick":
            return research_and_write(sid)
        return start_and_analyze(sid)
    if action == "wire":
        done = [pipeline.wire_story(db, i, user_id=uid()) for i in ids]
        flash(f"Published {sum(1 for d in done if d)} wire stor{'y' if len(ids) == 1 else 'ies'}.")
        return redirect(back)
    marks = ",".join("?" * len(ids))
    if action == "ignore":
        db.run(f"UPDATE items SET status='ignored' WHERE id IN ({marks}) AND status='new'", ids)
        flash(f"Ignored {len(ids)} item{'s' if len(ids) != 1 else ''}.")
    elif action == "restore":
        db.run(f"UPDATE items SET status='new', skip_reason=NULL WHERE id IN ({marks}) "
               f"AND status IN ('ignored','skipped')", ids)
        flash("Moved back to New items.")
    return redirect(back)


@bp.route("/manual", methods=["POST"])
@login_required("editor")
def manual_add():
    db = g.db
    src = db.one("SELECT * FROM sources WHERE type='manual'")
    from ..article import is_link
    text = request.form.get("text", "").strip()
    url = request.form.get("url", "").strip()
    if len(text) < 40 and not is_link(text) and not is_link(url):
        flash("Paste a link, or at least a few sentences.", "error")
        return redirect(url_for(".queue", sid=src["id"]))
    trust = request.form.get("trust") if request.form.get("trust") in sources.TRUST else "tip"
    sid = pipeline.develop_text(db, text, url=url if is_link(url) else "", title=request.form.get("title", "").strip(),
                                trust=trust, user_id=uid())
    util.activity(db, uid(), "manual add", f"story:{sid}")
    return research_and_write(sid) if request.form.get("quick") else start_and_analyze(sid)


# ── Develop this story ──────────────────────────────────
@bp.route("/develop/<int:sid>", methods=["GET", "POST"])
@login_required("editor")
def develop(sid):
    db = g.db
    s = story_row(db.one("SELECT * FROM stories WHERE id=?", (sid,))) or abort(404)
    if s["status"] not in ("developing", "draft"):
        return redirect(url_for(".story", sid=sid))
    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        if action == "abandon":
            if pipeline.abandon_development(db, sid):
                flash("Stopped. The items are back where they came from.")
                return redirect(url_for(".queue", sid=s["source_id"]) if s["source_id"] else url_for(".queue"))
            db.update("stories", sid, status="draft")
            flash("Back to the draft, unchanged.")
            return redirect(url_for(".story", sid=sid))
        dev = s["dev"]
        for i, q in enumerate(dev.get("questions", [])):
            q["a"] = f.get(f"a{i}", "").strip()[:3000]
        extra_q = f.get("new_question", "").strip()
        if extra_q:
            dev.setdefault("questions", []).append({"q": extra_q[:300], "a": f.get("new_answer", "").strip()[:3000]})
        dev["details"] = f.get("details", "").strip()[:10000]
        dev["pasted"] = f.get("pasted", "").strip()[:30000]
        dev["links"] = [x.strip() for x in f.get("links", "").split() if x.strip().startswith(("http://", "https://"))][:8]
        cats = settings.get(db, "categories") or []
        fields = {"dev": json.dumps(dev), "updated_at": now(), "updated_by": uid(),
                  "headline": util.text_only(f.get("headline", ""), 200) or s["headline"]}
        if f.get("story_type") in WD.TEMPLATES:
            fields["story_type"] = f["story_type"]
        if f.get("category") in cats:
            fields["category"] = f["category"]
            dev["category_chosen"] = True
        if f.get("scope") in ("local", "national", "world"):
            fields["scope"] = f["scope"]
            dev["scope_chosen"] = True
        fields["dev"] = json.dumps(dev)
        db.update("stories", sid, **fields)
        try:
            if action == "reanalyze":
                pipeline.develop(db, sid)
                flash("Looked again with what you added.")
            elif action == "write":
                pipeline.write_story(db, sid, factcheck=bool(f.get("factcheck")))
                util.activity(db, uid(), "wrote story", f"story:{sid}", fields["headline"])
                flash("Written. Read it through, tick the checklist, then publish.")
                return redirect(url_for(".story", sid=sid))
            else:
                if s["status"] == "developing":
                    flash("Saved as In development. Come back to it any time from the In development tab.")
                else:
                    flash("Saved.")
        except ai.AIUnavailable as e:
            flash(str(e), "error")
        return redirect(url_for(".develop", sid=sid))
    ready, ai_problem = ai.status(db)
    src = db.one("SELECT * FROM sources WHERE id=?", (s["source_id"],)) if s["source_id"] else None
    photos = s["dev"].get("photos", [])
    return render_template("admin/develop.html", s=s, dev=s["dev"], src=src, templates=WD.TEMPLATES,
                           categories=settings.get(db, "categories"), ai_ready=ready, ai_problem=ai_problem,
                           photos=photos, factcheck_default=settings.get(db, "factcheck_default"),
                           web_problem=settings.get(db, "web_search_problem") if settings.get(db, "ai_research") else "",
                           cost=ai.story_cost(db, sid))


# ── story review ────────────────────────────────────────
def _story_callit(db, sid):
    from .. import callit
    p = callit.for_story(db, sid)
    if p:
        p["spread"] = callit.distribution(db, p)
    return p


@bp.route("/story/<int:sid>/callit", methods=["POST"])
@login_required("editor")
def story_callit(sid):
    """Add a Call It to a story, settle it with the real answer, or call it off."""
    from .. import callit
    db = g.db
    db.one("SELECT id FROM stories WHERE id=?", (sid,)) or abort(404)
    f = request.form
    a = f.get("action")
    try:
        if a == "create":
            closes = util.local_input_to_utc(db, f.get("closes_at", ""))
            callit.create(db, sid, f.get("question", ""), f.get("kind", "number"), closes, unit=f.get("unit", ""),
                          lo=f.get("lo"), hi=f.get("hi"), step=f.get("step"),
                          choices=f.get("choices", "").replace("\r", "").split("\n"), user_id=uid())
            flash("Call It added. It shows at the end of the story.")
        elif a in ("resolve", "void"):
            p = callit.get(db, f.get("pid", type=int)) or abort(404)
            if a == "void":
                callit._clear(db, p)
                db.run("UPDATE predictions SET status='void' WHERE id=?", (p["id"],))
                flash("Call It removed. Any points it gave were taken back.")
            else:
                answer = f.get("answer", "").strip()
                if p["kind"] == "number":
                    float(answer)
                elif answer not in p["choices_list"]:
                    raise ValueError("Pick the right answer from the list.")
                res = callit.resolve(db, p, answer)
                winners = sum(1 for r in res if r[3])
                flash(f"Settled! {len(res)} guesses scored" + (f", {winners} winner{'s' if winners != 1 else ''}." if winners else "."))
    except ValueError as e:
        flash(str(e) if str(e) and not str(e).startswith("could not convert") else "Enter a number.", "error")
    return redirect(url_for(".story", sid=sid) + "#callit")


@bp.route("/story/<int:sid>", methods=["GET", "POST"])
@login_required()
def story(sid):
    db = g.db
    s = story_row(db.one("SELECT * FROM stories WHERE id=?", (sid,))) or abort(404)
    if s["status"] == "developing":
        return redirect(url_for(".develop", sid=sid))
    src = db.one("SELECT * FROM sources WHERE id=?", (s["source_id"],)) if s["source_id"] else None
    if request.method == "POST":
        return _story_post(db, s)
    tip = db.one("SELECT * FROM tips WHERE id=?", (s["tip_id"],)) if s["tip_id"] else None
    sub = db.one("SELECT * FROM submissions WHERE id=?", (s["submission_id"],)) if s["submission_id"] else None
    member = db.one("SELECT * FROM members WHERE id=?", (s["member_id"],)) if s["member_id"] else None
    photos = loads(tip["photos"], []) if tip else []
    if sub:
        photos += [p for p in loads(sub["photos"], []) if p not in photos]
    fc = [f["text"] for f in s["factcheck"]]
    platforms = [(k, label, social.connected(db, k)) for k, label in settings.PLATFORMS]
    return render_template("admin/story.html", s=s, src=src, tip=tip, sub=sub, member=member,
                           categories=settings.get(db, "categories"), templates=WD.TEMPLATES,
                           subcats=settings.subcategories(db), call=_story_callit(db, sid),
                           body_marked=util.mark_phrases(s["body"], fc) if fc else s["body"],
                           platforms=platforms, share=s["social"], limits=social.LIMITS,
                           site_url_set=bool(settings.get(db, "site_url")),
                           publish_local=(util.local(db, s["publish_at"]).strftime("%Y-%m-%dT%H:%M")
                                          if s["publish_at"] else ""),
                           event_start_local=(util.local(db, s["event_start"]).strftime("%Y-%m-%dT%H:%M")
                                              if s["event_start"] else ""),
                           event_end_local=(util.local(db, s["event_end"]).strftime("%Y-%m-%dT%H:%M")
                                            if s["event_end"] else ""),
                           photos=photos, ai_ready=ai.status(db)[0], cost=ai.story_cost(db, sid))


def _story_post(db, s):
    f = request.form
    action = f.get("action", "save")
    arg = None
    if ":" in action:  # buttons that carry a value, like use_photo:<file> or retry_social:<platform>
        action, arg = action.split(":", 1)
    sid = s["id"]
    back = redirect(url_for(".story", sid=sid))
    if (action in ("approve", "schedule", "unpublish", "reject", "restore", "factcheck", "redevelop", "retry_social",
                   "use_photo", "revise", "undo") or s["status"] in ("published", "scheduled")) and not can("editor"):
        abort(403, "Reviewers can edit drafts but not publish or change live stories. Ask an editor.")
    if action == "restore" and s["status"] != "rejected":
        abort(400)
    fields = {
        "headline": util.text_only(f.get("headline", ""), 200) or s["headline"],
        "summary": util.text_only(f.get("summary", ""), 400),
        "body": util.clean_html(f.get("body", "")),
        "category": f.get("category") or s["category"],
        "subcategory": f.get("subcategory", "") if f.get("subcategory", "") in
        settings.subcategories(db, f.get("category") or s["category"]) else "",
        "byline": util.text_only(f.get("byline", ""), 120),
        "breaking": 1 if f.get("breaking") else 0,
        "featured": 1 if f.get("featured") else 0,
        "scope": f.get("scope") if f.get("scope") in ("local", "national", "world") else s["scope"],
        "comments_mode": f.get("comments_mode") if f.get("comments_mode") in ("open", "locked", "off") else s["comments_mode"],
        "updated_at": now(), "updated_by": uid(),
    }
    if s["member_id"] and s["credit"] == "byline":
        fields["edited_note"] = 1 if f.get("edited_note") else 0
    if f.get("share_form"):
        share = loads(db.val("SELECT social FROM stories WHERE id=?", (sid,)), {})
        share.pop("_none", None)
        for p, _label in settings.PLATFORMS:
            cur = share.get(p) or {}
            if cur.get("status") in ("posted", "pending"):
                continue
            text = util.text_only(f.get("share_text_" + p, ""), 2000)
            if f.get("share_" + p):
                share[p] = {**cur, "on": True, "text": text}
            elif cur:
                share[p] = {**cur, "on": False, "text": text}
            if s["status"] == "published" and share.get(p, {}).get("on"):
                share[p]["status"] = "pending"  # ticked after publishing: post it now
        fields["social"] = json.dumps(share)
    checked = set(f.getlist("checked"))
    fields["checklist"] = json.dumps([{"text": c["text"], "checked": str(i) in checked}
                                      for i, c in enumerate(s["checklist"])])
    if "video" in f:
        v = f.get("video", "").strip()
        if v and not util.video_embed(v):
            flash("That video link isn't one we can embed. Use a YouTube link, or a Facebook video or reel link.",
                  "error")
        else:
            fields["video"] = v or None
    if s["kind"] == "event":
        fields["event_start"] = util.local_input_to_utc(db, f.get("event_start")) or s["event_start"]
        fields["event_end"] = util.local_input_to_utc(db, f.get("event_end")) if f.get("event_end") else None
        fields["event_location"] = util.text_only(f.get("event_location", ""), 200)
    if f.get("remove_image"):
        fields["image"], fields["image_alt"] = None, None
    up = request.files.get("image")
    if up and up.filename:
        name = util.save_image(up)
        if not name:
            flash("That file isn't an image we can use (JPG or PNG under 12 MB).", "error")
            return back
        fields["image"], fields["image_alt"] = name, util.text_only(f.get("image_alt", ""), 200) or fields["headline"]
    elif f.get("image_alt") is not None and s["image"]:
        fields["image_alt"] = util.text_only(f.get("image_alt", ""), 200)
    if s["status"] == "published":
        fields["slug"] = s["slug"]  # keep links working after edits
        note = util.text_only(f.get("correction", ""), 1000)
        if note:
            fields["corrections"] = json.dumps(s["corrections"] + [{"at": now(), "text": note}])
    if s["status"] == "published" and s["image"] and s["image"].startswith("card-") and \
            fields["headline"] != s["headline"] and not fields.get("image"):
        fields["image"] = None  # headline changed: regenerate the headline graphic
    db.update("stories", sid, **fields)

    all_checked = all(str(i) in checked for i in range(len(s["checklist"])))
    if fields["featured"] and not s["featured"] and s["status"] == "published":
        community.on_featured(db, {**s, **fields})
    if action == "factcheck":
        try:
            flags = writing.fact_check(db, db.one("SELECT * FROM stories WHERE id=?", (sid,)))
            flash(f"Fact-check found {len(flags)} thing{'s' if len(flags) != 1 else ''} to check." if flags
                  else "Fact-check found nothing unsupported.")
        except ai.AIUnavailable as e:
            flash(str(e), "error")
        return back
    if action in ("revise", "undo"):
        if action == "undo":
            writing.undo_revision(db, db.one("SELECT * FROM stories WHERE id=?", (sid,)))
            flash("Undone.")
            return back
        note = f.get("note", "").strip()
        if not note:
            flash("Type what to add or change first.", "error")
            return back
        try:
            ok = writing.revise(db, db.one("SELECT * FROM stories WHERE id=?", (sid,)), note)
            flash("Updated. Check the changes; Undo puts it back." if ok else "The AI didn't return an update. "
                                                                              "Try rewording the note.", None if ok else "error")
        except ai.AIUnavailable as e:
            flash(str(e), "error")
        return back
    if action == "redevelop" and s["status"] == "draft":
        db.update("stories", sid, status="developing")
        return redirect(url_for(".develop", sid=sid))
    if action == "retry_social":
        share = loads(db.val("SELECT social FROM stories WHERE id=?", (sid,)), {})
        p = arg or f.get("platform")
        if p in share and share[p].get("status") == "failed":
            share[p]["status"] = "pending"
            db.update("stories", sid, social=json.dumps(share))
            flash("Trying again within a minute.")
        return back
    if action == "use_photo":
        photo = arg or f.get("photo")
        tip = db.one("SELECT * FROM tips WHERE id=?", (s["tip_id"],)) if s["tip_id"] else None
        sub = db.one("SELECT * FROM submissions WHERE id=?", (s["submission_id"],)) if s["submission_id"] else None
        owner = None
        if tip and photo in loads(tip["photos"], []):
            owner = tip["member_id"]
        elif sub and photo in loads(sub["photos"], []):
            owner = sub["member_id"]
        elif photo not in s["dev"].get("photos", []):
            abort(400)
        m = db.one("SELECT * FROM members WHERE id=?", (owner,)) if owner else None
        public = (sub["credit"] if sub else (tip["credit"] if tip else 1))
        db.update("stories", sid, image=photo, image_alt=fields["headline"], photo_member_id=owner,
                  photo_credit=(f"Photo: @{m['username']}" if m and public else "Photo: submitted"))
        flash("Photo added. It's credited on the article.")
        return back
    if action == "save":
        util.activity(db, uid(), "edited", f"story:{sid}", fields["headline"])
        if s["status"] == "published" and fields.get("image", "x") is None:
            from .. import graphics
            db.update("stories", sid, image=graphics.headline_card(db, {**s, **fields}), image_alt=fields["headline"])
        flash("Saved.")
    elif action in ("approve", "schedule"):
        if not all_checked:
            flash("Tick every item on the verification checklist before approving.", "error")
            return back
        if action == "schedule":
            when = util.local_input_to_utc(db, f.get("publish_at"))
            if not when or when <= now():
                flash("Pick a date and time in the future to schedule.", "error")
                return back
            db.update("stories", sid, status="scheduled", publish_at=when)
            util.activity(db, uid(), "scheduled", f"story:{sid}", fields["headline"])
            flash(f"Scheduled for {util.fmt_date(util.local(db, when))}.")
        else:
            pipeline.publish(db, sid, user_id=uid())
            flash("Published.")
        return redirect(url_for(".queue", sid=s["source_id"]) if s["source_id"] else url_for(".queue"))
    elif action == "reject":
        pipeline.unpublish(db, sid, reject=True, reason=util.text_only(f.get("reject_reason", ""), 300))
        util.activity(db, uid(), "rejected", f"story:{sid}", fields["headline"])
        flash("Rejected.")
        return redirect(url_for(".queue", sid=s["source_id"]) if s["source_id"] else url_for(".queue"))
    elif action == "unpublish":
        pipeline.unpublish(db, sid)
        util.activity(db, uid(), "unpublished", f"story:{sid}", fields["headline"])
        flash("Taken off the site and moved back to drafts.")
    elif action == "restore":
        db.update("stories", sid, status="draft")
        flash("Moved back to drafts.")
    return back


# ── tips ────────────────────────────────────────────────
@bp.route("/tips")
@login_required()
def tips():
    db = g.db
    tab = request.args.get("tab", "new")
    rows = db.q("SELECT * FROM tips WHERE status=? ORDER BY id DESC LIMIT 200", (tab,))
    for r in rows:
        r["photos"] = loads(r["photos"], [])
    sel_id = request.args.get("id", type=int) or (rows[0]["id"] if rows else None)
    sel = next((r for r in rows if r["id"] == sel_id), None)
    if not sel and sel_id:
        sel = db.one("SELECT * FROM tips WHERE id=?", (sel_id,))
        if sel:
            sel["photos"] = loads(sel["photos"], [])
    counts = {t: db.val("SELECT COUNT(*) FROM tips WHERE status=?", (t,)) for t in ("new", "used", "dismissed")}
    src = db.one("SELECT * FROM sources WHERE type='tips'")
    return render_template("admin/tips.html", rows=rows, sel=sel, tab=tab, counts=counts, src=src)


@bp.route("/tips/<int:tid>", methods=["POST"])
@login_required()
def tip_action(tid):
    db = g.db
    t = db.one("SELECT * FROM tips WHERE id=?", (tid,)) or abort(404)
    action = request.form.get("action")
    if action in ("story", "develop", "quick"):
        if not can("editor"):
            abort(403)
        if t["status"] != "new":
            flash("This tip was already used or dismissed.", "error")
            return redirect(url_for(".tips", id=tid))
        sid = pipeline.develop_tip(db, t, user_id=uid())
        util.activity(db, uid(), "tip → developing", f"story:{sid}")
        return research_and_write(sid) if action == "quick" else start_and_analyze(sid)
    if not can("editor"):
        abort(403, "Ask an editor to do this.")
    if action == "dismiss" and t["status"] == "new":
        db.update("tips", tid, status="dismissed")
    elif action == "restore" and t["status"] == "dismissed":
        db.update("tips", tid, status="new")
    elif action == "block":
        if t["ip_hash"]:
            db.run("INSERT OR IGNORE INTO blocked(ip_hash, created_at) VALUES(?,?)", (t["ip_hash"], now()))
            db.run("UPDATE tips SET status='dismissed' WHERE ip_hash=? AND status='new'", (t["ip_hash"],))
        flash("Blocked. Future tips from this sender are discarded.")
    util.activity(db, uid(), f"tip {action}", f"tip:{tid}")
    return redirect(url_for(".tips", tab=request.args.get("tab", "new")))


@bp.route("/uploads/<path:name>")
@login_required()
def private_upload(name):
    """Tip photos are private until used in a published story."""
    from ..db import UPLOADS
    p = (UPLOADS / name).resolve()
    if not str(p).startswith(str(UPLOADS.resolve())) or not p.exists():
        abort(404)
    return send_file(p)


@bp.route("/tips/<int:tid>/photo-to-story/<int:sid>", methods=["POST"])
@login_required("editor")
def tip_photo_to_story(tid, sid):
    db = g.db
    t = db.one("SELECT * FROM tips WHERE id=?", (tid,)) or abort(404)
    photo = request.form.get("photo")
    if photo not in loads(t["photos"], []):
        abort(400)
    db.update("stories", sid, image=photo, image_alt=request.form.get("alt") or "Photo submitted by a reader")
    flash("Photo added to the story. Make sure you have the sender's permission to publish it.")
    return redirect(url_for(".story", sid=sid))


# ── settings ────────────────────────────────────────────
@bp.route("/settings")
@login_required("owner")
def settings_home():
    return redirect(url_for(".settings_section", section="publication"))


@bp.route("/settings/<section>", methods=["GET", "POST"])
@login_required("owner")
def settings_section(section):
    db = g.db
    sec = next((s for s in settings.SECTIONS if s["id"] == section), None) or abort(404)
    if request.method == "POST":
        for fld in sec["fields"]:
            k, t = fld["key"], fld["type"]
            if t == "secret":
                if request.form.get(k + "__remove"):
                    settings.put(db, k, "")
                elif request.form.get(k, "").strip():
                    settings.put(db, k, request.form[k].strip())
            elif t == "image":
                if request.form.get(k + "__remove"):
                    settings.put(db, k, "")
                up = request.files.get(k)
                if up and up.filename:
                    name = util.save_image(up, max_side=800)
                    if not name:
                        flash("That logo isn't an image we can use.", "error")
                        continue
                    settings.put(db, k, name)
            elif t == "chips":
                settings.put(db, k, settings.coerce(fld, request.form.getlist(k) or request.form.get(k + "__text", "")))
            else:
                settings.put(db, k, settings.coerce(fld, request.form.get(k)))
        util.activity(db, uid(), "changed settings", section)
        flash("Settings saved.")
        return redirect(url_for(".settings_section", section=section))
    return render_template("admin/settings.html", sec=sec, values=settings.all_values(db),
                           sections=settings.SECTIONS, later=settings.LATER, section=section,
                           spend=ai.month_spend(db) if section == "ai" else None,
                           locked=writing.locked_rules(db) if section == "writing" else "")


@bp.route("/settings/test-email", methods=["POST"])
@login_required("owner")
def test_email():
    ok = util.send_email(g.db, g.user["email"], "Test email from your newsroom", "Outgoing email works.")
    flash("Test email sent to " + g.user["email"] if ok else "Couldn't send. Check the server, port, username and password.",
          None if ok else "error")
    return redirect(url_for(".settings_section", section="system"))


@bp.route("/settings/people", methods=["GET", "POST"])
@login_required("owner")
def people():
    db = g.db
    temp = None
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "add":
            email = f.get("email", "").strip()
            if "@" not in email or db.val("SELECT 1 FROM users WHERE email=?", (email,)):
                flash("Enter a new, valid email address.", "error")
            else:
                temp = pysecrets.token_urlsafe(9)
                db.insert("users", email=email, name=f.get("name", "").strip() or email.split("@")[0],
                          password_hash=hash_password(temp), role=f.get("role") if f.get("role") in ROLES else "editor",
                          created_at=now())
                util.activity(db, uid(), "added user", email)
                util.send_email(db, email, f"You've been added to {settings.get(db, 'site_name')}",
                                f"Log in at {request.host_url}admin/login with this email and the temporary password:\n\n"
                                f"{temp}\n\nChange it under Account after logging in.")
        else:
            u = db.one("SELECT * FROM users WHERE id=?", (f.get("id", type=int),)) or abort(404)
            if u["id"] == g.user["id"]:
                flash("You can't change your own role or access here.", "error")
            elif action == "role" and f.get("role") in ROLES:
                db.update("users", u["id"], role=f["role"])
                util.activity(db, uid(), "changed role", u["email"], f["role"])
            elif action == "toggle":
                db.update("users", u["id"], active=0 if u["active"] else 1)
                util.activity(db, uid(), "deactivated user" if u["active"] else "reactivated user", u["email"])
            elif action == "reset":
                temp = pysecrets.token_urlsafe(9)
                db.update("users", u["id"], password_hash=hash_password(temp))
                util.activity(db, uid(), "reset password", u["email"])
        if not temp:
            return redirect(url_for(".people"))
    return render_template("admin/people.html", users=db.q("SELECT * FROM users ORDER BY id"), roles=ROLES,
                           temp=temp, sections=settings.SECTIONS, later=settings.LATER, section="people")


@bp.route("/settings/activity")
@login_required("owner")
def activity_log():
    rows = g.db.q("SELECT a.*, u.name AS user_name FROM activity a LEFT JOIN users u ON u.id=a.user_id "
                  "ORDER BY a.id DESC LIMIT 300")
    return render_template("admin/activity.html", rows=rows, sections=settings.SECTIONS, later=settings.LATER,
                           section="activity")


@bp.route("/settings/backups", methods=["GET", "POST"])
@login_required("owner")
def backups():
    from ..worker import make_backup, prune_backups
    if request.method == "POST":
        p = make_backup("manual")
        prune_backups(int(settings.get(g.db, "backup_keep") or 7))
        util.activity(g.db, uid(), "made backup", p.name)
        flash("Backup created. Download it and keep it somewhere safe.")
        return redirect(url_for(".backups"))
    files = sorted(BACKUPS.glob("*.zip"), reverse=True)
    usage = g.db.q("SELECT substr(at,1,7) AS month, COUNT(*) AS calls, ROUND(SUM(cost),2) AS cost "
                   "FROM ai_usage GROUP BY month ORDER BY month DESC LIMIT 12")
    by_step = g.db.q("SELECT purpose, COUNT(*) AS calls, SUM(searches) AS searches, SUM(input_tokens) AS tin, "
                     "ROUND(SUM(cost),2) AS cost FROM ai_usage WHERE at >= ? GROUP BY purpose ORDER BY cost DESC",
                     (datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat(),))
    stories = g.db.val("SELECT COUNT(DISTINCT story_id) FROM ai_usage WHERE story_id IS NOT NULL AND at >= ?",
                       (datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat(),))
    return render_template("admin/backups.html", files=files, usage=usage, by_step=by_step, stories=stories,
                           sections=settings.SECTIONS,
                           later=settings.LATER, section="backups")


@bp.route("/settings/backups/<name>")
@login_required("owner")
def backup_download(name):
    p = (BACKUPS / name).resolve()
    if p.parent != BACKUPS.resolve() or not p.exists() or p.suffix != ".zip":
        abort(404)
    return send_file(p, as_attachment=True)


@bp.app_template_global()
def ai_key_set():
    return has_secret(g.db, "setting:ai_key") if g.get("db") else False



# ── weather settings ────────────────────────────────────
@bp.route("/weather", methods=["GET", "POST"])
@login_required("owner")
def weather_settings():
    db = g.db
    c = weather.cfg(db)
    results, query = [], ""
    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        locs = list(c["weather_locations"])
        if action == "search":
            query = f.get("q", "").strip()
            if query:
                try:
                    results = weather.geocode(query)
                    if not results:
                        flash("No places found. Try the town and state, like “Riverton, Iowa”.", "error")
                except Exception as e:
                    flash(sources.friendly_error(e), "error")
        elif action == "add":
            try:
                lat, lon = round(float(f["lat"]), 4), round(float(f["lon"]), 4)
                assert -90 <= lat <= 90 and -180 <= lon <= 180
            except (KeyError, ValueError, AssertionError):
                flash("That location's coordinates aren't valid.", "error")
                return redirect(url_for(".weather_settings"))
            name = util.text_only(f.get("name", ""), 60) or "Weather"
            slug = util.slugify(name, 40)
            while any(l_["slug"] == slug for l_ in locs):
                slug += "-2"
            locs.append({"slug": slug, "name": name, "lat": lat, "lon": lon})
            weather.save_cfg(db, weather_locations=locs)
            ok = weather.refresh(db, locs[-1])
            flash(f"Added {name}." + ("" if ok else " The forecast couldn't be loaded yet; see the message below."))
            util.activity(db, uid(), "added weather location", name)
            return redirect(url_for(".weather_settings"))
        elif action == "remove":
            slug = f.get("slug")
            weather.save_cfg(db, weather_locations=[l_ for l_ in locs if l_["slug"] != slug])
            db.run("DELETE FROM weather WHERE slug=?", (slug,))
            return redirect(url_for(".weather_settings"))
        elif action in ("up", "rename"):
            i = next((n for n, l_ in enumerate(locs) if l_["slug"] == f.get("slug")), None)
            if i is not None and action == "up" and i > 0:
                locs[i - 1], locs[i] = locs[i], locs[i - 1]
            if i is not None and action == "rename":
                locs[i]["name"] = util.text_only(f.get("name", ""), 60) or locs[i]["name"]
            weather.save_cfg(db, weather_locations=locs)
            return redirect(url_for(".weather_settings"))
        elif action == "refresh":
            n = weather.refresh_if_due(db, force=True)
            flash(f"Updated {n} of {len(locs)} location(s).", None if n == len(locs) else "error")
            return redirect(url_for(".weather_settings"))
        else:
            weather.save_cfg(
                db, weather_enabled=bool(f.get("weather_enabled")),
                weather_units="C" if f.get("weather_units") == "C" else "F",
                weather_alert_banner=f.get("weather_alert_banner") if f.get("weather_alert_banner") in
                ("none", "severe", "all") else "severe",
                **{k: bool(f.get(k)) for k in ("weather_show_current", "weather_show_hourly", "weather_show_daily",
                                               "weather_show_radar", "weather_show_alerts", "weather_home_strip")})
            util.activity(db, uid(), "changed weather settings")
            flash("Weather settings saved.")
            return redirect(url_for(".weather_settings"))
    status = {r["slug"]: r for r in db.q("SELECT slug, updated_at, error FROM weather")}
    suggest = ", ".join(x for x in (settings.get(db, "town"), settings.get(db, "state")) if x)
    return render_template("admin/weather.html", c=weather.cfg(db), status=status, results=results,
                           query=query or suggest, sections=settings.SECTIONS, later=settings.LATER, section="weather")


from . import admin_community  # noqa: E402,F401  (registers the community pages on this blueprint)
from . import admin_sports  # noqa: E402,F401  (and the sports pages)
from . import admin_content  # noqa: E402,F401  (and the content finder)
