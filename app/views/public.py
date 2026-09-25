"""The public website and installable app."""
import io
import json
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from .. import callit, community, graphics, settings, specials, sports, util, weather
from ..db import UPLOADS, loads, now, story_row
from ..security import flask_secret_key, ip_hash, rate_limited

bp = Blueprint("public", __name__)

PAGES = {"about": ("About us", "page_about"), "contact": ("Contact", "page_contact"),
         "corrections": ("Corrections policy", "page_corrections"), "privacy": ("Privacy policy", "page_privacy"),
         "terms": ("Terms of use", "page_terms"), "ai": ("How we use AI", "page_ai")}


def published(where="1=1", args=(), limit=20, offset=0, order="published_at DESC"):
    return [story_row(r) for r in g.db.q(
        f"SELECT * FROM stories WHERE status='published' AND kind='story' AND {where} "
        f"ORDER BY {order} LIMIT ? OFFSET ?", (*args, limit, offset))]


def upcoming_events(limit=6):
    return g.db.q("SELECT * FROM stories WHERE status='published' AND kind='event' AND "
                  "COALESCE(event_end, event_start) >= ? ORDER BY event_start LIMIT ?", (now(), limit))


def calendar_items(limit=6, game_days=14, max_games=None):
    """Events and upcoming games together, soonest first, in one shape for the templates."""
    db = g.db
    items = [{"sort": e["event_start"] or "", "when": event_when(e), "title": e["headline"],
              "place": e["event_location"], "url": url_for("public.article", slug=e["slug"]), "kind": "event"}
             for e in upcoming_events(limit)]
    if settings.get(db, "events_show_games"):
        t = datetime.now(timezone.utc)
        rows = db.q(f"{sports.GAME_SQL} WHERE g.status IN ('scheduled','live','postponed') AND g.starts_at >= ? "
                    "AND g.starts_at < ? ORDER BY g.starts_at LIMIT 300",
                    ((t - timedelta(hours=3)).isoformat(timespec="seconds"),
                     (t + timedelta(days=game_days)).isoformat(timespec="seconds")))
        games = sports.dedupe(db, sports.decorate(db, rows))[:max_games] if max_games is not None else \
            sports.dedupe(db, sports.decorate(db, rows))
        for gm in games:
            when = gm["day_label"] + ("" if gm["time_tbd"] else ", " + gm["time_label"].replace("AM", "a.m.").replace("PM", "p.m."))
            items.append({"sort": gm["starts_at"], "when": when + (" · " + gm["status_label"] if gm["status"] != "scheduled" else ""),
                          "title": f"{gm['team']} {'at' if gm['home'] == 'away' else 'vs.'} {gm['opponent']}",
                          "place": gm["location"] or ("Home" if gm["home"] == "home" else ""), "kind": "game",
                          "url": url_for("sports.game", gid=gm["id"])})
    items.sort(key=lambda x: x["sort"])
    return items[:limit]


def cat_slug(name):
    return util.slugify(name)


@bp.app_template_global()
def category_url(name, sub=None):
    if sub:
        return url_for("public.category", slug=cat_slug(name), sub=cat_slug(sub))
    return url_for("public.category", slug=cat_slug(name))


@bp.app_template_global()
def media_url(name):
    return url_for("public.media", name=name) if name else ""


@bp.app_template_global()
def event_when(s):
    st, en = util.local(g.db, s.get("event_start")), util.local(g.db, s.get("event_end"))
    if not st:
        return ""
    txt = st.strftime("%a, %b %d").replace(" 0", " ")
    if st.hour or st.minute:
        txt += ", " + st.strftime("%I:%M %p").lstrip("0").replace("AM", "a.m.").replace("PM", "p.m.")
    if en and en.date() != st.date():
        txt += " to " + en.strftime("%b %d").replace(" 0", " ")
    return txt


@bp.app_template_global()
def md(text):
    return util.render_markdown(text)


@bp.app_template_global()
def today_local():
    return util.fmt_date(datetime.now(util.tz(g.db)), with_time=False)


@bp.app_template_global()
def byline_html(s):
    """Who a story is by, as the article shows it."""
    from markupsafe import Markup, escape
    if s.get("member_id") and s.get("credit") == "byline":
        if s.get("credit_public"):
            m = g.db.one("SELECT username FROM members WHERE id=?", (s["member_id"],))
            if m:
                org = g.db.one("SELECT name FROM orgs WHERE id=? AND status='approved'", (s["org_id"],)) \
                    if s.get("org_id") else None
                return Markup(f'By <a href="/u/{escape(m["username"])}"><strong>@{escape(m["username"])}</strong></a>'
                              + (f" for {escape(org['name'])}" if org else ""))
        return Markup("By <strong>a community member</strong>")
    return Markup(f"By <strong>{escape(s['byline'])}</strong>") if s.get("byline") else Markup("")


