"""Source types, and collecting from a source into the items table.

Each type declares the fields its Add/Edit form shows, so new types only need an entry here
and a fetch function. Types marked with a later stage appear in the form but disabled.
"""
import hashlib
import json
import logging

from .. import settings, util
from ..db import now
from ..security import get_secret
from . import calendar, email_inbox, rss, weather, wire
from .http import FetchError

log = logging.getLogger("newsroom.sources")


def _f(key, label, type="text", help="", **kw):
    return {"key": key, "label": label, "type": type, "help": help, **kw}


TYPES = {
    "rss": {"label": "News feed (RSS)", "hint": "Most agency and school websites", "stage": 1, "fetch": rss.fetch,
            "default_trust": "official", "default_interval": 30,
            "fields": [_f("url", "Feed address", "url", "Usually ends in /rss, /feed or .xml. Google Alerts can also give you a feed.")]},
    "weather": {"label": "Weather alerts", "hint": "National Weather Service", "stage": 1, "fetch": weather.fetch,
                "default_trust": "official", "default_interval": 10, "default_category": "Weather",
                "fields": [_f("state", "State (2 letters)", help="Leave blank to use Settings → Coverage area."),
                           _f("county", "County", help="Leave blank to use Settings → Coverage area.")]},
    "email": {"label": "Email inbox", "hint": "Press releases, newsletters", "stage": 1, "fetch": email_inbox.fetch,
              "default_trust": "official", "default_interval": 15,
              "fields": [_f("host", "Email server (IMAP)", help="Gmail: imap.gmail.com · Outlook: outlook.office365.com"),
                         _f("username", "Email address or username"),
                         _f("password", "Password", "secret", "For Gmail, create an App Password. Stored encrypted."),
                         _f("folder", "Folder", help="Usually INBOX."),
                         _f("official_domains", "Official senders", "chips",
                            "Email domains treated as official, such as rivertonpd.gov. Everyone else is treated as a tip.")]},
    "calendar": {"label": "Calendar", "hint": "Community events (.ics)", "stage": 1, "fetch": calendar.fetch,
                 "default_trust": "official", "default_interval": 360, "default_category": "Events",
                 "fields": [_f("url", "Calendar address", "url", "An .ics or iCal link from the library, parks or chamber calendar.")]},
    "wire": {"label": "Wire service", "hint": "National and world news you license", "stage": 1, "fetch": wire.fetch,
             "default_trust": "official", "default_interval": 30, "default_category": "National & World",
             "fields": [_f("url", "Feed address", "url", "The RSS or Atom feed link your wire provider gives you."),
                        _f("key_header", "Key header name", help="If your provider gives you an API key, the name "
                           "of the header it goes in, such as x-api-key or Authorization. Leave blank if the key is "
                           "already part of the feed address."),
                        _f("password", "Account key", "secret", "Stored encrypted. For Authorization headers, "
                           "include the word Bearer if your provider says to."),
                        _f("credit", "Credit line", help="Shown as the byline, such as The Associated Press."),
                        _f("scope", "National or world", "select", options=[("national", "National"),
                                                                             ("world", "World")])]},
    "manual": {"label": "Manual add", "hint": "Paste a post or article", "stage": 1, "builtin": True},
    "tips": {"label": "Reader tips", "hint": "Your tip form", "stage": 1, "builtin": True},
    "page": {"label": "Web page", "hint": "Pages without a feed", "stage": 3},
    "meeting": {"label": "Meeting portal", "hint": "Agendas and minutes", "stage": 3},
    "pdf": {"label": "PDF documents", "hint": "Agendas, budgets, logs", "stage": 3},
    "data": {"label": "Open data", "hint": "Permits, inspections", "stage": 3},
}
FETCHABLE = {k for k, t in TYPES.items() if t.get("fetch")}
TRUST = {"official": "Official", "tip": "Tip (unverified)", "outlet": "Other news outlet (tip only)"}


def config_for(db, source):
    cfg = json.loads(source.get("config") or "{}")
    if source["type"] == "weather":
        cfg.setdefault("state", "")
        cfg.setdefault("county", "")
        cfg["state"] = cfg["state"] or settings.get(db, "state")
        cfg["county"] = cfg["county"] or settings.get(db, "county")
    return cfg


def secret_for(db, source):
    return get_secret(db, f"source:{source['id']}:password") if source.get("id") else ""


def run_fetch(db, source, config=None, secret=None, test=False):
    t = TYPES[source["type"]]
    config = config if config is not None else config_for(db, source)
    secret = secret if secret is not None else secret_for(db, source)
    if source["type"] == "calendar":
        return calendar.fetch(source, config, secret, default_tz=util.tz(db))
    if source["type"] == "email":
        return email_inbox.fetch(source, config, secret, mark_read=not test)
    return t["fetch"](source, config, secret)


def friendly_error(e):
    if isinstance(e, (FetchError, ValueError)):
        return str(e)
    log.exception("source error")
    return "Something went wrong reading this source. " + type(e).__name__


def collect(db, source):
    """Fetch a source and store new items. Returns list of new item ids. Records status on the source."""
    try:
        found = run_fetch(db, source)
    except Exception as e:
        db.update("sources", source["id"], last_checked=now(), last_error=friendly_error(e),
                  error_count=source["error_count"] + 1)
        return []
    new_ids = []
    for it in found:
        key = f"{source['id']}|{it.get('key') or it.get('url') or it.get('title')}"
        h = hashlib.sha256(key.encode()).hexdigest()
        if db.val("SELECT 1 FROM items WHERE hash=?", (h,)):
            continue
        extra = dict(it.get("extra") or {})
        if it.get("trust"):
            extra["trust"] = it["trust"]
        new_ids.append(db.insert("items", source_id=source["id"], hash=h, url=it.get("url", ""),
                                 title=it.get("title", "")[:500], text=it.get("text", "")[:12000],
                                 published=it.get("published", ""), extra=json.dumps(extra), fetched_at=now()))
    fields = {"last_checked": now(), "last_error": None, "error_count": 0}
    if new_ids:
        fields["last_new_item"] = now()
    db.update("sources", source["id"], **fields)
    return new_ids


def first_run_mark_old(db, source_id):
    """On a brand-new feed, don't draft its whole back catalogue: keep the 5 newest items.
    Feeds list newest first, and items are stored in feed order."""
    ids = [r["id"] for r in db.q("SELECT id FROM items WHERE source_id=? AND status='new' ORDER BY id", (source_id,))]
    for i in ids[5:]:
        db.update("items", i, status="skipped", skip_reason="Older item from before this source was added")
    return ids[:5]
