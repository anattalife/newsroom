"""Headline graphics for stories without a photo, and app icons. Pure Pillow, text-based by design:
the system never generates fake 'photos' of real events."""
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import settings
from .db import UPLOADS

FONTS = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf"],
    "regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf",
                "C:/Windows/Fonts/arial.ttf"],
    "serif": ["/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", "/Library/Fonts/Georgia Bold.ttf",
              "C:/Windows/Fonts/georgiab.ttf"],
}
BG_TOP, BG_BOTTOM = (22, 30, 44), (32, 44, 66)


def font(weight, size):
    for p in FONTS[weight]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def rgb(h, fallback=(179, 38, 30)):
    try:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _wrap(draw, text, fnt, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text, weight, max_w, max_h, start, minimum=24, spacing=1.18):
    size = start
    while size >= minimum:
        f = font(weight, size)
        lines = _wrap(draw, text, f, max_w)
        if len(lines) * size * spacing <= max_h:
            return f, lines, size
        size -= 2
    f = font(weight, minimum)
    return f, _wrap(draw, text, f, max_w)[: int(max_h // (minimum * spacing))], minimum


def headline_card(db, story, w=1200, h=630):
    brand = rgb(settings.get(db, "brand_color"))
    name = settings.get(db, "site_name") or ""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    pad = int(w * 0.06)
    # category pill
    cat = (story.get("category") or "Local News").upper()
    pf = font("bold", 26)
    tw = d.textlength(cat, font=pf)
    d.rounded_rectangle([pad, pad, pad + tw + 28, pad + 46], radius=8, fill=brand)
    d.text((pad + 14, pad + 8), cat, font=pf, fill="white")
    # headline
    top = pad + 46 + 36
    f, lines, size = _fit(d, story["headline"], "bold", w - 2 * pad, h - top - 120, start=66)
    y = top
    for ln in lines:
        d.text((pad, y), ln, font=f, fill="white")
        y += int(size * 1.18)
    # footer
    d.rectangle([0, h - 12, w, h], fill=brand)
    d.text((pad, h - pad - 30), name.upper(), font=font("bold", 26), fill="white")
    from .util import tz
    date = datetime.now(tz(db)).strftime("%b %d, %Y").replace(" 0", " ")
    fd = font("regular", 24)
    d.text((w - pad - d.textlength(date, font=fd), h - pad - 28), date, font=fd, fill=(178, 190, 210))
    fname = f"card-{story['id']}-{int(datetime.now().timestamp())}.jpg"
    img.save(UPLOADS / fname, quality=90)
    return fname


def app_icon(db, size):
    """App icon: the uploaded logo on the brand color, or a letter mark."""
    brand = rgb(settings.get(db, "brand_color"))
    img = Image.new("RGB", (size, size), brand)
    logo = settings.get(db, "logo")
    if logo and (UPLOADS / logo).exists():
        lg = Image.open(UPLOADS / logo).convert("RGBA")
        lg.thumbnail((int(size * 0.7), int(size * 0.7)))
        img.paste(lg, ((size - lg.width) // 2, (size - lg.height) // 2), lg)
    else:
        letter = (settings.get(db, "site_name") or "N").strip()[:1].upper()
        d = ImageDraw.Draw(img)
        f = font("serif", int(size * 0.62))
        bbox = d.textbbox((0, 0), letter, font=f)
        d.text(((size - (bbox[2] - bbox[0])) / 2 - bbox[0], (size - (bbox[3] - bbox[1])) / 2 - bbox[1]),
               letter, font=f, fill="white")
    return img


TIER_RGB = {"Bronze": (168, 103, 46), "Silver": (140, 150, 163), "Gold": (200, 150, 30), "Legend": (123, 63, 181)}
RARITY_RGB = {"common": (200, 150, 30), "rare": (31, 95, 173), "epic": (123, 63, 181), "legendary": (200, 150, 30)}


def badge_card(db, username, name, tier, rarity, description, secret=False, w=1200, h=630):
    """A picture of an earned badge, sized for Facebook link previews. Returns PNG bytes."""
    import io
    name_site = settings.get(db, "site_name") or ""
    brand = rgb(settings.get(db, "brand_color"))
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    ring = TIER_RGB.get(tier) or RARITY_RGB.get(rarity, (200, 150, 30))
    cx, cy, r = 300, h // 2 - 10, 170
    # ribbon tails, then the medal
    d.polygon([(cx - 90, cy + 90), (cx - 150, cy + 260), (cx - 95, cy + 230), (cx - 60, cy + 275), (cx - 20, cy + 120)],
              fill=brand)
    d.polygon([(cx + 90, cy + 90), (cx + 150, cy + 260), (cx + 95, cy + 230), (cx + 60, cy + 275), (cx + 20, cy + 120)],
              fill=brand)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ring)
    d.ellipse([cx - r + 22, cy - r + 22, cx + r - 22, cy + r - 22], fill=(255, 250, 238))
    letter = (name[:1] or "★").upper()
    lf = font("serif", 150)
    lw = d.textlength(letter, font=lf)
    d.text((cx - lw / 2, cy - 98), letter, font=lf, fill=ring)
    # words
    x = 540
    kicker = ("SECRET BADGE DISCOVERED" if secret else "BADGE EARNED") + f" · {rarity.upper()}"
    d.text((x, 120), kicker, font=font("bold", 26), fill=RARITY_RGB.get(rarity, (242, 221, 160)))
    f, lines, size = _fit(d, name, "serif", w - x - 60, 190, start=76, minimum=40)
    y = 168
    for ln in lines:
        d.text((x, y), ln, font=f, fill="white")
        y += int(size * 1.15)
    if tier:
        d.text((x, y + 6), tier.upper(), font=font("bold", 34), fill=ring)
        y += 56
    fd = font("regular", 28)
    for ln in _wrap(d, description, fd, w - x - 60)[:2]:
        d.text((x, y + 10), ln, font=fd, fill=(205, 212, 224))
        y += 38
    d.text((x, h - 118), f"@{username}", font=font("bold", 36), fill="white")
    d.rectangle([0, h - 12, w, h], fill=brand)
    d.text((x, h - 70), name_site.upper(), font=font("bold", 24), fill=(178, 190, 210))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