@bp.app_template_global()
def tip_credit(s):
    if s.get("member_id") and s.get("credit") == "tip" and s.get("credit_public"):
        return g.db.val("SELECT username FROM members WHERE id=?", (s["member_id"],))
    return None


@bp.app_template_global()
def my_vote(target, tid):
    m = g.get("member")
    return bool(m) and bool(g.db.val("SELECT 1 FROM votes WHERE member_id=? AND target=? AND target_id=?",
                                     (m["id"], target, tid)))


@bp.app_template_global()
def badge_list(mid, limit=3):
    return community.member_badges(g.db, mid)[:limit]


@bp.context_processor
def public_ctx():
    cats = settings.get(g.db, "categories") or []
    wc = weather.cfg(g.db)
    m = g.get("member")
    return {"categories": cats,
            "celebrate": _celebrate(m),
            # not on story pages: a name popping up beside an anonymous story could look like a byline
            "shout": g.db.one("SELECT * FROM shoutouts WHERE created_at >= ? ORDER BY id DESC LIMIT 1",
                              (_hours_ago(24),)) if request.endpoint != "public.article" else None,
            "member_notices": community.unread_notices(g.db, m["id"]) if m else [],
            "needs_confirm": community.needs_confirm(g.db, m) if m else False,
            "weather_on": wc["weather_enabled"] and bool(wc["weather_locations"]),
            "weather_alert": weather.banner_alert(g.db),
            "breaking": g.db.one("SELECT * FROM stories WHERE status='published' AND breaking=1 AND published_at >= ? "
                                 "ORDER BY published_at DESC", (_hours_ago(24),))}


def _celebrate(m):
    """A badge the member earned (or leveled up) since they last looked: shown once, with confetti."""
    if not m or request.path.startswith(("/static", "/media")):
        return None
    b = g.db.one("SELECT b.*, mb.tier FROM member_badges mb JOIN badges b ON b.id=mb.badge_id WHERE mb.member_id=? "
                 "AND mb.shown=0 AND b.active=1 ORDER BY b.secret DESC, mb.tier DESC LIMIT 1", (m["id"],))
    if not b:
        return None
    g.db.run("UPDATE member_badges SET shown=1 WHERE member_id=?", (m["id"],))
    b["tier_name"] = community.tier_name(b, b["tier"])
    b["holders"] = g.db.val("SELECT COUNT(*) FROM member_badges WHERE badge_id=?", (b["id"],))
    b["leveled"] = b["tier"] > 1
    return b


@bp.app_template_global()
def crowns_for(mid):
    """The crowns a member is wearing right now (loaded once per page)."""
    if "_crowns" not in g:
        from .. import crowns
        g._crowns = crowns.all_by_member(g.db)
    return g._crowns.get(mid, []) if mid else []


@bp.app_template_global()
def level_of(points):
    return community.level_of(points)


def _hours_ago(h):
    return datetime.fromtimestamp(time.time() - h * 3600, timezone.utc).isoformat(timespec="seconds")


# ── pages ───────────────────────────────────────────────
def local_only():
    return "" if settings.get(g.db, "home_mix_national") else " AND scope='local'"


@bp.route("/")
def home():
    db = g.db
    stories = published("featured=1" + local_only(), limit=22)
    if settings.get(db, "home_fill_latest") and len(stories) < 14:
        seen = [s["id"] for s in stories] or [0]
        stories += published(f"id NOT IN ({','.join('?' * len(seen))})" + local_only(), tuple(seen),
                             limit=14 - len(stories))
    top = stories[0] if stories else None
    wc = weather.cfg(db)
    strip = None
    if wc["weather_enabled"] and wc["weather_home_strip"] and wc["weather_locations"]:
        strip = weather.view(db, wc["weather_locations"][0])
        strip["loc"] = wc["weather_locations"][0]
    return render_template(
        "public/home.html", top=top, latest=stories[1:8], more=stories[8:14], wx=strip,
        events=calendar_items(6, game_days=3, max_games=3) if settings.get(db, "home_show_events") else [],
        national=published("scope!='local'", limit=5, order="featured DESC, published_at DESC")
        if settings.get(db, "home_show_national") else [],
        specials=specials.on_day(db, limit=4) if settings.get(db, "home_show_specials") else [],
        scores=sports.dedupe(db, sports.decorate(db, sports.strip(db)))
        if settings.get(db, "home_show_scores") else [],
        form_token=_form_token())


