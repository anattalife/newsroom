"""Every setting the owner can change, described once.

The dashboard's Settings pages are generated from SECTIONS, so adding a field
here is all it takes to make it editable. Values are stored as JSON in the
`settings` table; fields of type "secret" are stored encrypted instead.
"""
import json

from . import page_defaults as PD
from . import writing_defaults as WD
from .security import get_secret, has_secret, set_secret

US_STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
             "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
             "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
             "PR", "GU", "VI", "AS", "MP"]
TIMEZONES = [("America/New_York", "Eastern"), ("America/Chicago", "Central"), ("America/Denver", "Mountain"),
             ("America/Phoenix", "Arizona"), ("America/Los_Angeles", "Pacific"), ("America/Anchorage", "Alaska"),
             ("Pacific/Honolulu", "Hawaii"), ("America/Puerto_Rico", "Atlantic (Puerto Rico)")]
MODELS = [("claude-sonnet-5", "Claude Sonnet 5 (recommended)"),
          ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (cheapest, simpler writing)"),
          ("claude-opus-5-5", "Claude Opus 5.5 (most capable, most expensive)")]

OLD_DEFAULT_CATEGORIES = ["Local News", "Public Safety", "Government", "Schools", "Weather", "Business",
                          "Community", "Traffic", "Sports", "Events"]
DEFAULT_CATEGORIES = ["Local News", "Government & Schools", "Public Safety", "Faith & Churches",
                      "Obituaries & Remembrances", "Sports", "Business & Openings", "Food & Specials", "Farm & Rural",
                      "Arts & Entertainment", "Health", "Clubs & Nonprofits", "Youth & Education", "Seniors",
                      "Outdoors", "Weather", "Events", "National & World"]
# Shown on each category page to invite organizations to share their news (owner can edit)
CATEGORY_INVITES = {
    "Government & Schools": "School districts, township clerks and the library: share meeting results, calendars and announcements.",
    "Public Safety": "Sheriff, police, fire and EMS: share releases, safety tips and road closures.",
    "Faith & Churches": "Churches and ministries: share service times, new pastors, community meals and events.",
    "Obituaries & Remembrances": "Funeral homes: share obituaries and service details, with the family's permission.",
    "Sports": "Athletic departments, rec leagues and booster clubs: share scores, schedules and standout players.",
    "Business & Openings": "Chamber, downtown association and business owners: share openings, changes and milestones.",
    "Food & Specials": "Restaurants and cafés: become a free Community Partner and post today's specials.",
    "Farm & Rural": "Extension office, FFA, 4-H, co-ops and the fair board: share results, programs and dates.",
    "Arts & Entertainment": "Theatre, music groups and galleries: share shows, auditions and exhibits.",
    "Health": "Hospital, health department and clinics: share clinics, screenings and health notices.",
    "Clubs & Nonprofits": "Rotary, Lions, VFW, Legion and charities: share projects, fundraisers and meetings.",
    "Youth & Education": "Scouts, youth groups, libraries and colleges: share programs and achievements.",
    "Seniors": "Senior center and Commission on Aging: share programs, meals and trips.",
    "Outdoors": "DNR, parks, conservation clubs and lake associations: share conditions, cleanups and events.",
}
PLATFORMS = [("facebook", "Facebook Page"), ("instagram", "Instagram"), ("threads", "Threads"),
             ("bluesky", "Bluesky"), ("x", "X")]


def F(key, label, type="text", default="", help="", options=None, **kw):
    return {"key": key, "label": label, "type": type, "default": default, "help": help,
            "options": options or [], **kw}


