"""From collected items to stories in development, drafts, and published stories."""
import json
import logging
import uuid
from datetime import datetime, timezone

from . import ai, community, graphics, settings, sources, util, writing
from .db import loads, now

log = logging.getLogger("newsroom.pipeline")


# ── creating stories ─────────────────────────────────────
def create_story(db, source_id, fields, kind="story", status="draft", user_id=None, **extra):
    t = now()
    cols = dict(
        source_id=source_id, kind=kind, status=status,
        headline=fields.get("headline") or "Untitled", summary=fields.get("summary", ""), body=fields.get("body", ""),
        category=fields.get("category") or "Local News", byline=fields.get("byline", settings.get(db, "byline") or ""),
        confidence=fields.get("confidence", "medium"),
        checklist=json.dumps(fields.get("checklist", [])), cites=json.dumps(fields.get("cites", [])),
        event_start=fields.get("event_start"), event_end=fields.get("event_end"),
        event_location=fields.get("event_location"), created_at=t, updated_at=t, updated_by=user_id)
    cols.update(extra)
    return db.insert("stories", **cols)


ITEM_SQL = ("SELECT i.*, s.name AS source_name, s.trust AS source_trust, s.category AS source_category, "
            "s.type AS source_type, s.config AS source_config FROM items i JOIN sources s ON s.id=i.source_id")


def items_by_id(db, ids, status="new"):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return db.q(f"{ITEM_SQL} WHERE i.id IN ({marks}) AND i.status=? ORDER BY i.id", (*ids, status))


def claim(db, ids):
    """Mark items as taken so a double-click, a second editor or the worker can't use them twice.
    Returns (token, claimed items)."""
    if not ids:
        return None, []
    token = f"{now()}|{uuid.uuid4().hex}"
    marks = ",".join("?" * len(ids))
    with db.tx():
        db.run(f"UPDATE items SET status='writing', claim=? WHERE id IN ({marks}) AND status='new'", (token, *ids))
    return token, db.q(f"{ITEM_SQL} WHERE i.claim=? AND i.status='writing' ORDER BY i.id", (token,))


def release(db, token):
    db.run("UPDATE items SET status='new', claim=NULL WHERE claim=? AND status='writing'", (token,))


def release_stale(db, minutes=15):
    """After a crash mid-write, items would stay 'writing' forever; put old claims back."""
    cutoff = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - minutes * 60, timezone.utc)
    db.run("UPDATE items SET status='new', claim=NULL WHERE status='writing' AND claim < ?",
           (cutoff.isoformat(timespec="seconds"),))


def item_source(item):
    ex = loads(item.get("extra"), {})
    return {"kind": "item", "id": item["id"], "name": ex.get("from") or item["source_name"],
            "trust": ai.item_trust(item), "title": item.get("title") or "", "url": item.get("url") or "",
            "text": item.get("text") or "", "published": item.get("published") or "",
            "note": "; ".join(ex.get("notes") or [])}


def first_video(dev):
    """If a source is a YouTube or Facebook video, the story embeds it."""
    for u in [s_.get("url") for s_ in dev.get("sources", [])] + list(dev.get("links", [])):
        if util.video_embed(u):
            return u
    return None


def new_development(db, source_id, dev, headline, category, user_id=None, **extra):
    dev.setdefault("questions", [])
    dev.setdefault("known", [])
    if not extra.get("video"):
        extra["video"] = first_video(dev)
    return create_story(db, source_id, {"headline": headline, "category": category}, status="developing",
                        user_id=user_id, dev=json.dumps(dev), **extra)