@bp.route("/weather")
def weather_page():
    c = weather.cfg(g.db)
    locs = c["weather_locations"]
    if not c["weather_enabled"] or not locs:
        abort(404)
    loc = next((x for x in locs if x["slug"] == request.args.get("loc")), locs[0])
    return render_template("public/weather.html", c=c, locs=locs, loc=loc, w=weather.view(g.db, loc))


@bp.route("/story/<slug>")
def article(slug):
    db = g.db
    s = story_row(db.one("SELECT * FROM stories WHERE slug=? AND status='published'", (slug,))) or abort(404)
    related = published("category=? AND id!=?", (s["category"], s["id"]), limit=4)
    s["public_cites"] = [c for c in s["cites"] if c.get("trust") != "tip"
                         and str(c.get("url", "")).startswith(("http://", "https://"))]
    sort = "new" if request.args.get("sort") == "new" else "top"
    comments_on = settings.get(db, "comments_on") and s["comments_mode"] != "off"
    org = db.one("SELECT * FROM orgs WHERE id=? AND status='approved'", (s["org_id"],)) if s["org_id"] else None
    org_more = published("org_id=? AND id!=?", (org["id"], s["id"]), limit=3) if org else []
    return render_template("public/article.html", s=s, related=related, sort=sort, org=org, org_more=org_more,
                           callit=callit_view(callit.for_story(db, s["id"], g.get("member"))),
                           comments=community.comment_tree(db, s["id"], g.get("member"), sort) if comments_on else [],
                           comments_on=comments_on, voted=my_vote("story", s["id"]),
                           can_links=community.level(db, g.get("member"), "priv_links"))


def callit_view(p):
    """A prediction plus what its box needs: the spread of guesses (once you've guessed or it's closed) and results."""
    if not p:
        return None
    db = g.db
    p["show_spread"] = bool(p["mine"]) or p["closed"]
    p["spread"] = callit.distribution(db, p) if p["show_spread"] else None
    p["results"] = callit.results(db, p) if p["status"] == "resolved" else []
    if p["kind"] == "number" and p["lo"] is not None:
        p["ticks"] = [p["lo"] + (p["hi"] - p["lo"]) * i / 4 for i in range(5)]
    return p


@bp.route("/callit/<int:pid>", methods=["POST"])
def callit_guess(pid):
    from .members import can_act
    db = g.db
    p = callit.get(db, pid) or abort(404)
    back = callit._link(db, p)
    problem = can_act()
    if problem:
        flash(problem, "error")
        return redirect(back)
    if rate_limited(db, f"callit:{g.member['id']}", 60, 86400):
        flash("That's a lot of guesses for one day.", "error")
        return redirect(back)
    p = callit.decorate(db, p, g.member)
    f = request.form
    try:
        value = (f.get("ours", ""), f.get("theirs", "")) if p["kind"] == "score" else f.get("value", "")
        shown = callit.guess(db, p, g.member, value)
        flash(f"🎯 Locked in: {shown.replace('-', '–') if p['kind'] == 'score' else shown}"
              f"{(' ' + p['unit']) if p['unit'] else ''}. We'll see who's closest!")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(back)


def listing(title, where="1=1", args=(), order="published_at DESC", template="public/list.html", **extra):
    page = max(1, request.args.get("page", 1, type=int))
    rows = published(where, args, limit=21, offset=(page - 1) * 20, order=order)
    return render_template(template, title=title, rows=rows[:20], page=page, has_more=len(rows) > 20, **extra)


@bp.route("/category/<slug>")
@bp.route("/category/<slug>/<sub>")
def category(slug, sub=None):
    """A section, with everything in it; or one of its subcategories (Sports → College)."""
    db = g.db
    name = next((c for c in settings.get(db, "categories") or [] if cat_slug(c) == slug), None) or abort(404)
    if name == "National & World":
        return redirect(url_for(".national_world"))
    subs = settings.subcategories(db, name)
    subname = None
    if sub:
        subname = next((x for x in subs if cat_slug(x) == sub), None) or abort(404)
    where, args = ("category=? AND subcategory=?", (name, subname)) if subname else ("category=?", (name,))
    page = max(1, request.args.get("page", 1, type=int))
    featured = published(f"{where} AND featured=1", args, limit=4) if page == 1 else []
    skip = [f["id"] for f in featured] or [0]
    rows = published(f"{where} AND id NOT IN ({','.join('?' * len(skip))})", (*args, *skip), limit=21,
                     offset=(page - 1) * 20)
    invites = settings.get(db, "category_invites") or {}
    scores = sports.dedupe(db, sports.decorate(db, sports.strip(db, 10))) \
        if name == "Sports" and page == 1 and not subname else None
    return render_template("public/list.html", title=subname or name, section=name, subs=subs, subname=subname,
                           scores=scores, featured=featured, rows=rows[:20], page=page, has_more=len(rows) > 20,
                           invite=invites.get(name), is_category=True)


