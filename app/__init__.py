"""Flask application factory."""
import logging
import os
from datetime import timedelta

from flask import Flask, g, redirect, render_template, request, url_for

from . import db as dbm
from . import settings, util
from .security import can, check_csrf, csrf_token, flask_secret_key

VERSION = "2.13.1"


def create_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    dbm.init()
    app = Flask(__name__)
    app.secret_key = flask_secret_key()
    app.config.update(
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("NEWSROOM_HTTPS") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )

    @app.before_request
    def before():
        g.db = dbm.DB()
        g.user = None
        from flask import session
        uid = session.get("uid")
        if uid:
            u = g.db.one("SELECT * FROM users WHERE id=? AND active=1", (uid,))
            if u and session.get("pwv") == u["password_hash"][-12:]:  # logs out other sessions on password change
                g.user = u
        g.member = None
        mid = session.get("mid")
        if mid:
            m = g.db.one("SELECT * FROM members WHERE id=?", (mid,))
            if m and session.get("mpv") == m["password_hash"][-12:] and m["status"] != "banned":
                g.member = m
        if not g.member and g.user:
            g.member = g.db.one("SELECT * FROM members WHERE staff_user_id=?", (g.user["id"],))
        if g.member and request.endpoint and not request.endpoint.startswith(("static", "admin.")):
            from . import community
            community.touch(g.db, g.member)
        if request.endpoint and not request.endpoint.startswith("static"):
            check_csrf()
        g.setup_done = bool(g.db.val("SELECT 1 FROM users WHERE role='owner' AND active=1"))
        if not g.setup_done and request.endpoint not in (None, "static", "admin.setup"):
            return redirect(url_for("admin.setup"))

    @app.teardown_request
    def teardown(_exc):
        d = g.pop("db", None)
        if d:
            d.close()

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https://radar.weather.gov; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; script-src 'self' https://plausible.io; "
            "connect-src 'self' https://plausible.io; "
            "frame-src https://www.youtube-nocookie.com https://www.facebook.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return resp

    @app.context_processor
    def ctx():
        d = g.get("db")
        s = settings.all_values(d) if d else {}
        return {"S": s, "csrf_token": csrf_token, "can": can, "user": g.get("user"), "member": g.get("member"),
                "VERSION": VERSION}

    @app.template_filter("ago")
    def _ago(iso):
        return util.ago(iso)

    @app.template_filter("plain")
    def _plain(text, links=False):
        from markupsafe import Markup
        return Markup(util.plain_to_html(text, links=links))

    @app.template_global()
    def video_embed(url):
        return util.video_embed(url)

    @app.template_filter("localdate")
    def _localdate(iso, with_time=True):
        return util.fmt_date(util.local(g.db, iso), with_time)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def err(e):
        code = getattr(e, "code", 500)
        msg = {404: "We couldn't find that page.", 413: "That upload is too large."}.get(code, getattr(e, "description", ""))
        return render_template("public/error.html", code=code, message=msg), code

    from .views.admin import bp as admin_bp
    from .views.members import bp as members_bp
    from .views.public import bp as public_bp
    from .views.sports import bp as sports_bp
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(public_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(sports_bp)
    return app
