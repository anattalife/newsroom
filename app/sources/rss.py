"""RSS 2.0 / Atom feeds (standard library parser; tolerant of common feed quirks)."""
import html
import re
import xml.etree.ElementTree as ET

from .http import get

_TAG = re.compile(r"<[^>]+>")


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError("This address didn't return a news feed. Check it's the feed (RSS) link, not the web page.") from e
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "url": "", "text": "", "published": "", "guid": "", "html": ""}
        for ch in el:
            t = _local(ch.tag)
            if t == "title":
                d["title"] = clean(ch.text)
            elif t == "link":
                href = ch.get("href")
                if href and ch.get("rel", "alternate") == "alternate":
                    d["url"] = href
                elif not href and (ch.text or "").strip():
                    d["url"] = ch.text.strip()
            elif t in ("guid", "id"):
                d["guid"] = (ch.text or "").strip()
            elif t in ("description", "summary", "content", "encoded"):
                txt = clean(ch.text)
                if len(txt) > len(d["text"]):
                    d["text"] = txt
                if len(ch.text or "") > len(d["html"]):
                    d["html"] = ch.text or ""
            elif t in ("pubDate", "published", "updated", "date") and not d["published"]:
                d["published"] = (ch.text or "").strip()
        if d["title"] or d["text"]:
            items.append(d)
    return items


def fetch(source, config, secret, headers=None, keep_html=False):
    from ..article import unwrap
    items = parse(get(config["url"], headers=headers).content)
    for i in items:
        i["url"] = unwrap(i["url"])  # Google Alerts links point through a redirect
        i["key"] = i.pop("guid") or i["url"] or i["title"]
        html = i.pop("html")
        if keep_html and html:
            i["extra"] = {"html": html[:40000]}
    return items