def start_development(db, item_ids, user_id=None):
    """Develop this story on one or more items. Returns (story id, is_event) or (None, reason)."""
    found = items_by_id(db, item_ids)
    if not found:
        return None, "Those items were already used, ignored, or are being worked on right now."
    token, items = claim(db, [i["id"] for i in found])
    if not items:
        return None, "Those items are already being worked on."
    try:
        if len(items) == 1 and items[0]["source_type"] == "calendar":
            return make_event(db, items[0], user_id), "event"
        first = items[0]
        wire = next((i for i in items if i["source_type"] == "wire"), None)
        extra = {}
        if wire:
            extra["scope"] = loads(wire["source_config"], {}).get("scope") or "national"
        sid = new_development(db, first["source_id"], {"sources": [item_source(i) for i in items]},
                              util.text_only(first["title"] or "In development", 200) or "In development",
                              first["source_category"], user_id, **extra)
        for it in items:
            db.update("items", it["id"], status="developing", story_id=sid, claim=None)
        return sid, ""
    finally:
        release(db, token)


def develop_text(db, text, url="", title="", trust="tip", user_id=None):
    """Manual add: pasted text, or just a link. Returns story id."""
    src = db.one("SELECT * FROM sources WHERE type='manual'")
    from .article import is_link, unwrap
    text = (text or "").strip()
    if is_link(text) and not url:
        url, text = unwrap(text), ""
    iid = db.insert("items", source_id=src["id"], hash="manual-" + uuid.uuid4().hex, url=url, title=title[:500],
                    text=text[:15000], published=now(), extra=json.dumps({"trust": trust}), fetched_at=now(),
                    status="new")
    sid, _ = start_development(db, [iid], user_id)
    if title == "" and sid:
        db.update("stories", sid, headline="In development: " + (util.domain_of(url) or text[:60] or "manual add"))
    return sid


def develop_tip(db, tip, user_id=None):
    src = db.one("SELECT * FROM sources WHERE type='tips'")
    member = db.one("SELECT * FROM members WHERE id=?", (tip["member_id"],)) if tip.get("member_id") else None
    text = tip["text"] + (f"\n\nWhere: {tip['location']}" if tip.get("location") else "")
    dev = {"sources": [{"kind": "tip", "id": tip["id"], "trust": "member" if member else "tip",
                        "name": f"Tip from @{member['username']}" if member else "Reader tip", "text": text,
                        "title": ""}],
           "photos": loads(tip["photos"], [])}
    sid = new_development(db, src["id"], dev, "In development: " + util.text_only(tip["text"], 70),
                          src["category"], user_id, tip_id=tip["id"],
                          member_id=member["id"] if member else None, credit="tip" if member else None,
                          credit_public=1 if tip.get("credit", 1) else 0)
    db.update("tips", tip["id"], status="used", story_id=sid)
    return sid


FACT_LABELS = [("what", "What happened?"), ("where", "Where?"), ("when", "When?"), ("who", "Who's involved?"),
               ("how", "How do you know?"), ("else", "Anything else?")]


def submission_text(sub):
    if sub["kind"] == "article":
        return (sub["headline"] + "\n\n" + util.text_only(sub["body"].replace("</p>", "\n\n"))).strip()
    f = loads(sub["facts"], {})
    return "\n".join(f"{label} {f.get(k, '').strip()}" for k, label in FACT_LABELS if f.get(k, "").strip())


def develop_submission(db, sub, user_id=None):
    m = db.one("SELECT * FROM members WHERE id=?", (sub["member_id"],))
    src = db.one("SELECT * FROM sources WHERE type='tips'")
    dev = {"sources": [{"kind": "submission", "id": sub["id"], "trust": "member",
                        "name": f"Submitted by @{m['username']}", "title": sub["headline"],
                        "text": submission_text(sub)}],
           "links": [u for u in loads(sub["links"], []) if u.startswith(("http://", "https://"))][:5],
           "photos": loads(sub["photos"], [])}
    sid = new_development(db, src["id"], dev, sub["headline"] or "In development: member submission",
                          sub["category"] or src["category"], user_id, member_id=m["id"],
                          credit="byline" if sub["kind"] == "article" else "tip", credit_public=sub["credit"],
                          submission_id=sub["id"], org_id=sub["org_id"], edited_note=1 if sub["kind"] == "article" else 0,
                          video=sub["video"] if util.video_embed(sub["video"]) else None)
    db.run("UPDATE submissions SET status='developing', story_id=? WHERE id=?", (sid, sub["id"]))
    return sid