SECTIONS = [
    {"id": "publication", "title": "Publication and brand", "fields": [
        F("site_name", "Publication name", default="My Local News"),
        F("tagline", "Tagline", help="Shown under the name on some pages."),
        F("logo", "Logo", "image", help="Square PNG or JPG works best. Until you add one, a letter mark is used."),
        F("brand_color", "Brand color", "color", default="#B3261E"),
        F("site_url", "Website address", "url", help="Your site's address, like https://news.example.com. Filled in "
          "automatically the first time you open the dashboard. Used in social posts and emails."),
        F("contact_email", "Public contact email", "email"),
        F("phone", "Public phone number"),
        F("address", "Mailing address", "textarea"),
        F("timezone", "Time zone", "select", default="America/Chicago", options=TIMEZONES),
        F("social_facebook", "Facebook page link", "url"),
        F("social_instagram", "Instagram link", "url"),
        F("social_x", "X link", "url"),
        F("social_bluesky", "Bluesky link", "url"),
        F("social_youtube", "YouTube link", "url"),
        F("social_tiktok", "TikTok link", "url"),
    ]},
    {"id": "coverage", "title": "Coverage area", "intro":
        "Background for the AI, so it knows where your readers are. It never uses this to skip or refuse a story: "
        "you decide what gets written.", "fields": [
        F("town", "Main town", default="Riverton"),
        F("county", "County", help="Also used for weather alerts."),
        F("state", "State", "select", default="", options=[("", "Choose…")] + [(s, s) for s in US_STATES]),
        F("nearby", "Nearby towns", "chips", default=[]),
        F("zips", "ZIP codes", "chips", default=[]),
        F("neighborhoods", "Neighborhoods and landmarks", "chips", default=[]),
    ]},
    {"id": "ai", "title": "AI writing", "fields": [
        F("ai_key", "Claude API key", "secret", help="From console.anthropic.com. Stored encrypted."),
        F("ai_model", "Model for writing articles", "select", default="claude-sonnet-5", options=MODELS),
        F("ai_model_research", "Model for research and fact-checks", "select", default="claude-haiku-4-5-20251001",
          options=MODELS, help="Research reads a lot of web pages, which is where most of the cost is. The cheapest "
                               "model does this well; the article itself is still written by the model above."),
        F("ai_cap", "Monthly spending cap (USD)", "number", default=40, help="Drafting pauses when this is reached."),
        F("ai_price_in", "Price per million input tokens (USD)", "number", default=3.0,
          help="Used to estimate spending. Check current prices at anthropic.com/pricing."),
        F("ai_price_out", "Price per million output tokens (USD)", "number", default=15.0),
        F("ai_paused", "Pause AI", "toggle", default=False),
        F("ai_research", "Let the AI research on the web when you click Develop", "toggle", default=True,
          help="It searches for the missing facts (official sites, the organization's own pages, local news) and "
               "answers its own questions with a source link, so you only fill the gaps. About 1 cent per search."),
        F("ai_research_max", "Most web searches per story", "number", default=2,
          help="Each search adds about 1 cent plus the pages it reads. 1 or 2 is usually enough for a local story."),
        F("factcheck_default", "Tick “Check facts after writing” by default", "toggle", default=False,
          help="A second, shorter AI call that compares the article with the material and flags anything it can't find."),
    ]},
    {"id": "writing", "title": "Writing instructions", "intro":
        "What the AI is told when you click Write the article. The locked rules (accuracy, privacy, always write, "
        "never refuse) are always on and can't be edited. Everything here can.", "fields": [
        F("write_structure", "Structure", "textarea", default=WD.STRUCTURE, rows=8),
        F("write_style", "Style", "textarea", default=WD.STYLE, rows=6),
        F("write_banned", "Never use these words or phrases", "textarea", default=WD.BANNED, rows=5),
        F("write_headline", "Headline, social post and summary", "textarea", default=WD.HEADLINE, rows=4,
          help="{town} is replaced with your main town."),
        F("house_rules", "Your extra rules", "textarea", help="Anything else, one per line."),
        *[F("tmpl_" + k, f"Story type: {label}", "textarea", default=text, rows=2)
          for k, (label, text) in WD.TEMPLATES.items()],
        *[F(f"style_example_{i}", f"Style example {i}", "textarea", rows=6,
            help="Paste an article whose tone and plainness you like. The AI matches the style, never the facts or wording."
            if i == 1 else "") for i in range(1, 6)],
    ]},
    {"id": "review", "title": "Review and publishing", "fields": [
        F("byline", "Default byline", default="Staff"),
        F("ai_disclosure", "AI disclosure (end of every article)", "textarea",
          default="This story was drafted with AI assistance from public sources and reviewed by an editor before publication."),
        F("correction_label", "Correction heading", default="Correction"),
        F("edited_note_text", "Note on member articles you edited", default="Edited by the newsroom."),
    ]},
    {"id": "website", "title": "Public website", "fields": [
        F("site_description", "Site description", "textarea", help="Shown in Google results and link previews."),
        F("categories", "Categories", "chips", default=DEFAULT_CATEGORIES,
          help="Rename, add or remove. Drag isn't needed: the order you type them is the order on the site."),
        F("home_fill_latest", "Fill the homepage with the latest stories when there aren't enough Featured ones",
          "toggle", default=True),
        F("home_mix_national", "Include National & World stories in the main homepage list", "toggle", default=False),
        F("home_show_national", "Show the National & World box on the homepage", "toggle", default=True),
        F("home_show_specials", "Show today's food specials on the homepage", "toggle", default=True),
        F("home_show_scores", "Show the scores strip on the homepage", "toggle", default=True),
        F("home_show_events", "Show upcoming events on the homepage", "toggle", default=True),
        F("events_show_games", "Include school games in Upcoming events", "toggle", default=True),
        F("home_show_tipbox", "Show the tip box on the homepage", "toggle", default=True),
        F("page_about", "About page", "markdown", default=PD.ABOUT,
          help="{site}, {town} and {area} are filled in with your publication name, town and county."),
        F("page_contact", "Contact page", "markdown", default=PD.CONTACT,
          help="Your email, phone and address from Publication and brand are shown after this text."),
        F("page_corrections", "Corrections policy", "markdown", default=PD.CORRECTIONS),
        F("page_privacy", "Privacy policy", "markdown", default="[YOUR PRIVACY POLICY]"),
        F("page_terms", "Terms of use", "markdown", default="[YOUR TERMS OF USE]"),
        F("page_ai", "How we use AI", "markdown", default=PD.AI),
        F("footer_text", "Footer text"),
        F("plausible_domain", "Plausible Analytics domain", help="Optional privacy-friendly visitor stats. Leave blank for none."),
    ]},
    {"id": "tips", "title": "Reader tips", "fields": [
        F("tips_enabled", "Accept tips", "toggle", default=True),
        F("tips_photos", "Allow photo uploads", "toggle", default=True),
        F("tips_max_photos", "Maximum photos per tip", "number", default=5),
        F("tips_intro", "Text above the tip form", "textarea",
          default="Send us a tip. Photos welcome. You can stay anonymous, and your contact details are never published."),
    ]},
    {"id": "community", "title": "Members and community", "intro":
        "Members have accounts on the public site only. They never see the dashboard, sources or the AI.", "fields": [
        F("members_open", "Allow new members to sign up", "toggle", default=True),
        F("blocked_email_domains", "Also refuse sign-ups from these email domains", "chips", default=[],
          help="Temporary-email services like mailinator.com are already refused. Add any others you see bots use."),
        F("members_confirm_email", "Ask new members to confirm their email",
          "toggle", default=True, help="Only works once outgoing email is set up (Settings → System)."),
        F("comments_on", "Comments on articles", "toggle", default=True),
        F("hold_first_comments", "Hold a new member's first comments for approval", "number", default=3),
        F("flag_hide", "Hide a comment for review after this many flags", "number", default=3,
          help="0 means flags never hide anything automatically."),
        F("comment_edit_minutes", "Minutes members can edit a comment", "number", default=15),
        F("blocked_words", "Words that hold a comment for review", "chips", default=[]),
        F("pts_article", "Points: member-written article published", "number", default=20),
        F("pts_facts", "Points: facts or tip published", "number", default=10),
        F("pts_featured", "Points: article marked Featured", "number", default=15),
        F("pts_upvote", "Points: each upvote on an article", "number", default=1),
        F("pts_comment_upvote", "Points: each upvote on a comment", "number", default=1),
        F("pts_photo", "Points: photo used in a published article", "number", default=5),
        F("pts_score", "Points: first to report a game's final score", "number", default=2),
        F("pts_live", "Work points: each live game update (up to 10 per game)", "number", default=1),
        F("pts_stream", "Work points: sharing a live stream of a game", "number", default=25),
        F("pts_video", "Work points: sharing a video of a game", "number", default=5),
        F("pts_callit_win", "Call It: points for the closest guess", "number", default=25),
        F("pts_callit_close", "Call It: points for the next closest (top 10)", "number", default=10),
        F("pts_callit_exact", "Call It: points for calling a game's exact score", "number", default=30),
        F("pts_callit_winner", "Call It: points for picking a game's winner", "number", default=5),
        F("pts_callit_choice", "Call It: points for a right answer to a multiple choice", "number", default=5),
        F("pts_comment", "Work points: each comment", "number", default=1),
        F("pts_comment_daily_cap", "Most Work points a member can earn from comments in a day", "number", default=5),
        F("pts_reply", "Social points: each reply to your comment, or comment on your story", "number", default=1),
        F("pts_daily_cap", "Most points one member can give another in a day by upvoting and replying", "number", default=5),
        F("priv_unhold", "Points to stop holding comments", "number", default=25),
        F("priv_links", "Points to post links in comments and add more photos", "number", default=100),
        F("priv_flag_double", "Points for flags to count double", "number", default=250),
        F("priv_suggest", "Points to be suggested to you as trusted", "number", default=500),
        F("max_photos", "Photos per submission", "number", default=6),
        F("max_photos_plus", "Photos per submission after reaching the links level", "number", default=12),
        F("writing_guide", "Writing guide shown beside the member editor", "markdown", default=WD.WRITING_GUIDE),
        F("partner_intro", "Become a community partner page", "markdown", default=PD.PARTNER_INTRO),
    ]},
    {"id": "app", "title": "Mobile app", "intro":
        "Readers install the app from your website: on a phone, the site offers “Add to home screen”.", "fields": [
        F("app_name", "App name", help="Defaults to the publication name."),
        F("app_short_name", "Short name under the icon", help="12 characters or fewer."),
    ]},
    {"id": "notifications", "title": "Notifications", "intro":
        "Needs outgoing email to be set up under System.", "fields": [
        F("notify_to", "Send notifications to", "email", help="Defaults to the owner's email."),
        F("notify_drafts", "Email me when new items are collected", "toggle", default=True),
        F("notify_tips", "Email me when a tip arrives", "toggle", default=True),
        F("notify_failures", "Email me when a source stops working", "toggle", default=True),
        F("notify_submissions", "Email me when a member submits something", "toggle", default=True),
        F("notify_flags", "Email me when something is flagged", "toggle", default=False),
        F("notify_social", "Email me when a social post fails", "toggle", default=True),
    ]},
    {"id": "system", "title": "System", "fields": [
        F("smtp_host", "Outgoing email server (SMTP)", help="For example email-smtp.us-east-1.amazonaws.com for Amazon SES."),
        F("smtp_port", "Port", "number", default=587),
        F("smtp_user", "Username"),
        F("smtp_password", "Password", "secret"),
        F("smtp_from", "Send from address", "email"),
        F("backup_keep", "Nightly backups to keep", "number", default=7),
    ]},
]
LATER = [
    ("Ad spots", "Display ad slots are planned next. Everything for members and partners stays free."),
]
# stored under other keys and edited on their own pages, but known here so get() has defaults
# Subcategories inside a section, e.g. Sports → College. The newsroom adds and removes them.
SUBCATEGORIES = {"Sports": ["Middle School", "JV/Varsity", "College"]}


