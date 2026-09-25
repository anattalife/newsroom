"""Opening an article link and pulling out its readable text, so the AI has the facts to work from.

Used by Develop this story for items with a link, links you add, and links pasted into Manual add.
Paywalls and logins are never bypassed: if the page doesn't show the article, we say so.
"""
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .sources.http import FetchError, get

# Opening one page that an editor chose, the way a browser would.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
MIN_ARTICLE_CHARS = 400
MAX_ARTICLE_CHARS = 15000


def unwrap(url):
    """Google Alerts and similar links point through a redirect; return the real article address."""
    try:
        u = urlparse(url or "")
    except ValueError:
        return url
    if u.hostname and u.hostname.endswith("google.com") and u.path in ("/url", "/link"):
        q = parse_qs(u.query)
        for key in ("url", "q"):
            if q.get(key) and q[key][0].startswith(("http://", "https://")):
                return q[key][0]
    return url


def is_link(text):
    t = (text or "").strip()
    return bool(re.fullmatch(r"https?://\S+", t))


def extract(html):
    """(title, text) from an article page."""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg",
                     "figure", "iframe", "button"]):
        tag.decompose()
    candidates = soup.find_all("article") or soup.find_all("main") or [soup.body or soup]
    best, best_len = None, 0
    for c in candidates:
        n = sum(len(p.get_text(" ", strip=True)) for p in c.find_all("p"))
        if n > best_len:
            best, best_len = c, n
    paras = []
    for p in (best or soup).find_all(["p", "h2", "h3", "li"]):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(t) >= 40 and t not in paras:
            paras.append(t)
    text = "\n".join(paras)
    if len(text) < MIN_ARTICLE_CHARS:
        desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content") and desc["content"] not in text:
            text = (desc["content"].strip() + "\n" + text).strip()
    return title, text[:MAX_ARTICLE_CHARS]


def fetch(url):
    """Returns (title, text, note). note explains a partial result, or is '' when the full article was read."""
    url = unwrap(url)
    try:
        r = get(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
                              "Accept-Language": "en-US,en;q=0.9"})
    except FetchError as e:
        raise FetchError(f"Couldn't open the article: {e}") from e
    ctype = r.headers.get("Content-Type", "") if hasattr(r, "headers") else ""
    if ctype and "html" not in ctype:
        raise FetchError("That link isn't a web page.")
    html = r.content.decode(getattr(r, "encoding", None) or "utf-8", "replace")
    title, text = extract(html)
    if len(text) < MIN_ARTICLE_CHARS:
        return title, text, ("Only a short part of the article could be read (it may be behind a paywall). "
                             "Check the facts against the original.")
    return title, text, ""