@bp.route("/all")
def all_articles():
    return listing("All articles", intro="Every story, newest first.", list_tab="all")


@bp.route("/top")
def top():
    windows = {"day": 1, "week": 7, "month": 30, "all": None}
    t = request.args.get("t") if request.args.get("t") in windows else "week"
    if windows[t]:
        return listing("Top", "published_at >= ?", (_hours_ago(24 * windows[t]),),
                       order="upvotes DESC, comment_count DESC, published_at DESC", window=t, list_tab="top")
    return listing("Top", order="upvotes DESC, comment_count DESC, published_at DESC", window=t, list_tab="top")


@bp.route("/national")
def national():
    return listing("National", "scope='national'", order="featured DESC, published_at DESC", list_tab="national")


@bp.route("/world")
def world():
    return listing("World", "scope='world'", order="featured DESC, published_at DESC", list_tab="world")


@bp.route("/national-world")
def national_world():
    return listing("National & World", "scope!='local'", order="published_at DESC", list_tab="nw")


@bp.route("/events")
def events():
    return render_template("public/events.html", rows=calendar_items(limit=200))


@bp.route("/search")
def search():
    q = request.args.get("q", "").strip()[:100]
    rows = []
    if len(q) >= 2:
        like = f"%{q}%"
        rows = published("(headline LIKE ? OR summary LIKE ? OR body LIKE ?)", (like, like, like), limit=40)
    return render_template("public/list.html", title=f"Search: {q}" if q else "Search", rows=rows, q=q,
                           page=1, has_more=False, is_search=True)


@bp.route("/page/<name>")
def page(name):
    title, key = PAGES.get(name) or abort(404)
    if name == "about":
        title = "About " + (settings.get(g.db, "site_name") or "us")
    return render_template("public/page.html", title=title, name=name,
                           body=util.render_markdown(util.fill_page(g.db, settings.get(g.db, key))))


# ── tips ────────────────────────────────────────────────
def _signer():
    return URLSafeSerializer(flask_secret_key(), salt="tipform")


def _form_token():
    return _signer().dumps({"t": time.time()})


@bp.route("/tip", methods=["GET", "POST"])
def tip():
    db = g.db
    m = g.get("member")
    if not settings.get(db, "tips_enabled"):
        return render_template("public/page.html", title="Tips", body="<p>We're not taking tips online right now.</p>")
    error = None
    if request.method == "POST":
        f = request.form
        try:
            started = _signer().loads(f.get("ft", "")).get("t", 0)
        except BadSignature:
            started = 0
        ih = ip_hash()
        if f.get("website"):                        # honeypot: bots fill every field
            return redirect(url_for(".tip_thanks"))
        if time.time() - started < 3 or time.time() - started > 86400:
            error = "Please try sending that again."
        elif db.val("SELECT 1 FROM blocked WHERE ip_hash=?", (ih,)) or (m and not community.active(m)):
            return redirect(url_for(".tip_thanks"))  # quietly discard
        elif rate_limited(db, "tip:" + ih, 5, 3600):
            error = "You've sent several tips recently. Please wait a bit and try again."
        elif len(f.get("text", "").strip()) < 10:
            error = "Tell us a little more about what happened."
        elif request.files.getlist("photos") and any(p.filename for p in request.files.getlist("photos")) \
                and not f.get("photo_ok"):
            error = "Tick the box to confirm you took the photos or have permission to share them."
        else:
            photos = []
            if settings.get(db, "tips_photos"):
                for up in request.files.getlist("photos")[: int(settings.get(db, "tips_max_photos") or 5)]:
                    if up and up.filename:
                        name = util.save_image(up)
                        if name:
                            photos.append(name)
            anon = bool(f.get("anonymous")) and not m
            tid = db.insert("tips", text=f["text"].strip()[:5000], location=f.get("location", "").strip()[:300],
                            name="" if anon else (f"@{m['username']}" if m else f.get("name", "").strip()[:120]),
                            contact="" if anon else (m["email"] if m else f.get("contact", "").strip()[:200]),
                            anonymous=1 if anon else 0, photos=json.dumps(photos), ip_hash=ih, created_at=now(),
                            member_id=m["id"] if m else None, credit=0 if (m and f.get("credit") == "0") else 1)
            if not m:
                session["guest_tips"] = (session.get("guest_tips") or [])[-9:] + [tid]
            if settings.get(db, "notify_tips"):
                util.send_email(db, util.notify_address(db), f"New tip: {f['text'].strip()[:60]}",
                                f"{f['text'].strip()}\n\nLocation: {f.get('location', '')}\n\n"
                                f"Review it at {request.host_url}admin/tips")
            return redirect(url_for(".tip_thanks"))
    return render_template("public/tip.html", error=error, form_token=_form_token(), f=request.form)