def subcategories(db, category=None):
    """{section: [subcategory, …]}, or just one section's list."""
    subs = get(db, "subcategories") or {}
    return list(subs.get(category) or []) if category is not None else subs
EXTRA_DEFAULTS = {"social_defaults": {}, "category_invites": CATEGORY_INVITES, "subcategories": SUBCATEGORIES}

FIELDS = {f["key"]: f for s in SECTIONS for f in s["fields"]}


def get(db, key):
    f = FIELDS.get(key)
    if f and f["type"] == "secret":
        return get_secret(db, "setting:" + key)
    r = db.one("SELECT value FROM settings WHERE key=?", (key,))
    if r:
        return json.loads(r["value"])
    return f["default"] if f else EXTRA_DEFAULTS.get(key)


def migrate(db):
    """One-time changes for sites upgraded from earlier versions."""
    for key, old, new in (("write_style", WD.OLD_STYLE, WD.STYLE), ("write_structure", WD.OLD_STRUCTURE, WD.STRUCTURE),
                          *[(k, v, FIELDS[k]["default"]) for k, v in PD.OLD.items()],
                          ("partner_intro", PD.PARTNER_INTRO_V2, PD.PARTNER_INTRO),
                          ("partner_intro", PD.PARTNER_INTRO_V3, PD.PARTNER_INTRO),
                          ("page_about", PD.ABOUT_V2, PD.ABOUT), ("page_contact", PD.CONTACT_V2, PD.CONTACT)):
        r = db.one("SELECT value FROM settings WHERE key=?", (key,))
        if r and json.loads(r["value"]).replace("\r\n", "\n").strip() == old.strip():
            put(db, key, new)  # still the old default: switch to the improved one
    r = db.one("SELECT value FROM settings WHERE key='ai_research_max'")
    if r and json.loads(r["value"]) == 5 and not db.one("SELECT 1 FROM settings WHERE key='_migrated_research_max'"):
        put(db, "ai_research_max", 2)  # the old default: lower it to cut research costs
        put(db, "_migrated_research_max", True)
    inv = get(db, "category_invites") or {}
    if inv.get("Food & Specials") == "Restaurants: post today\'s specials for free in the Directory.":  # the directory is gone
        put(db, "category_invites", {**inv, "Food & Specials": CATEGORY_INVITES["Food & Specials"]})
    if not db.one("SELECT 1 FROM settings WHERE key='_migrated_org_points'"):
        from . import community
        touched = set()
        for s in db.q("SELECT id FROM stories WHERE org_id IS NOT NULL"):
            for r in db.q("SELECT member_id, reason, ref FROM points_log WHERE ref=? OR ref LIKE ?",
                          (f"story:{s['id']}", f"story:{s['id']}:by:%")):
                community.revoke(db, r["member_id"], r["reason"], r["ref"])
                touched.add(r["member_id"])
            for c in db.q("SELECT id, member_id FROM comments WHERE story_id=?", (s["id"],)):
                for r in db.q("SELECT member_id, reason, ref FROM points_log WHERE reason='reply' AND ref LIKE ?",
                              (f"reply:{c['id']}:by:%",)):
                    if not db.val("SELECT parent_id FROM comments WHERE id=?", (c["id"],)):
                        community.revoke(db, r["member_id"], r["reason"], r["ref"])   # replies to the story itself
                        touched.add(r["member_id"])
        put(db, "_migrated_org_points", True)
    if not db.one("SELECT 1 FROM settings WHERE key='_migrated_badges_v2'"):
        from . import community
        for r in db.q("SELECT id FROM members"):
            community.check_badges(db, r["id"], quiet=True)   # tiers for what people have already done
        put(db, "_migrated_badges_v2", True)
    if not db.one("SELECT 1 FROM settings WHERE key='_migrated_points_split'"):
        from . import community
        for c in db.q("SELECT * FROM comments WHERE status='visible'"):
            community.sync_comment_points(db, c, backfill=True)  # comments and replies now earn points
        community.recalc_all(db)  # fill in Work and Social points from what's already been earned
        put(db, "_migrated_points_split", True)
    stored = db.one("SELECT value FROM settings WHERE key='categories'")
    if stored and json.loads(stored["value"]) == OLD_DEFAULT_CATEGORIES:
        put(db, "categories", DEFAULT_CATEGORIES)  # still the untouched old list: switch to the new one
        for old, new in (("Government", "Government & Schools"), ("Schools", "Government & Schools"),
                         ("Business", "Business & Openings"), ("Community", "Local News"),
                         ("Traffic", "Public Safety")):
            for table in ("stories", "sources"):
                db.run(f"UPDATE {table} SET category=? WHERE category=?", (new, old))
    elif stored:
        cats = json.loads(stored["value"])
        if "National & World" not in cats:
            put(db, "categories", cats + ["National & World"])


