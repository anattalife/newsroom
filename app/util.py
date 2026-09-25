"""Shared helpers: HTML cleaning, slugs, dates, markdown, images, email, activity log."""
import io
import logging
import re
import smtplib
import unicodedata
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import markdown as md
from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, ProcessingInstruction
from PIL import Image, ImageOps

from . import settings
from .db import UPLOADS, now

log = logging.getLogger("newsroom")

# ── HTML cleaning (source text and AI output are untrusted) ──
ALLOWED = {"p", "a", "strong", "em", "b", "i", "ul", "ol", "li", "h2", "h3", "h4", "blockquote", "br"}
DROP = ["script", "style", "iframe", "object", "embed", "form", "input", "button", "select", "textarea",
        "svg", "math", "template", "noscript", "meta", "link", "base", "frame", "frameset"]


def clean_html(html):
    soup = BeautifulSoup(html or "", "html.parser")
    # comments, doctypes and processing instructions can smuggle markup past a tag filter
    for node in soup.find_all(string=lambda t: isinstance(t, (Comment, Declaration, Doctype, ProcessingInstruction, CData))):
        node.extract()
    for tag in soup.find_all(DROP):
        if not tag.decomposed:
            tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        if tag.name not in ALLOWED:
            tag.unwrap()
        else:
            href = tag.get("href", "")
            tag.attrs = {}
            if tag.name == "a" and href.strip().lower().startswith(("http://", "https://", "mailto:")):
                tag.attrs = {"href": href.strip(), "rel": "nofollow noopener"}
    return str(soup).strip()


def text_only(html, limit=None):
    t = re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()
    return t[:limit] if limit else t


def render_markdown(text):
    return clean_html(md.markdown(text or "", extensions=["sane_lists"]))


def slugify(text, maxlen=70):
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t[:maxlen].rstrip("-") or "story")


def unique_slug(db, headline, story_id):
    base = slugify(headline)
    slug, n = base, 2
    while db.val("SELECT 1 FROM stories WHERE slug=? AND id!=?", (slug, story_id)):
        slug, n = f"{base}-{n}", n + 1
    return slug


# ── dates ───────────────────────────────────────────────
def tz(db):
    try:
        return ZoneInfo(settings.get(db, "timezone") or "America/Chicago")
    except Exception:
        return ZoneInfo("America/Chicago")


def parse_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def local(db, iso):
    d = parse_iso(iso)
    return d.astimezone(tz(db)) if d else None


def fmt_date(d, with_time=True):
    if not d:
        return ""
    s = d.strftime("%b %d, %Y").replace(" 0", " ")
    if with_time:
        s += ", " + d.strftime("%I:%M %p").lstrip("0").replace("AM", "a.m.").replace("PM", "p.m.")
    return s