def abandon_development(db, sid):
    """Stop developing: everything goes back where it came from."""
    s = db.one("SELECT * FROM stories WHERE id=? AND status='developing'", (sid,))
    if not s or s["published_at"] or loads(s["dev"], {}).get("written_at"):
        return False  # it was written before: going back to Develop never deletes it
    db.run("UPDATE items SET status='new', story_id=NULL WHERE story_id=? AND status='developing'", (sid,))
    if s["tip_id"]:
        db.update("tips", s["tip_id"], status="new", story_id=None)
    if s["submission_id"]:
        db.run("UPDATE submissions SET status='waiting', story_id=NULL WHERE id=?", (s["submission_id"],))
    db.run("DELETE FROM stories WHERE id=?", (sid,))
    return True


THIN = 800  # characters: less than this and the story needs looking up before it can be written well


def needs_research(db, sid):
    """Only research when the sources are too thin to write from (saves time and money)."""
    s = db.one("SELECT dev FROM stories WHERE id=?", (sid,))
    dev = writing.refresh_sources(loads(s["dev"], {}))
    db.update("stories", sid, dev=json.dumps(dev))
    have = sum(len(x.get("text") or "") for x in dev.get("sources", [])) + len(dev.get("pasted") or "")
    return have < THIN


def develop(db, sid, writer=None):
    s = db.one("SELECT * FROM stories WHERE id=?", (sid,))
    s["dev"] = loads(s["dev"], {})
    return writing.analyze(db, s, writer=writer)


def write_story(db, sid, writer=None, factcheck=False):
    s = db.one("SELECT * FROM stories WHERE id=?", (sid,))
    s["dev"] = loads(s["dev"], {})
    fields = writing.write(db, s, writer=writer, factcheck=factcheck)
    db.run("UPDATE items SET status='drafted' WHERE story_id=? AND status='developing'", (sid,))
    return fields


# ── no-AI story types ────────────────────────────────────
def make_event(db, item, user_id=None):
    """Calendar item → event listing draft. No AI, so no cost."""
    ex = loads(item["extra"], {})
    official = ai.item_trust(item) == "official"
    sid = create_story(db, item["source_id"], {
        "headline": item["title"], "summary": util.text_only(item["text"], 300),
        "body": "".join(f"<p>{util.clean_html(p)}</p>" for p in (item["text"] or "").split("\n\n") if p.strip()),
        "category": "Events" if "Events" in (settings.get(db, "categories") or []) else item["source_category"],
        "confidence": "high" if official else "low",
        "checklist": [] if official else
        [{"text": "Community-submitted event: confirm it's real and the details are right.", "checked": False}],
        "cites": [{"name": item["source_name"], "url": item["url"], "trust": ai.item_trust(item), "title": item["title"]}],
        "event_start": ex.get("event_start"), "event_end": ex.get("event_end"),
        "event_location": ex.get("location")}, kind="event", user_id=user_id)
    db.update("items", item["id"], status="drafted", story_id=sid, claim=None)
    return sid


def wire_story(db, item_id, user_id=None, publish_now=True):
    """A wire story, published as the wire wrote it, with its credit. No AI."""
    if db.val("SELECT s.type FROM items i JOIN sources s ON s.id=i.source_id WHERE i.id=?", (item_id,)) != "wire":
        return None  # only licensed wire copy may be published word for word
    token, items = claim(db, [item_id])
    if not items:
        return None
    try:
        it = items[0]
        cfg = loads(it["source_config"], {})
        ex = loads(it["extra"], {})
        html = util.clean_html(ex.get("html") or "")
        if not util.text_only(html):
            html = "".join(f"<p>{util.clean_html(p)}</p>" for p in (it["text"] or "").split("\n") if p.strip())
        credit = cfg.get("credit") or it["source_name"]
        cats = settings.get(db, "categories") or []
        sid = create_story(db, it["source_id"], {
            "headline": it["title"] or "Untitled", "summary": util.text_only(it["text"], 300), "body": html,
            "category": "National & World" if "National & World" in cats else it["source_category"],
            "byline": credit, "confidence": "high",
            "cites": [{"name": credit, "url": it["url"], "trust": "official", "title": it["title"]}]},
            user_id=user_id, wire=1, scope=cfg.get("scope") or "national", social=json.dumps({"_none": True}))
        db.update("items", it["id"], status="drafted", story_id=sid, claim=None)
    finally:
        release(db, token)
    if publish_now:
        publish(db, sid, user_id=user_id, note="wire story")
    return sid