def all_values(db):
    stored = {r["key"]: json.loads(r["value"]) for r in db.q("SELECT key,value FROM settings")}
    out = {}
    for k, f in FIELDS.items():
        if f["type"] == "secret":
            out[k] = has_secret(db, "setting:" + k)  # only whether it's set, never the value
        else:
            out[k] = stored.get(k, f["default"])
    for k, v in EXTRA_DEFAULTS.items():
        out.setdefault(k, stored.get(k, v))
    out.update({k: v for k, v in stored.items() if k not in FIELDS})
    return out


def put(db, key, value):
    f = FIELDS.get(key)
    if f and f["type"] == "secret":
        set_secret(db, "setting:" + key, value)
        return
    db.run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           (key, json.dumps(value)))


def coerce(field, raw):
    """Turn a submitted form value into the stored type."""
    t = field["type"]
    if t == "toggle":
        return raw in ("on", "1", "true", True)
    if t == "number":
        try:
            n = float(raw)
            return int(n) if n.is_integer() else n
        except (TypeError, ValueError):
            return field["default"]
    if t == "chips":
        items = raw if isinstance(raw, list) else str(raw or "").replace("\r", "").replace(",", "\n").split("\n")
        return [i.strip() for i in items if i.strip()]
    if t == "color":
        raw = (raw or "").strip()
        return raw if len(raw) == 7 and raw.startswith("#") else field["default"]
    if t == "select":
        allowed = [o[0] if isinstance(o, (list, tuple)) else o for o in field["options"]]
        return raw if raw in allowed else field["default"]
    return (raw or "").strip()