def ago(iso):
    d = parse_iso(iso)
    if not d:
        return ""
    s = (datetime.now(timezone.utc) - d).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} hr ago"
    days = int(s // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def local_input_to_utc(db, value):
    """'2026-09-24T07:00' in the newsroom's time zone → UTC ISO string."""
    try:
        d = datetime.fromisoformat(value).replace(tzinfo=tz(db))
        return d.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


# ── images ──────────────────────────────────────────────
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 40_000_000  # about a 7,000 × 5,700 photo; stops 'decompression bomb' uploads


def save_image(file_storage, max_side=2000):
    """Validate, strip metadata (incl. GPS location), resize, save as JPEG/PNG. Returns filename or None."""
    data = file_storage.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            return None
        img.verify()
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception:
        return None
    img.thumbnail((max_side, max_side))
    keep_alpha = img.mode in ("RGBA", "LA", "P") and file_storage.filename.lower().endswith(".png")
    name = uuid.uuid4().hex + (".png" if keep_alpha else ".jpg")
    out = UPLOADS / name
    if keep_alpha:
        img.save(out, "PNG", optimize=True)  # re-encoding drops EXIF
    else:
        img.convert("RGB").save(out, "JPEG", quality=86, optimize=True)
    return name


# ── email ───────────────────────────────────────────────
def send_email(db, to, subject, body):
    from .security import get_secret
    host = settings.get(db, "smtp_host")
    if not host or not to:
        return False
    try:
        msg = EmailMessage()
        msg["From"] = settings.get(db, "smtp_from") or settings.get(db, "smtp_user")
        msg["To"] = to
        msg["Subject"] = re.sub(r"\s+", " ", subject).strip()[:150]
        msg.set_content(body)
        with smtplib.SMTP(host, int(settings.get(db, "smtp_port") or 587), timeout=20) as s:
            s.starttls()
            if settings.get(db, "smtp_user"):
                s.login(settings.get(db, "smtp_user"), get_secret(db, "setting:smtp_password"))
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning("email failed: %s", e)
        return False


def notify_address(db):
    return settings.get(db, "notify_to") or db.val("SELECT email FROM users WHERE role='owner' AND active=1 ORDER BY id")


# ── activity log ────────────────────────────────────────
def activity(db, user_id, action, target="", detail=""):
    db.insert("activity", user_id=user_id, action=action, target=str(target), detail=detail[:500], at=now())


def domain_of(url):
    from urllib.parse import urlparse
    try:
        host = urlparse(url or "").hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def mark_phrases(html, phrases):
    """Wrap each phrase (in the text only, never inside tags or links' addresses) in <mark> for display
    in the editor. The marks are never saved: clean_html removes them."""
    soup = BeautifulSoup(html or "", "html.parser")
    for ph in phrases:
        ph = (ph or "").strip()
        if len(ph) < 6:
            continue
        pat = re.compile(r"\s+".join(re.escape(w) for w in ph.split()), re.I)
        for node in list(soup.find_all(string=True)):
            if isinstance(node, Comment) or (node.parent and node.parent.name == "mark"):
                continue
            m = pat.search(str(node))
            if not m:
                continue
            text = str(node)
            mark = soup.new_tag("mark", attrs={"class": "fc"})
            mark.string = text[m.start():m.end()]
            node.replace_with(text[:m.start()], mark, text[m.end():])
            break
    return str(soup)


def plain_to_html(text, links=False, nofollow=True):
    """Member-written plain text (comments) → safe paragraphs, optionally with links."""
    from html import escape
    paras = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    out = []
    for p in paras:
        t = escape(p.strip()).replace("\n", "<br>")
        if links:
            t = re.sub(r"(https?://[^\s<]+[^\s<.,;:!?)\]'\"])",
                       lambda m: f'<a href="{m.group(1)}" rel="nofollow ugc noopener" target="_blank">{m.group(1)}</a>', t)
        out.append(f"<p>{t}</p>")
    return "".join(out)


# ── video embeds (YouTube and Facebook) ─────────────────
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_embed(url):
    """A YouTube or Facebook video link → {"provider", "src", "tall"} for an embedded player, or None."""
    from urllib.parse import parse_qs, quote, urlparse
    url = (url or "").strip()
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    host = host[4:] if host.startswith("www.") else host
    host = host[2:] if host.startswith("m.") else host
    path = u.path.rstrip("/")
    if host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"):
        vid, tall = None, False
        if host == "youtu.be":
            vid = path.lstrip("/").split("/")[0]
        elif path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
        else:
            parts = path.split("/")
            if len(parts) >= 3 and parts[1] in ("shorts", "live", "embed", "v"):
                vid, tall = parts[2], parts[1] == "shorts"
        if vid and _YT_ID.match(vid):
            return {"provider": "youtube", "src": f"https://www.youtube-nocookie.com/embed/{vid}", "tall": tall}
        return None
    if host in ("facebook.com", "web.facebook.com", "fb.watch") or host.endswith(".facebook.com"):
        is_video = host == "fb.watch" or "/videos/" in path + "/" or path.startswith(("/watch", "/reel")) \
            or "/reel/" in path
        if is_video:
            clean = f"https://{'fb.watch' if host == 'fb.watch' else 'www.facebook.com'}{u.path}" + \
                (f"?{u.query}" if u.query and path.startswith("/watch") else "")
            return {"provider": "facebook", "tall": "/reel" in path,
                    "src": "https://www.facebook.com/plugins/video.php?show_text=false&href=" + quote(clean, safe="")}
    return None


def fill_page(db, text):
    """Footer page text: fill in {site}, {town} and {area}, and leave out any [placeholder] paragraph the
    owner hasn't written yet (and its heading), so it never shows on the live site."""
    town = settings.get(db, "town") or ""
    county = (settings.get(db, "county") or "").strip()
    area = f"{county} County" if county and not county.lower().endswith("county") else (county or town)
    text = (text or "").replace("{site}", settings.get(db, "site_name") or "").replace("{town}", town) \
        .replace("{area}", area)
    blocks = [b for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
              if not re.fullmatch(r"\[[^\]]*\]", b.strip())]
    out = []
    for i, b in enumerate(blocks):
        is_head = b.lstrip().startswith("#")
        nxt = blocks[i + 1].lstrip() if i + 1 < len(blocks) else None
        if is_head and (nxt is None or nxt.startswith("#")):
            continue  # a heading with nothing left under it
        out.append(b)
    return "\n\n".join(out)
