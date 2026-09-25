"""Posting published stories to Facebook, Instagram, Threads, Bluesky and X.

Each platform is connected once in the dashboard (Social accounts). When a story publishes, the worker posts it to
every ticked platform and records the result on the story: posted (with a link) or failed (with the reason).
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import secrets as pysecrets
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from PIL import Image

from . import settings, util
from .db import UPLOADS, loads, now
from .security import get_secret, set_secret

log = logging.getLogger("newsroom.social")

GRAPH = "https://graph.facebook.com"
THREADS = "https://graph.threads.net/v1.0"
TIMEOUT = 30

# platform: [(key, label, is_secret, help)]
FIELDS = {
    "facebook": [("fb_page_id", "Page ID", False, "Your Facebook Page's numeric ID (Page → About → Page transparency)."),
                 ("fb_token", "Page access token", True, "A long-lived Page access token from your Meta app. "
                                                          "See the guide for how to get one that doesn't expire.")],
    "instagram": [("ig_user_id", "Instagram account ID", False, "The Instagram Business account ID linked to your Page."),
                  ("ig_token", "Access token (optional)", True, "Leave blank to use the Facebook Page token.")],
    "threads": [("threads_user_id", "Threads user ID", False, ""),
                ("threads_token", "Threads access token", True, "Renewed automatically every week.")],
    "bluesky": [("bsky_handle", "Handle", False, "Like yournews.bsky.social"),
                ("bsky_password", "App password", True, "Bluesky → Settings → Privacy and security → App passwords."),
                ("bsky_service", "Server", False, "Leave as https://bsky.social unless you host your own.")],
    "x": [("x_api_key", "API key", True, "From your X developer app (Keys and tokens)."),
          ("x_api_secret", "API key secret", True, ""),
          ("x_access_token", "Access token", True, "Generate with Read and write permission."),
          ("x_access_secret", "Access token secret", True, "")],
}
LIMITS = {"facebook": 5000, "instagram": 2000, "threads": 500, "bluesky": 300, "x": 280}


class SocialError(Exception):
    pass


def val(db, key):
    secret = any(k == key and s for fl in FIELDS.values() for k, _, s, _ in fl)
    if secret:
        return get_secret(db, "social:" + key)
    v = settings.get(db, "social_" + key)
    if key == "bsky_service":
        return (v or "https://bsky.social").rstrip("/")
    return (v or "").strip() if isinstance(v, str) else (v or "")


def save(db, key, value):
    secret = any(k == key and s for fl in FIELDS.values() for k, _, s, _ in fl)
    if secret:
        set_secret(db, "social:" + key, value)
    else:
        settings.put(db, "social_" + key, value)


def connected(db, p):
    need = {"facebook": ["fb_page_id", "fb_token"], "instagram": ["ig_user_id"],
            "threads": ["threads_user_id", "threads_token"], "bluesky": ["bsky_handle", "bsky_password"],
            "x": ["x_api_key", "x_api_secret", "x_access_token", "x_access_secret"]}.get(p, ["-"])
    ok = all(val(db, k) for k in need)
    if p == "instagram":
        ok = ok and bool(val(db, "ig_token") or val(db, "fb_token"))
    return ok


def site_url(db):
    return (settings.get(db, "site_url") or "").rstrip("/")


def story_url(db, s):
    return f"{site_url(db)}/story/{s['slug']}"


def _err(r):
    try:
        d = r.json()
        msg = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else None
        msg = msg or d.get("message") or d.get("detail") or d.get("title") or d.get("error")
    except ValueError:
        msg = r.text[:200]
    return f"{r.status_code}: {msg}"


def _ok(r):
    if r.status_code >= 400:
        raise SocialError(_err(r))
    try:
        return r.json()
    except ValueError:
        return {}


def share_image(s):
    """A JPEG copy of the story image under 950 KB, for platforms that need JPEG or have size limits."""
    if not s.get("image"):
        return None
    src = UPLOADS / s["image"]
    if not src.exists():
        return None
    out = UPLOADS / (src.stem + "-share.jpg")
    if not out.exists():
        img = Image.open(src).convert("RGB")
        img.thumbnail((1600, 1600))
        q = 85
        while True:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, optimize=True)
            if buf.tell() < 950_000 or q <= 40:
                break
            q -= 10
        out.write_bytes(buf.getvalue())
    return out


def fit(text, limit, suffix=""):
    room = limit - len(suffix)
    t = (text or "").strip()
    if len(t) > room:
        t = t[:max(0, room - 1)].rsplit(" ", 1)[0] + "…"
    return t + suffix


# ── platforms ─────────────────────────────────────────
def post_facebook(db, s, text, link):
    d = _ok(requests.post(f"{GRAPH}/{val(db, 'fb_page_id')}/feed", timeout=TIMEOUT,
                          data={"message": fit(text, LIMITS["facebook"]), "link": link,
                                "access_token": val(db, "fb_token")}))
    return f"https://www.facebook.com/{d.get('id', '')}"


def post_instagram(db, s, text, link):
    img = share_image(s)
    if not img:
        raise SocialError("Instagram needs a photo or headline graphic.")
    token = val(db, "ig_token") or val(db, "fb_token")
    uid = val(db, "ig_user_id")
    caption = fit(text, LIMITS["instagram"] - 40, "\n\nFull story: link in bio.")
    d = _ok(requests.post(f"{GRAPH}/{uid}/media", timeout=TIMEOUT, data={
        "image_url": f"{site_url(db)}/media/{img.name}", "caption": caption, "access_token": token}))
    cid = d.get("id")
    for _ in range(10):  # Instagram fetches and processes the image first
        st = _ok(requests.get(f"{GRAPH}/{cid}", timeout=TIMEOUT,
                              params={"fields": "status_code", "access_token": token})).get("status_code")
        if st in (None, "FINISHED"):
            break
        if st == "ERROR":
            raise SocialError("Instagram couldn't use the image. Check that your site address in Settings is right.")
        time.sleep(3)
    d = _ok(requests.post(f"{GRAPH}/{uid}/media_publish", timeout=TIMEOUT,
                          data={"creation_id": cid, "access_token": token}))
    try:
        return _ok(requests.get(f"{GRAPH}/{d['id']}", timeout=TIMEOUT,
                                params={"fields": "permalink", "access_token": token})).get("permalink", "")
    except Exception:
        return "https://www.instagram.com/"


def post_threads(db, s, text, link):
    uid, token = val(db, "threads_user_id"), val(db, "threads_token")
    img = share_image(s)
    body = fit(text, LIMITS["threads"], "\n\n" + link)
    data = {"media_type": "IMAGE" if img else "TEXT", "text": body, "access_token": token}
    if img:
        data["image_url"] = f"{site_url(db)}/media/{img.name}"
    cid = _ok(requests.post(f"{THREADS}/{uid}/threads", timeout=TIMEOUT, data=data)).get("id")
    if img:
        time.sleep(5)  # Threads recommends a short wait for images to process
    pid = _ok(requests.post(f"{THREADS}/{uid}/threads_publish", timeout=TIMEOUT,
                            data={"creation_id": cid, "access_token": token})).get("id")
    try:
        return _ok(requests.get(f"{THREADS}/{pid}", timeout=TIMEOUT,
                                params={"fields": "permalink", "access_token": token})).get("permalink", "")
    except Exception:
        return "https://www.threads.net/"


def bsky_session(db):
    svc = val(db, "bsky_service")
    return svc, _ok(requests.post(f"{svc}/xrpc/com.atproto.server.createSession", timeout=TIMEOUT,
                                  json={"identifier": val(db, "bsky_handle"), "password": val(db, "bsky_password")}))


def post_bluesky(db, s, text, link):
    svc, sess = bsky_session(db)
    auth = {"Authorization": f"Bearer {sess['accessJwt']}"}
    external = {"uri": link, "title": s["headline"][:300], "description": (s["summary"] or "")[:300]}
    img = share_image(s)
    if img:
        blob = _ok(requests.post(f"{svc}/xrpc/com.atproto.repo.uploadBlob", timeout=TIMEOUT,
                                 headers={**auth, "Content-Type": "image/jpeg"}, data=img.read_bytes()))
        external["thumb"] = blob.get("blob")
    record = {"$type": "app.bsky.feed.post", "text": fit(text, LIMITS["bluesky"]),
              "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "langs": ["en"],
              "embed": {"$type": "app.bsky.embed.external", "external": external}}
    d = _ok(requests.post(f"{svc}/xrpc/com.atproto.repo.createRecord", timeout=TIMEOUT, headers=auth,
                          json={"repo": sess["did"], "collection": "app.bsky.feed.post", "record": record}))
    rkey = d.get("uri", "").rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{sess.get('handle') or val(db, 'bsky_handle')}/post/{rkey}"


def _oauth1(db, method, url):
    """OAuth 1.0a header for X (the body is JSON, so it isn't part of the signature)."""
    ck, cs = val(db, "x_api_key"), val(db, "x_api_secret")
    tk, ts = val(db, "x_access_token"), val(db, "x_access_secret")
    p = {"oauth_consumer_key": ck, "oauth_nonce": pysecrets.token_hex(16), "oauth_signature_method": "HMAC-SHA1",
         "oauth_timestamp": str(int(time.time())), "oauth_token": tk, "oauth_version": "1.0"}
    enc = lambda x: quote(str(x), safe="~")  # noqa: E731
    base_url, _, query = url.partition("?")
    params = dict(p)
    for pair in filter(None, query.split("&")):
        k, _, v = pair.partition("=")
        params[k] = v
    param_str = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(params.items()))
    base = "&".join([method.upper(), enc(base_url), enc(param_str)])
    sig = base64.b64encode(hmac.new(f"{enc(cs)}&{enc(ts)}".encode(), base.encode(), hashlib.sha1).digest()).decode()
    p["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(p.items()))


def post_x(db, s, text, link):
    url = "https://api.twitter.com/2/tweets"
    body = fit(text, LIMITS["x"] - 24) + " " + link  # X counts every link as 23 characters
    d = _ok(requests.post(url, timeout=TIMEOUT, json={"text": body},
                          headers={"Authorization": _oauth1(db, "POST", url)}))
    return f"https://x.com/i/web/status/{(d.get('data') or {}).get('id', '')}"


POSTERS = {"facebook": post_facebook, "instagram": post_instagram, "threads": post_threads,
           "bluesky": post_bluesky, "x": post_x}


def test(db, p):
    """(ok, message) — checks the connection without posting."""
    if not connected(db, p):
        return False, "Fill in every field first."
    try:
        if p == "facebook":
            d = _ok(requests.get(f"{GRAPH}/{val(db, 'fb_page_id')}", timeout=TIMEOUT,
                                 params={"fields": "name", "access_token": val(db, "fb_token")}))
            return True, f"Connected to the Page “{d.get('name', '?')}”."
        if p == "instagram":
            d = _ok(requests.get(f"{GRAPH}/{val(db, 'ig_user_id')}", timeout=TIMEOUT,
                                 params={"fields": "username",
                                         "access_token": val(db, "ig_token") or val(db, "fb_token")}))
            return True, f"Connected to @{d.get('username', '?')}."
        if p == "threads":
            d = _ok(requests.get(f"{THREADS}/me", timeout=TIMEOUT,
                                 params={"fields": "username", "access_token": val(db, "threads_token")}))
            return True, f"Connected to @{d.get('username', '?')} on Threads."
        if p == "bluesky":
            _, sess = bsky_session(db)
            return True, f"Connected to @{sess.get('handle')}."
        if p == "x":
            url = "https://api.twitter.com/2/users/me"
            d = _ok(requests.get(url, timeout=TIMEOUT, headers={"Authorization": _oauth1(db, "GET", url)}))
            return True, f"Connected to @{(d.get('data') or {}).get('username', '?')} on X."
    except SocialError as e:
        return False, f"The platform said: {e}"
    except requests.RequestException as e:
        return False, f"Couldn't reach the platform ({type(e).__name__})."
    return False, "Unknown platform."


def refresh_threads(db):
    """Threads tokens last 60 days; renew weekly while they still work."""
    if not connected(db, "threads"):
        return
    last = util.parse_iso(settings.get(db, "social_threads_refreshed"))
    if last and (datetime.now(timezone.utc) - last).days < 7:
        return
    try:
        d = _ok(requests.get("https://graph.threads.net/refresh_access_token", timeout=TIMEOUT,
                             params={"grant_type": "th_refresh_token", "access_token": val(db, "threads_token")}))
        if d.get("access_token"):
            save(db, "threads_token", d["access_token"])
        settings.put(db, "social_threads_refreshed", now())
    except Exception as e:
        log.warning("threads token refresh failed: %s", e)
        settings.put(db, "social_threads_refreshed", now())


def post_pending(db, poster=None):
    """Post every published story's pending platforms. Returns number of attempts."""
    n = 0
    if not site_url(db):
        return 0
    for s in db.q("SELECT * FROM stories WHERE status='published' AND social LIKE '%\"pending\"%' LIMIT 20"):
        link = story_url(db, s)
        failures = []
        for p, v in loads(s["social"], {}).items():
            if not isinstance(v, dict) or v.get("status") != "pending":
                continue
            n += 1
            try:
                if not connected(db, p):
                    raise SocialError("This account isn't connected any more (Social accounts).")
                url = (poster or POSTERS[p])(db, s, v.get("text") or s["social_text"] or s["headline"], link)
                result = {"status": "posted", "url": url, "error": "", "at": now()}
            except (SocialError, requests.RequestException, KeyError, OSError) as e:
                result = {"status": "failed", "error": str(e)[:300], "at": now()}
                failures.append((p, str(e)))
            with db.tx():  # re-read so an editor's change made while posting isn't lost
                cur = loads(db.val("SELECT social FROM stories WHERE id=?", (s["id"],)), {})
                cur.setdefault(p, {}).update(result)
                db.update("stories", s["id"], social=json.dumps(cur))
        if failures and settings.get(db, "notify_social"):
            util.send_email(db, util.notify_address(db), f"Social post failed: {s['headline'][:60]}",
                            "\n".join(f"{p}: {e}" for p, e in failures) + "\n\nOpen the story in the newsroom and "
                                                                          "click Try again, or check Social accounts.")
    return n