@bp.route("/tip/thanks")
def tip_thanks():
    return render_template("public/tip_thanks.html", guest=not g.get("member") and bool(session.get("guest_tips")))


# ── files, feeds, app ───────────────────────────────────
@bp.route("/media/<name>")
def media(name):
    db = g.db
    if "/" in name or "\\" in name or name.startswith("."):
        abort(404)
    base = name[:-len("-share.jpg")] if name.endswith("-share.jpg") else None
    allowed = (name == settings.get(db, "logo")
               or db.val("SELECT 1 FROM stories WHERE image=? AND status='published'", (name,))
               or (base and db.val("SELECT 1 FROM stories WHERE image LIKE ? AND status='published'", (base + ".%",)))
               or db.val("SELECT 1 FROM members WHERE photo=? AND status!='banned'", (name,))
               or db.val("SELECT 1 FROM orgs WHERE logo=? AND status='approved'", (name,))
               or db.val("SELECT 1 FROM specials WHERE image=? AND status='live'", (name,)))
    p = UPLOADS / name
    if not allowed or not p.exists():
        abort(404)
    resp = send_file(p, max_age=86400 * 30)
    return resp


@bp.route("/feed.xml")
def feed():
    s = settings.all_values(g.db)
    base = request.host_url.rstrip("/")
    items = "".join(
        f"<item><title>{escape(r['headline'])}</title><link>{base}/story/{r['slug']}</link>"
        f"<guid>{base}/story/{r['slug']}</guid><description>{escape(r['summary'])}</description>"
        f"<category>{escape(r['category'])}</category>"
        f"<pubDate>{util.parse_iso(r['published_at']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for r in published(limit=30))
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(s["site_name"])}</title>'
           f"<link>{base}/</link><description>{escape(s.get('site_description') or s['site_name'])}</description>"
           f"{items}</channel></rss>")
    return Response(xml, mimetype="application/rss+xml")


@bp.route("/specials")
def specials_page():
    return render_template("public/specials.html", rows=specials.on_day(g.db), tab="specials")


@bp.route("/sitemap.xml")
def sitemap():
    base = request.host_url.rstrip("/")
    urls = [f"{base}/", f"{base}/events", f"{base}/all", f"{base}/specials", f"{base}/sports"] + \
        [f"{base}/story/{r['slug']}" for r in published(limit=5000)]
    body = "".join(f"<url><loc>{escape(u)}</loc></url>" for u in urls)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"{body}</urlset>", mimetype="application/xml")


@bp.route("/robots.txt")
def robots():
    return Response(f"User-agent: *\nDisallow: /admin/\nDisallow: /me\nDisallow: /submit\n"
                    f"Sitemap: {request.host_url}sitemap.xml\n", mimetype="text/plain")


@bp.route("/manifest.webmanifest")
def manifest():
    s = settings.all_values(g.db)
    name = s.get("app_name") or s["site_name"]
    return Response(json.dumps({
        "name": name, "short_name": (s.get("app_short_name") or name)[:12],
        "start_url": "/?app=1", "scope": "/", "display": "standalone",
        "background_color": "#ffffff", "theme_color": s["brand_color"],
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }), mimetype="application/manifest+json")


@bp.route("/icon-<int:size>.png")
def icon(size):
    if size not in (180, 192, 512):
        abort(404)
    buf = io.BytesIO()
    graphics.app_icon(g.db, size).save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=3600)


@bp.route("/sw.js")
def service_worker():
    from flask import current_app
    resp = current_app.send_static_file("public/sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/offline")
def offline():
    return render_template("public/page.html", title="You're offline",
                           body="<p>Check your connection. Stories you've opened recently are still available.</p>")