def auto_write(db, source, item_ids, writer=None):
    """Sources set to 'write and publish automatically' (official only): each item written straight away,
    without questions, and published if the draft is clean."""
    created = []
    for iid in item_ids:
        if source["type"] == "wire":
            sid = wire_story(db, iid)
            if sid:
                created.append(sid)
            continue
        token, items = claim(db, [iid])
        if not items:
            continue
        try:
            if source["type"] == "calendar":
                created.append(make_event(db, items[0]))
                continue
            sid = new_development(db, source["id"], {"sources": [item_source(items[0])]},
                                  items[0]["title"] or "Untitled", source["category"])
            db.update("items", iid, status="developing", story_id=sid, claim=None)
            write_story(db, sid, writer=writer)
            created.append(sid)
        finally:
            release(db, token)
    for sid in created:
        maybe_autopublish(db, source, sid)
    return created


def maybe_autopublish(db, source, sid):
    """Auto-publish only when the source allows it, it is official, and the draft is clean."""
    s = db.one("SELECT * FROM stories WHERE id=?", (sid,))
    if s["status"] != "draft":
        return
    if (source["approval"] == "auto_high" and source["trust"] == "official" and s["confidence"] == "high"
            and not loads(s["checklist"], [])):
        publish(db, sid, user_id=None, note="auto-published by source rule")


def process_source(db, source, writer=None):
    """Collect new items (free). Only 'automatic' sources go on to the AI.
    Returns (new item count, created story ids, problem or '')."""
    first = not db.val("SELECT 1 FROM items WHERE source_id=?", (source["id"],))
    new_ids = sources.collect(db, source)
    if first and source["type"] in ("rss", "wire"):
        new_ids = sources.first_run_mark_old(db, source["id"])
    if not new_ids or source["approval"] != "auto_high" or source["trust"] != "official":
        return len(new_ids), [], ""
    if source["type"] not in ("calendar", "wire") and writer is None:
        ready, reason = ai.status(db)
        if not ready:
            return len(new_ids), [], reason  # items stay on the list for you
    try:
        return len(new_ids), auto_write(db, source, new_ids, writer=writer), ""
    except ai.AIUnavailable as e:
        return len(new_ids), [], str(e)


# ── member submissions ───────────────────────────────────
def submission_story(db, sub, user_id=None, edited=False, trusted=False):
    """A member-written article → a story (draft), credited to the member."""
    m = db.one("SELECT * FROM members WHERE id=?", (sub["member_id"],))
    src = db.one("SELECT * FROM sources WHERE type='tips'")
    photos = loads(sub["photos"], [])
    org = db.one("SELECT * FROM orgs WHERE id=? AND status='approved'", (sub["org_id"],)) if sub["org_id"] else None
    credit_name = f"@{m['username']}" if sub["credit"] else "a community member"
    cats = settings.get(db, "categories") or ["Local News"]
    sid = create_story(db, src["id"], {
        "headline": sub["headline"] or "Untitled", "body": util.clean_html(sub["body"]),
        "summary": util.text_only(sub["body"], 200), "category": sub["category"] if sub["category"] in cats else cats[0],
        "byline": credit_name + (f" for {org['name']}" if org else ""), "confidence": "medium",
        "cites": [{"name": f"Written by {credit_name}", "url": "", "trust": "tip", "title": ""}]},
        user_id=user_id, member_id=m["id"], credit="byline", credit_public=sub["credit"], submission_id=sub["id"],
        org_id=sub["org_id"], edited_note=1 if edited else 0, trusted_publish=1 if trusted else 0,
        image=photos[0] if photos else None, image_alt=sub["headline"] if photos else None,
        photo_member_id=m["id"] if photos else None,
        photo_credit=(f"Photo: @{m['username']}" if sub["credit"] else "Photo: submitted") if photos else None,
        video=sub["video"] if util.video_embed(sub["video"]) else None)
    db.run("UPDATE submissions SET story_id=? WHERE id=?", (sid, sub["id"]))
    return sid


# ── publishing ───────────────────────────────────────────
def default_social(db, s):
    """Which platforms a story goes to when nobody chose (auto-published stories)."""
    from . import social
    if s["wire"]:
        return {}
    chosen = (settings.get(db, "social_defaults") or {}).get(s["category"])
    if chosen is None:
        return {}
    text = s["social_text"] or s["summary"] or s["headline"]
    return {p: {"on": True, "text": text} for p in chosen if social.connected(db, p)}


def publish(db, sid, user_id=None, note=""):
    s = db.one("SELECT * FROM stories WHERE id=?", (sid,))
    fields = {"status": "published", "published_at": s["published_at"] or now(), "publish_at": None,
              "slug": s["slug"] or util.unique_slug(db, s["headline"], sid), "updated_at": now()}
    if not s["image"] and s["kind"] == "story":
        try:
            fields["image"] = graphics.headline_card(db, s)
            fields["image_alt"] = s["headline"]
        except Exception as e:
            log.warning("graphic failed: %s", e)
    social = loads(s["social"], {})
    if not social and s["kind"] == "story":
        social = default_social(db, s)
    for p, v in social.items():
        if isinstance(v, dict) and v.get("on") and v.get("status") != "posted":
            v["status"] = "pending"
    fields["social"] = json.dumps(social)
    db.update("stories", sid, **fields)
    if s["tip_id"]:
        db.update("tips", s["tip_id"], status="used", story_id=sid)
    util.activity(db, user_id, "published", f"story:{sid}", note or s["headline"])
    community.on_published(db, db.one("SELECT * FROM stories WHERE id=?", (sid,)))


def unpublish(db, sid, reject=False, reason=""):
    """Take a story off the site (or reject a draft): points it earned are taken back, and the member's
    submission shows what happened."""
    s = db.one("SELECT * FROM stories WHERE id=?", (sid,))
    fields = {"status": "rejected", "reject_reason": reason} if reject else {"status": "draft", "publish_at": None}
    db.update("stories", sid, **fields)
    ref = f"story:{sid}"
    for mid, reasons in ((s["member_id"], ("article", "tip", "featured")), (s["photo_member_id"], ("photo",))):
        for r in reasons:
            community.revoke(db, mid, r, ref)
    if s["submission_id"]:
        db.run("UPDATE submissions SET status=?, updated_at=? WHERE id=?",
               ("declined" if reject else "developing", now(), s["submission_id"]))
        if reject:
            community.notice(db, s["member_id"], f"The newsroom won't be running “{s['headline'][:80]}”.", "/me")


def publish_due(db):
    t = now()
    due = db.q("SELECT id, checklist FROM stories WHERE status='scheduled' AND publish_at <= ?", (t,))
    n = 0
    for r in due:
        if not all(c.get("checked") for c in loads(r["checklist"], [])):
            db.update("stories", r["id"], status="draft", publish_at=None)
            util.activity(db, None, "schedule cancelled", f"story:{r['id']}", "Checklist was no longer complete")
            continue
        publish(db, r["id"], note="scheduled")
        n += 1
    return n


def sources_due(db):
    out = []
    t = datetime.now(timezone.utc)
    for s in db.q("SELECT * FROM sources WHERE enabled=1"):
        if s["type"] not in sources.FETCHABLE:
            continue
        last = util.parse_iso(s["last_checked"])
        wait = max(5, s["interval_min"]) * 60
        if s["error_count"] >= 3:
            wait = min(wait * (2 ** min(s["error_count"] - 2, 4)), 6 * 3600)  # back off a failing source
        if not last or (t - last).total_seconds() >= wait:
            out.append(s)
    return out
