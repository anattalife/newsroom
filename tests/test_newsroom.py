"""End-to-end tests. Run with:  python -m unittest discover tests

No network or API key needed: web requests and the AI are replaced with fakes.
"""
import io
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TMP = os.environ.get("NEWSROOM_TEST_DATA") or tempfile.mkdtemp(prefix="newsroom-test-")
os.environ["NEWSROOM_DATA"] = TMP

from PIL import Image  # noqa: E402

from app import create_app, pipeline, worker  # noqa: E402
from app import db as dbm  # noqa: E402
from app.sources import calendar, rss, weather  # noqa: E402

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>PD</title>
<item><title>Police warn of phone scam</title><link>https://pd.example/1</link><guid>pd-1</guid>
<description>&lt;p&gt;Riverton police say callers pose as officers and demand gift cards. Several residents reported calls.&lt;/p&gt;</description>
<pubDate>Wed, 23 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>Car seat checks Saturday</title><link>https://pd.example/2</link><guid>pd-2</guid>
<description>Free car-seat inspections at the station 9 a.m. to noon.</description></item>
<item><title>Springfield news roundup</title><link>https://pd.example/3</link><guid>pd-3</guid>
<description>Unrelated town.</description></item>
</channel></rss>"""

ICS_START = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y%m%dT150000Z")
ICS = f"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:ev1
SUMMARY:Library book sale
DTSTART:{ICS_START}
LOCATION:Main Library\\, 1 Elm St
DESCRIPTION:Hundreds of books\\, $1 each.
END:VEVENT
BEGIN:VEVENT
UID:old
SUMMARY:Old event
DTSTART:20200101T100000Z
END:VEVENT
END:VCALENDAR""".encode()

NWS = {"features": [
    {"id": "a1", "properties": {"id": "a1", "event": "Frost Advisory", "headline": "Frost Advisory issued for Polk County",
                                "severity": "Minor", "areaDesc": "Polk; Story", "description": "Temps near 30.",
                                "sent": "2026-09-23T20:00:00Z", "expires": "2026-09-24T14:00:00Z"}},
    {"id": "a2", "properties": {"id": "a2", "event": "Wind", "headline": "Wind in Far County",
                                "areaDesc": "Far County", "description": "x"}}]}


class FakeResp:
    def __init__(self, content=b"", js=None):
        self.content = content
        self._js = js

    def json(self):
        return self._js


WX = "https://api.weather.gov/gridpoints/DMX/70,50"
POINTS = {"properties": {"forecast": WX + "/forecast", "forecastHourly": WX + "/forecast/hourly",
                         "observationStations": WX + "/stations", "radarStation": "KDMX",
                         "relativeLocation": {"properties": {"city": "Riverton", "state": "IA"}}}}
HOURLY = {"properties": {"periods": [
    {"startTime": f"2026-09-24T{h:02d}:00:00-05:00", "temperature": 60 + h % 5, "temperatureUnit": "F",
     "isDaytime": 6 < h < 19, "shortForecast": "Chance Rain Showers" if h == 3 else "Partly Sunny",
     "probabilityOfPrecipitation": {"value": 40 if h == 3 else 0}} for h in range(30)]}}
DAILY = {"properties": {"periods": [
    {"name": n, "isDaytime": i % 2 == 0, "temperature": 70 - i, "temperatureUnit": "F", "windSpeed": "10 mph",
     "shortForecast": "Mostly Sunny" if i % 2 == 0 else "Mostly Clear", "detailedForecast": f"Details for {n}.",
     "probabilityOfPrecipitation": {"value": None}}
    for i, n in enumerate(["Today", "Tonight", "Friday", "Friday Night", "Saturday", "Saturday Night"])]}}
OBS = {"properties": {"textDescription": "Partly Cloudy", "temperature": {"value": 20.0},
                      "windSpeed": {"value": 16.09}, "relativeHumidity": {"value": 55.4},
                      "timestamp": "2026-09-24T01:00:00+00:00"}}
PT_ALERTS = {"features": [{"properties": {"id": "t1", "event": "Tornado Warning", "severity": "Extreme",
                                          "headline": "Tornado Warning for Polk County", "description": "Take shelter.",
                                          "instruction": "Go to a basement.",
                                          "expires": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()}}]}


def fake_get(url, headers=None, timeout=25):
    if "nominatim" in url:
        return FakeResp(js=[{"display_name": "Riverton, Polk County, Iowa, United States", "lat": "41.5868",
                             "lon": "-93.6250"}])
    if "/points/" in url:
        return FakeResp(js=POINTS)
    if url.endswith("/forecast/hourly"):
        return FakeResp(js=HOURLY)
    if url.endswith("/forecast"):
        return FakeResp(js=DAILY)
    if url.endswith("/stations"):
        return FakeResp(js={"features": [{"properties": {"stationIdentifier": "KDSM"}}]})
    if "observations/latest" in url:
        return FakeResp(js=OBS)
    if "alerts/active?point=" in url:
        return FakeResp(js=PT_ALERTS)
    if "weather.gov" in url:
        return FakeResp(js=NWS)
    if url.endswith(".ics"):
        return FakeResp(ICS)
    return FakeResp(RSS)


def fake_ai(system, user):
    """Stands in for Claude in all three steps: Develop (questions), Write, and Fact-check."""
    titles = re.findall(r"^Title: (.+)$", user, re.M)
    if "develop a story before it is written" in system:
        assert "Never refuse" in system
        research = "Use web search" in system
        return "I searched the city's site. " + json.dumps({
                           "known": [f"The source says: {t}" for t in titles] or ["Only a little is known"],
                           "questions": [{"q": "When did it happen?", "answer": "Monday at 7 p.m." if research and
                                          "fall parade" in user.lower() else "", "source": "https://city.example/news"},
                                         "Who confirmed it?"],
                           "story_type": "crime" if "scam" in user.lower() else "general",
                           "category": "Public Safety", "scope": "local",
                           "headline_idea": titles[0] if titles else "Working headline"})
    if "You are updating an article" in system:
        note = user.split("EDITOR'S NOTE:\n", 1)[1]
        body = user.split("ARTICLE:\n", 1)[1].split("\n\nGAPS LIST:")[0]
        return json.dumps({"headline": user.split("HEADLINE: ", 1)[1].split("\n")[0], "summary": "s",
                           "body": body + f"<p>{note}</p>", "gaps": []})
    if "You check a news article" in system:
        return json.dumps({"flags": [{"text": "sixty officers", "problem": "The material doesn't mention this."}]}
                          if "sixty officers" in user else {"flags": []})
    assert "Always write it. Never refuse" in system and "Structure" in system
    answers = re.findall(r"^A: (.+)$", user, re.M)
    head = ("Combined: " if len(titles) > 1 else "") + (titles[0] if titles else "Community news")
    body = ("<p>According to police, " + head + ".</p>" + "".join(f"<p>{a}</p>" for a in answers)
            + "<script>alert(1)</script>" + ("<p>There were sixty officers.</p>" if "sixty" in user else ""))
    return json.dumps({"headline": head, "summary": "Summary of " + head, "social": "Social: " + head, "body": body,
                       "category": "Public Safety", "gaps": ["What time does it start?"],
                       "unconfirmed": ["Confirm the number of reports"] if "scam" in user.lower() else []})


fake_writer = fake_ai


def claude_patch():
    return mock.patch("app.ai.call_claude", side_effect=lambda db, s, u, **k: fake_ai(s, u))


ARTICLE = ("Hillsdale County commissioners voted 4-1 on Tuesday to buy a new ambulance for $310,000. " * 12).strip()


def fake_read_link(url):
    if "article.example" in url:
        return "Commissioners buy ambulance", ARTICLE, ""
    return "", "", ""


def jpeg_with_gps():
    img = Image.new("RGB", (40, 30), "red")
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (41.0, 30.0, 0.0)}  # GPS info
    exif[0x010F] = "PhoneMaker"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    buf.seek(0)
    return buf




class NewsroomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.c = cls.app.test_client()
        cls.patch = mock.patch("app.sources.http.get", side_effect=fake_get)
        cls.patch.start()
        import app.weather as wxmod
        for mod in (rss, weather, calendar, wxmod):
            mock.patch.object(mod, "get", side_effect=fake_get).start()
        mock.patch("app.writing.read_link", side_effect=fake_read_link).start()
        mock.patch("app.views.members.JOIN_MIN_SECONDS", 0).start()

    @classmethod
    def tearDownClass(cls):
        mock.patch.stopall()
        shutil.rmtree(TMP, ignore_errors=True)

    # helpers
    def post(self, url, data, client=None, **kw):
        client = client or self.c
        with client.session_transaction() as s:
            tok = s.get("csrf") or "x"
            s["csrf"] = tok
        return client.post(url, data={"_csrf": tok, **data}, **kw)

    def db(self):
        return dbm.DB()

    def item_id(self, d, like):
        return d.val("SELECT id FROM items WHERE title LIKE ? AND status='new' ORDER BY id", (like,))

    def develop(self, item_ids, answers=None, write=True, factcheck=False):
        """Develop this story → (optionally answer) → Write the article. Returns the story id."""
        with claude_patch():
            r = self.post("/admin/items", {"item": [str(i) for i in item_ids], "action": "develop"})
            self.assertIn("/admin/develop/", r.headers["Location"], r.headers.get("Location"))
            sid = int(r.headers["Location"].rsplit("/", 1)[1])
            if write:
                form = {"action": "write", **{f"a{i}": a for i, a in enumerate(answers or [])}}
                if factcheck:
                    form["factcheck"] = "on"
                self.post(f"/admin/develop/{sid}", form)
        return sid

    def approve(self, sid, client=None, **extra):
        d = self.db()
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        n = len(json.loads(s["checklist"]))
        return self.post(f"/admin/story/{sid}", {"headline": s["headline"], "summary": s["summary"], "body": s["body"],
                                                 "category": s["category"], "byline": s["byline"], "action": "approve",
                                                 "checked": [str(i) for i in range(n)], **extra}, client=client)

    def join(self, data, client=None):
        c = client or self.c
        ft = re.search(r'name="ft" value="([^"]+)"', c.get("/join").text)
        return self.post("/join", {**data, "ft": ft.group(1) if ft else ""}, client=c)

    def member_client(self, username, email=None, password="member-pass-1"):
        c = self.app.test_client()
        self.dbq("DELETE FROM rate")
        r = self.join({"username": username, "email": email or f"{username}@example.com",
                                "password": password}, client=c)
        self.assertEqual(r.status_code, 302, r.text[:500])
        return c

    def dbq(self, sql, args=()):
        d = self.db()
        try:
            return d.run(sql, args)
        finally:
            d.close()

    def mid(self, username):
        return self.db().val("SELECT id FROM members WHERE username=?", (username,))

    # ── setup and sources ─────────────────────────────────
    def test_01_setup_wizard(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/setup", r.headers["Location"])
        r = self.post("/admin/setup?step=1", {"name": "Pat", "email": "pat@example.com",
                                              "password": "short", "password2": "short"})
        self.assertIn("at least 10", r.text)
        r = self.post("/admin/setup?step=1", {"name": "Pat", "email": "pat@example.com",
                                              "password": "correct-horse-1", "password2": "correct-horse-1"})
        self.assertEqual(r.status_code, 302)
        r = self.post("/admin/setup?step=2", {"site_name": "Riverton Daily", "town": "Riverton", "county": "Polk",
                                              "state": "IA", "timezone": "America/Chicago"})
        self.assertEqual(r.status_code, 302)
        r = self.post("/admin/setup?step=3", {"ai_key": "sk-test-123", "ai_cap": "40"})
        self.assertEqual(r.status_code, 302)
        r = self.post("/admin/setup?step=4", {"use_police": "on", "url_police": "https://pd.example/rss",
                                              "use_weather": "on", "use_events": "on",
                                              "url_events": "https://lib.example/cal.ics"})
        self.assertEqual(r.status_code, 302)
        d = self.db()
        self.assertEqual(d.val("SELECT COUNT(*) FROM sources WHERE builtin=0"), 3)
        raw = d.val("SELECT value FROM secrets WHERE key='setting:ai_key'")
        self.assertNotIn(b"sk-test-123", raw)
        other = self.app.test_client()
        self.assertEqual(other.get("/admin/setup?step=1").status_code, 302)
        from app import settings
        settings.put(d, "site_url", "https://news.example")
        self.assertIn("National & World", settings.get(d, "categories"))

    def test_02_pages_render(self):
        for u in ["/admin/", "/admin/sources", "/admin/sources/new?type=rss", "/admin/sources/new?type=email",
                  "/admin/sources/new?type=weather", "/admin/sources/new?type=calendar",
                  "/admin/sources/new?type=wire", "/admin/queue/all", "/admin/queue/all?tab=developing",
                  "/admin/tips", "/admin/account", "/admin/weather", "/admin/settings/people", "/admin/settings/activity",
                  "/admin/settings/backups", "/admin/members/submissions", "/admin/community/members",
                  "/admin/community/flags", "/admin/community/flags?tab=flags", "/admin/community/flags?tab=recent",
                  "/admin/community/badges", "/admin/community/partners", "/admin/social"] + \
                [f"/admin/settings/{s}" for s in ("publication", "coverage", "ai", "writing", "review", "website",
                                                  "community", "tips", "app", "notifications", "system")]:
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, u)
        self.assertIn("Never refuse", self.c.get("/admin/settings/writing").text)  # the locked rules are shown

    def test_03_source_test_button(self):
        r = self.post("/admin/sources/test", {"type": "rss", "name": "X", "cfg_url": "https://pd.example/rss",
                                              "trust": "official"})
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 3)
        with mock.patch.object(rss, "get", side_effect=ValueError("Nothing found at that address (404).")):
            r = self.post("/admin/sources/test", {"type": "rss", "name": "X", "cfg_url": "https://bad"})
            self.assertFalse(r.get_json()["ok"])
            self.assertIn("404", r.get_json()["error"])

    # ── Develop this story ─────────────────────────────────
    def test_04_collect_then_develop(self):
        d = self.db()
        created = worker.cycle(d, writer=fake_writer)
        self.assertEqual(created, [])
        self.assertEqual(d.val("SELECT COUNT(*) FROM stories"), 0)
        self.assertEqual(d.val("SELECT COUNT(*) FROM items WHERE status='new'"), 5)
        src = d.val("SELECT id FROM sources WHERE type='rss'")
        page = self.c.get(f"/admin/queue/{src}")
        self.assertIn("Police warn of phone scam", page.text)
        self.assertIn("Write it", page.text)
        self.assertIn("Add details first", page.text)
        # step 1: the AI lists what it knows and asks what's missing
        scam = self.item_id(d, "%scam%")
        sid = self.develop([scam], write=False)
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual(s["status"], "developing")
        dev = json.loads(s["dev"])
        self.assertEqual([q["q"] for q in dev["questions"]], ["When did it happen?", "Who confirmed it?"])
        self.assertIn("phone scam", dev["known"][0])
        self.assertEqual(s["story_type"], "crime")
        self.assertEqual(d.val("SELECT status FROM items WHERE id=?", (scam,)), "developing")
        page = self.c.get(f"/admin/develop/{sid}").text
        self.assertIn("When did it happen?", page)
        self.assertIn("Write the article", page)
        # save part-way: stays In development
        self.post(f"/admin/develop/{sid}", {"action": "save", "a0": "Monday and Tuesday", "details": "Chief Lee said so."})
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (sid,)), "developing")
        self.assertIn("Police warn", self.c.get("/admin/queue/all?tab=developing").text)
        # step 2: write, with one question unanswered
        with claude_patch() as ai_call:
            r = self.post(f"/admin/develop/{sid}", {"action": "write", "a0": "Monday and Tuesday",
                                                    "details": "Chief Lee said so."})
            self.assertIn(f"/admin/story/{sid}", r.headers["Location"])
            system, user = ai_call.call_args[0][1], ai_call.call_args[0][2]
        self.assertTrue(user.startswith("EDITOR'S INPUT"))          # the editor's answers come first
        self.assertIn("Chief Lee said so.", user)
        self.assertIn("QUESTIONS NOBODY ANSWERED", user)            # open questions are listed, not guessed
        self.assertIn("Who confirmed it?", user.split("QUESTIONS NOBODY ANSWERED")[1])
        self.assertIn("Story type: Crime and courts", system)
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual(s["status"], "draft")
        self.assertIn("Monday and Tuesday", s["body"])
        self.assertNotIn("<script", s["body"])
        self.assertEqual(s["confidence"], "medium")
        self.assertEqual(len(json.loads(s["checklist"])), 1)
        self.assertEqual(s["social_text"], "Social: Police warn of phone scam")
        self.assertEqual(d.val("SELECT status FROM items WHERE id=?", (scam,)), "drafted")
        # the AI never refuses: an off-topic item still becomes a story
        spring = self.item_id(d, "%Springfield%")
        sid2 = self.develop([spring])
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (sid2,)), "draft")
        self.assertIn("Springfield", d.val("SELECT headline FROM stories WHERE id=?", (sid2,)))
        for like in ("%seat%", "%Frost%"):
            self.develop([self.item_id(d, like)])
        # stop developing: the item goes back to the list
        lib = self.item_id(d, "Library%")
        self.assertIsNotNone(lib)
        # calendar item → event listing, free even with AI paused
        from app import settings as st
        st.put(d, "ai_paused", True)
        r = self.post(f"/admin/items?one={lib}&action=develop", {})
        self.assertIn("/admin/story/", r.headers["Location"])
        st.put(d, "ai_paused", False)
        ev = d.one("SELECT * FROM stories WHERE kind='event'")
        self.assertEqual(ev["event_location"], "Main Library, 1 Elm St")
        # nothing is re-collected on the next run
        d.run("UPDATE sources SET last_checked=NULL")
        worker.cycle(d, writer=fake_writer)
        self.assertEqual(d.val("SELECT COUNT(*) FROM items WHERE status='new'"), 0)

    def test_04b_abandon_combine_and_auto_sources(self):
        d = self.db()
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        a = d.insert("items", source_id=src, hash="x0", title="Riverton parade", text="Parade Saturday.",
                     fetched_at=dbm.now())
        sid = self.develop([a], write=False)
        self.post(f"/admin/develop/{sid}", {"action": "abandon"})
        self.assertIsNone(d.one("SELECT * FROM stories WHERE id=?", (sid,)))
        self.assertEqual(d.val("SELECT status FROM items WHERE id=?", (a,)), "new")
        # combine two items from different sources into one story
        tips = d.val("SELECT id FROM sources WHERE type='tips'")
        b = d.insert("items", source_id=tips, hash="x2", title="Smoke seen on Elm Street", text="Lots of smoke.",
                     fetched_at=dbm.now())
        sid = self.develop([a, b])
        st = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertTrue(st["headline"].startswith("Combined"))
        self.assertEqual(len(json.loads(st["cites"])), 2)
        self.assertEqual({d.val("SELECT story_id FROM items WHERE id=?", (i,)) for i in (a, b)}, {sid})
        # an official source set to write and publish automatically: every item written, clean ones published
        auto = d.insert("sources", name="Auto feed", type="rss", trust="official", category="Public Safety",
                        interval_min=30, config=json.dumps({"url": "https://auto.example/rss"}), approval="auto_high",
                        created_at=dbm.now())
        worker.cycle(d, writer=fake_writer)
        self.assertEqual(d.val("SELECT COUNT(*) FROM stories WHERE source_id=? AND status='published'", (auto,)), 2)
        self.assertEqual(d.val("SELECT COUNT(*) FROM stories WHERE source_id=? AND status='draft'", (auto,)), 1)
        self.assertEqual(d.val("SELECT COUNT(*) FROM items WHERE source_id=? AND status='skipped'", (auto,)), 0)

    def test_05_approval_gate_featured_and_publish(self):
        d = self.db()
        scam = d.one("SELECT * FROM stories WHERE headline LIKE '%phone scam%' AND source_id=(SELECT id FROM sources "
                     "WHERE type='rss' ORDER BY id LIMIT 1) ORDER BY id")
        url = f"/admin/story/{scam['id']}"
        self.assertEqual(self.c.get(url).status_code, 200)
        form = {"headline": scam["headline"], "summary": "s", "body": scam["body"], "category": "Public Safety",
                "byline": "Staff", "action": "approve"}
        self.post(url, form)
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (scam["id"],)), "draft")
        r = self.post(url, {**form, "checked": "0", "featured": "on"})
        self.assertEqual(r.status_code, 302)
        s = d.one("SELECT * FROM stories WHERE id=?", (scam["id"],))
        self.assertEqual(s["status"], "published")
        self.assertEqual(s["featured"], 1)
        self.assertTrue(s["image"].startswith("card-"))
        r = self.c.get("/story/" + s["slug"])
        self.assertIn(s["headline"], r.text)
        self.assertIn("AI assistance", r.text)
        self.assertEqual(self.c.get("/media/" + s["image"]).status_code, 200)
        home = self.c.get("/").text
        self.assertIn(s["headline"], home.split('class="lead"')[1][:2000])  # featured story leads the homepage
        self.assertIn(s["slug"], self.c.get("/feed.xml").text)
        self.assertIn(s["slug"], self.c.get("/sitemap.xml").text)
        self.assertIn(s["headline"], self.c.get("/category/public-safety").text)

    def test_06_schedule_reject_correction(self):
        d = self.db()
        seat = d.one("SELECT * FROM stories WHERE headline LIKE '%seat%' ORDER BY id")
        url = f"/admin/story/{seat['id']}"
        base = {"headline": seat["headline"], "summary": "s", "body": "<p>b</p>", "category": "Public Safety",
                "byline": "Staff"}
        self.post(url, {**base, "action": "schedule", "publish_at": "2020-01-01T08:00"})
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (seat["id"],)), "draft")
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.post(url, {**base, "action": "schedule", "publish_at": future})
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (seat["id"],)), "scheduled")
        d.run("UPDATE stories SET publish_at='2020-01-01T00:00:00+00:00' WHERE id=?", (seat["id"],))
        pipeline.publish_due(d)
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (seat["id"],)), "published")
        slug = d.val("SELECT slug FROM stories WHERE id=?", (seat["id"],))
        self.post(url, {**base, "action": "save", "correction": "An earlier version gave the wrong time."})
        self.assertIn("wrong time", self.c.get("/story/" + slug).text)
        self.assertEqual(d.val("SELECT slug FROM stories WHERE id=?", (seat["id"],)), slug)
        wx = d.one("SELECT * FROM stories WHERE headline LIKE '%Frost%' ORDER BY id")
        self.post(f"/admin/story/{wx['id']}", {**base, "headline": wx["headline"], "action": "reject",
                                               "reject_reason": "dup"})
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (wx["id"],)), "rejected")

    def test_07_tips(self):
        anon = self.app.test_client()
        r = anon.get("/tip")
        ft = re.search(r'name="ft" value="([^"]+)"', r.text).group(1)
        r = self.post("/tip", {"ft": ft, "text": "Tree down on Oak Avenue blocking lanes"}, client=anon)
        self.assertIn("try sending", r.text)
        time.sleep(3.1)
        self.post("/tip", {"ft": ft, "text": "spam spam spam spam", "website": "x"}, client=anon)
        # photos need the permission tick
        r = self.post("/tip", {"ft": ft, "text": "Tree down on Oak Avenue blocking lanes", "location": "Oak Ave",
                               "photos": (jpeg_with_gps(), "p.jpg")}, client=anon, content_type="multipart/form-data")
        self.assertIn("permission", r.text)
        r = self.post("/tip", {"ft": ft, "text": "Tree down on Oak Avenue blocking lanes", "location": "Oak Ave",
                               "name": "Jordan", "contact": "j@example.com", "photo_ok": "on",
                               "photos": (jpeg_with_gps(), "p.jpg")}, client=anon, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        d = self.db()
        t = d.one("SELECT * FROM tips ORDER BY id DESC")
        self.assertEqual(d.val("SELECT COUNT(*) FROM tips"), 1)
        photo = json.loads(t["photos"])[0]
        self.assertFalse(Image.open(dbm.UPLOADS / photo).getexif())
        self.assertEqual(anon.get("/media/" + photo).status_code, 404)
        self.assertEqual(self.c.get("/admin/uploads/" + photo).status_code, 200)
        # the thanks page offers to create an account
        self.assertIn("Get credit for your tip", anon.get("/tip/thanks").text)
        time.sleep(0.1)
        self.post("/tip", {"ft": ft, "text": "Anonymous tip about the quarry noise", "anonymous": "on",
                           "name": "Secret", "contact": "s@example.com"}, client=anon)
        a = d.one("SELECT * FROM tips ORDER BY id DESC")
        self.assertEqual((a["name"], a["contact"]), ("", ""))
        # develop the tip: unverified, so low confidence with a checklist
        with claude_patch():
            r = self.post(f"/admin/tips/{t['id']}", {"action": "develop"})
            self.assertIn("/admin/develop/", r.headers["Location"])
            sid = int(r.headers["Location"].rsplit("/", 1)[1])
            self.post(f"/admin/develop/{sid}", {"action": "write"})
        st = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual(st["tip_id"], t["id"])
        self.assertEqual(st["confidence"], "low")
        self.assertIn("unverified tip", st["checklist"])
        self.assertEqual(d.val("SELECT status FROM tips WHERE id=?", (t["id"],)), "used")
        # the photo can be used, with credit
        self.post(f"/admin/story/{sid}", {"headline": st["headline"], "action": "use_photo", "photo": photo})
        st = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual((st["image"], st["photo_credit"]), (photo, "Photo: submitted"))

    def test_08_manual_add_link(self):
        d = self.db()
        manual = d.one("SELECT * FROM sources WHERE type='manual'")
        with claude_patch():
            r = self.post("/admin/manual", {"text": "https://article.example/ambulance", "trust": "outlet"})
            self.assertIn("/admin/develop/", r.headers["Location"])
            sid = int(r.headers["Location"].rsplit("/", 1)[1])
            dev = json.loads(d.val("SELECT dev FROM stories WHERE id=?", (sid,)))
            self.assertIn("310,000", dev["sources"][0]["text"])   # the link was opened and read
            self.post(f"/admin/develop/{sid}", {"action": "write"})
        st = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual(st["source_id"], manual["id"])
        self.assertIn("another outlet", st["checklist"])
        self.assertIn("word-for-word", st["checklist"]) if "commissioners voted" in st["body"] else None
        self.assertIn("article.example", st["cites"])

    def test_09_settings_and_writing_instructions(self):
        from app import settings, writing
        r = self.post("/admin/settings/coverage", {"town": "Riverton", "county": "Polk", "state": "IA",
                                                   "nearby": ["Millbrook", "Cedar Falls"], "zips__text": ""})
        self.assertEqual(r.status_code, 302)
        d = self.db()
        self.assertEqual(settings.get(d, "nearby"), ["Millbrook", "Cedar Falls"])
        self.post("/admin/settings/ai", {"ai_key": "", "ai_model": "claude-sonnet-5", "ai_cap": "25"})
        from app.security import get_secret
        self.assertEqual(get_secret(d, "setting:ai_key"), "sk-test-123")
        self.assertEqual(settings.get(d, "ai_cap"), 25)
        self.assertNotIn("sk-test-123", self.c.get("/admin/settings/ai").text)
        # the writing instructions are editable; the locked rules always stay
        vals = settings.all_values(d)
        form = {k: vals[k] for k in settings.FIELDS if k.startswith(("write_", "tmpl_", "style_example"))}
        form["write_banned"] = "Never say 'bustling'."
        form["style_example_1"] = "EXAMPLE ARTICLE TEXT"
        form["tmpl_obituary"] = "Warm and factual."
        self.post("/admin/settings/writing", form)
        system = writing.write_system(d, "obituary")
        self.assertIn("Never say 'bustling'.", system)
        self.assertIn("EXAMPLE ARTICLE TEXT", system)
        self.assertIn("Warm and factual.", system)
        self.assertIn("Always write it. Never refuse", system)
        self.assertIn("never judge", system)
        self.assertNotIn("skip", system.lower().replace("never a reason to skip", ""))
        self.assertIn("never a reason to skip", writing.analyze_system(d))

    def test_10_roles_and_security(self):
        r = self.post("/admin/settings/people", {"action": "add", "email": "rev@example.com", "name": "Rev",
                                                 "role": "reviewer"})
        temp = re.search(r"font-size:16px\">([^<]+)</b>", r.text).group(1)
        rev = self.app.test_client()
        self.post("/admin/login", {"email": "rev@example.com", "password": temp}, client=rev)
        d = self.db()
        draft = d.one("SELECT * FROM stories WHERE status='draft' ORDER BY id")
        r = self.post(f"/admin/story/{draft['id']}", {"headline": draft["headline"], "action": "approve",
                                                      "checked": [str(i) for i in range(9)]}, client=rev)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.post("/admin/items", {"item": ["1"], "action": "develop"}, client=rev).status_code, 403)
        self.assertEqual(rev.get("/admin/settings/ai").status_code, 403)
        self.assertEqual(rev.get("/admin/community/members").status_code, 403)
        r = self.c.post("/admin/check-now")
        self.assertEqual(r.status_code, 400)
        bad = self.app.test_client()
        for _ in range(9):
            r = self.post("/admin/login", {"email": "pat@example.com", "password": "nope"}, client=bad)
        self.assertIn("Too many attempts", r.text)
        self.assertIn("frame-ancestors 'none'", self.c.get("/").headers["Content-Security-Policy"])

    def test_11_backup(self):
        r = self.post("/admin/settings/backups", {})
        self.assertEqual(r.status_code, 302)
        files = list(dbm.BACKUPS.glob("newsroom-manual-*.zip"))
        self.assertEqual(len(files), 1)
        import zipfile
        names = zipfile.ZipFile(files[0]).namelist()
        self.assertIn("newsroom.db", names)
        self.assertIn("secret.key", names)
        self.assertEqual(self.c.get("/admin/settings/backups/" + files[0].name).status_code, 200)
        self.assertEqual(self.c.get("/admin/settings/backups/..%2Fnewsroom.db").status_code, 404)

    def test_12_public_pages(self):
        for u in ["/", "/weather", "/events", "/search?q=scam", "/category/public-safety", "/page/about", "/page/ai",
                  "/tip", "/manifest.webmanifest", "/icon-512.png", "/robots.txt", "/sw.js", "/offline", "/all", "/top",
                  "/top?t=all", "/national", "/world", "/national-world", "/category/national-world", "/leaderboard",
                  "/leaderboard?period=all", "/badges", "/partners", "/specials", "/submit", "/join",
                  "/login", "/forgot"]:
            r = self.c.get(u)
            self.assertIn(r.status_code, (200, 302), u)
        self.assertIn("scam", self.c.get("/search?q=scam").text.lower())
        self.assertIn("phone scam", self.c.get("/all").text)
        self.assertEqual(self.c.get("/story/nope").status_code, 404)
        self.assertEqual(self.c.get("/media/../secret.key").status_code, 404)
        self.assertEqual(self.c.get("/me").status_code, 302)  # members only
        self.assertIn("Disallow: /me", self.c.get("/robots.txt").text)

    def test_13_review_fixes(self):
        from app import util
        from app.sources import http
        self.assertNotIn("img", util.clean_html('<p>a<!--><img src=x onerror=alert(1)>--></p>'))
        self.assertEqual(util.clean_html("<form><span>x</span></form><svg><g>y</g></svg><p>ok</p>"), "<p>ok</p>")
        self.assertNotIn("javascript", util.clean_html('<a href=" javascript:alert(1)">x</a>'))
        for bad in ("http://127.0.0.1/feed", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/x"):
            with self.assertRaises(http.FetchError):
                http._check_public(bad)
        big = io.BytesIO()
        Image.new("1", (9000, 9000)).save(big, "PNG")
        big.seek(0)

        class FS:
            filename = "big.png"

            def read(self, n):
                return big.read(n)
        self.assertIsNone(util.save_image(FS()))
        d = self.db()
        pub = d.one("SELECT * FROM stories WHERE status='published' ORDER BY id")
        rev = self.app.test_client()
        d.run("DELETE FROM rate")
        d.run("UPDATE users SET password_hash=? WHERE email='rev@example.com'",
              (__import__("app.security", fromlist=["x"]).hash_password("reviewer-pass-1"),))
        self.post("/admin/login", {"email": "rev@example.com", "password": "reviewer-pass-1"}, client=rev)
        r = self.post(f"/admin/story/{pub['id']}", {"headline": "HACKED", "action": "save"}, client=rev)
        self.assertEqual(r.status_code, 403)
        self.assertNotEqual(d.val("SELECT headline FROM stories WHERE id=?", (pub["id"],)), "HACKED")
        dr = d.one("SELECT * FROM stories WHERE status='draft' AND checklist != '[]' ORDER BY id")
        d.update("stories", dr["id"], status="scheduled", publish_at="2020-01-01T00:00:00+00:00")
        pipeline.publish_due(d)
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (dr["id"],)), "draft")
        # with the AI paused you can still start developing and add your own details
        from app import settings as st
        st.put(d, "ai_paused", True)
        r = self.post("/admin/manual", {"text": "x " * 40, "trust": "tip"}, follow_redirects=True)
        self.assertIn("paused", r.text)
        self.assertIn("Write the article", r.text)
        st.put(d, "ai_paused", False)
        st.put(d, "smtp_host", "127.0.0.1")
        self.assertFalse(util.send_email(d, "a@example.com", "Fire on Main\nsmoke", "b"))
        st.put(d, "smtp_host", "")

    def test_14_setup_code_rate_limited(self):
        d = self.db()
        d.run("DELETE FROM rate")
        with mock.patch.dict(os.environ, {"SETUP_CODE": "right-code"}):
            d.run("UPDATE users SET active=0 WHERE role='owner'")
            try:
                c = self.app.test_client()
                last = None
                for _ in range(12):
                    last = self.post("/admin/setup?step=1", {"setup_code": "wrong", "email": "z@example.com",
                                                             "password": "correct-horse-9",
                                                             "password2": "correct-horse-9"}, client=c)
                self.assertIn("Too many attempts", last.text)
            finally:
                d.run("UPDATE users SET active=1 WHERE role='owner'")

    def test_15_weather(self):
        import app.weather as wx
        d = self.db()
        c = wx.cfg(d)
        self.assertEqual([l_["name"] for l_ in c["weather_locations"]], ["Riverton"])
        self.assertEqual(wx.refresh_if_due(d, force=True), 1)
        r = self.c.get("/weather")
        self.assertIn("68°F", r.text)
        self.assertIn("Tornado Warning", r.text)
        home = self.c.get("/").text
        self.assertIn("TORNADO WARNING", home)
        self.assertIn("Full forecast", home)
        self.assertEqual(self.c.get("/admin/weather").status_code, 200)
        r = self.post("/admin/weather", {"action": "search", "q": "Riverton, Iowa"})
        self.assertIn("Polk County", r.text)
        self.post("/admin/weather", {"action": "add", "name": "Millbrook", "lat": "41.7", "lon": "-93.5"})
        self.assertEqual(len(wx.cfg(d)["weather_locations"]), 2)
        self.post("/admin/weather", {"action": "remove", "slug": "millbrook"})
        self.assertEqual(len(wx.cfg(d)["weather_locations"]), 1)

    def test_16_no_double_developing_and_stale_alerts(self):
        import threading
        import app.weather as wx
        d = self.db()
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        iid = d.insert("items", source_id=src, hash="dbl", title="Riverton council meets", text="Agenda posted.",
                       fetched_at=dbm.now())
        results = []

        def go():
            results.append(pipeline.start_development(dbm.DB(), [iid]))
        threads = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(1 for sid, _ in results if sid), 1)
        self.assertEqual(d.val("SELECT COUNT(*) FROM stories WHERE status='developing' AND headline LIKE 'Riverton "
                               "council%'"), 1)
        old = [{"event": "Old Warning", "severity": "Extreme", "expires": "2020-01-01T00:00:00+00:00"}]
        self.assertEqual(wx.current_alerts(old, dbm.now()), [])
        with mock.patch.object(wx, "fetch", side_effect=wx.FetchError("down")) as f:
            wx.refresh_if_due(d, force=True)
            wx.refresh_if_due(d)
            self.assertEqual(f.call_count, 1)

    # ── members ────────────────────────────────────────────
    def test_17_join_and_guest_tip_moves_to_account(self):
        d = self.db()
        guest = self.app.test_client()
        ft = re.search(r'name="ft" value="([^"]+)"', guest.get("/tip").text).group(1)
        time.sleep(3.1)
        self.post("/tip", {"ft": ft, "text": "Water main break on 5th Street flooding the road"}, client=guest)
        tid = d.val("SELECT id FROM tips ORDER BY id DESC")
        d.run("DELETE FROM rate")
        r = self.join({"username": "ab", "email": "x@example.com", "password": "long-enough-1"}, client=guest)
        self.assertIn("3 to 20", r.text)
        r = self.join({"username": "admin", "email": "x@example.com", "password": "long-enough-1"}, client=guest)
        self.assertIn("3 to 20", r.text)
        r = self.join({"username": "casey", "email": "casey@example.com", "password": "long-enough-1"},
                      client=guest)
        self.assertEqual(r.status_code, 302)
        mid = self.mid("casey")
        self.assertEqual(d.val("SELECT member_id FROM tips WHERE id=?", (tid,)), mid)
        me = guest.get("/me").text
        self.assertIn("Water main break", me)
        self.assertIn("Waiting for the newsroom", me)
        # founding member badge (joined in the first 90 days)
        self.assertIn("Founding Member", guest.get("/u/casey").text)
        # duplicate username / email refused
        other = self.app.test_client()
        r = self.join({"username": "Casey", "email": "c2@example.com", "password": "long-enough-1"}, client=other)
        self.assertIn("taken", r.text)
        # log out and back in with username
        self.post("/logout", {}, client=guest)
        self.assertEqual(guest.get("/me").status_code, 302)
        r = self.post("/login", {"email": "casey", "password": "long-enough-1"}, client=guest)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(guest.get("/me").status_code, 200)
        # members can't reach the dashboard
        self.assertEqual(guest.get("/admin/").status_code, 302)
        self.assertIn("/admin/login", guest.get("/admin/").headers["Location"])
        # a logged-in member's tip goes to their account, with their credit choice
        ft = re.search(r'name="ft" value="([^"]+)"', guest.get("/tip").text).group(1)
        self.assertIn("Sending as", guest.get("/tip").text)
        time.sleep(3.1)
        self.post("/tip", {"ft": ft, "text": "The diner on Main is closing Friday after 40 years", "credit": "1"},
                  client=guest)
        t = d.one("SELECT * FROM tips ORDER BY id DESC")
        self.assertEqual((t["member_id"], t["credit"], t["name"]), (mid, 1, "@casey"))
        # developing it credits the member when published
        with claude_patch():
            r = self.post(f"/admin/tips/{t['id']}", {"action": "develop"})
            sid = int(r.headers["Location"].rsplit("/", 1)[1])
            self.post(f"/admin/develop/{sid}", {"action": "write"})
        self.approve(sid)
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual((s["status"], s["member_id"], s["credit"]), ("published", mid, "tip"))
        self.assertIn("Tip from", self.c.get("/story/" + s["slug"]).text)
        self.assertEqual(d.val("SELECT points FROM members WHERE id=?", (mid,)), 10)
        self.assertIn("Tipster", guest.get("/u/casey").text)
        self.assertIn("Published", guest.get("/me").text)

    def test_18_submissions(self):
        d = self.db()
        m = self.member_client("jamie")
        mid = self.mid("jamie")
        self.assertIn("Write it myself", m.get("/submit").text)
        # just the facts → the newsroom develops it
        r = self.post("/submit/new/facts", {"action": "submit", "fact_what": "The library roof leaked onto the "
                                            "children's books", "fact_when": "Sunday night", "credit": "1"}, client=m)
        self.assertEqual(r.status_code, 302)
        sub = d.one("SELECT * FROM submissions WHERE member_id=? ORDER BY id DESC", (mid,))
        self.assertEqual((sub["status"], sub["kind"]), ("waiting", "facts"))
        self.assertIn("library roof", self.c.get("/admin/members/submissions").text)
        self.assertIn("Develop this story", self.c.get(f"/admin/members/submissions/{sub['id']}").text)
        with claude_patch():
            r = self.post(f"/admin/members/submissions/{sub['id']}", {"action": "develop"})
            sid = int(r.headers["Location"].rsplit("/", 1)[1])
            self.post(f"/admin/develop/{sid}", {"action": "write"})
        self.approve(sid)
        self.assertEqual(d.val("SELECT status FROM submissions WHERE id=?", (sub["id"],)), "published")
        self.assertEqual(d.val("SELECT points FROM members WHERE id=?", (mid,)), 10)
        # write it myself → Publish as written: "By @jamie", +20, no AI
        body = "<p>" + "The Riverton FFA won the state tractor-driving contest on Saturday. " * 3 + "</p>"
        r = self.post("/submit/new/article", {"action": "submit", "headline": "FFA wins state contest", "body": body,
                                              "category": "Farm & Rural", "credit": "1"}, client=m)
        sub = d.one("SELECT * FROM submissions WHERE member_id=? ORDER BY id DESC", (mid,))
        with mock.patch("app.ai.call_claude") as never:
            self.post(f"/admin/members/submissions/{sub['id']}", {"action": "publish"})
            never.assert_not_called()
        s = d.one("SELECT * FROM stories WHERE submission_id=?", (sub["id"],))
        self.assertEqual((s["status"], s["credit"], s["category"]), ("published", "byline", "Farm & Rural"))
        page = self.c.get("/story/" + s["slug"]).text
        self.assertIn("@jamie", page)
        self.assertNotIn("Edited by the newsroom", page)
        self.assertNotIn("AI assistance", page)  # nothing AI about it
        self.assertEqual(d.val("SELECT points FROM members WHERE id=?", (mid,)), 30)
        self.assertIn("Byline", m.get("/u/jamie").text)
        # send back with a note, member edits and resends
        self.post("/submit/new/article", {"action": "submit", "headline": "Bake sale raises money for band",
                                          "body": body.replace("FFA", "Band"), "credit": "0"}, client=m)
        sub = d.one("SELECT * FROM submissions WHERE member_id=? ORDER BY id DESC", (mid,))
        r = self.post(f"/admin/members/submissions/{sub['id']}", {"action": "send_back", "note": ""}, follow_redirects=True)
        self.assertIn("Add a note", r.text)
        self.post(f"/admin/members/submissions/{sub['id']}", {"action": "send_back", "note": "How much was raised?"})
        self.assertIn("How much was raised?", m.get("/me").text)
        self.assertIn("How much was raised?", m.get(f"/submit/{sub['id']}").text)
        self.post(f"/submit/{sub['id']}", {"action": "submit", "headline": "Bake sale raises $900 for band",
                                           "body": body.replace("FFA", "Band"), "credit": "0"}, client=m)
        self.assertEqual(d.val("SELECT status FROM submissions WHERE id=?", (sub["id"],)), "waiting")
        # edit then publish: the note shows, and the member asked not to be named
        r = self.post(f"/admin/members/submissions/{sub['id']}", {"action": "edit"})
        sid = int(r.headers["Location"].rsplit("/", 1)[1])
        self.approve(sid, edited_note="on")
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        page = self.c.get("/story/" + s["slug"]).text
        self.assertIn("Edited by the newsroom", page)
        self.assertIn("a community member", page)
        self.assertNotIn("@jamie", page)
        self.assertEqual(d.val("SELECT points FROM members WHERE id=?", (mid,)), 50)  # points still earned
        # other members can't see or edit someone else's submission
        other = self.member_client("nosy_neighbor")
        self.assertEqual(other.get(f"/submit/{sub['id']}").status_code, 404)
        # photos need the permission tick; then they're private until published
        r = self.post("/submit/new/facts", {"action": "draft", "fact_what": "Photo of the new mural",
                                            "photos": (jpeg_with_gps(), "m.jpg")}, client=m,
                      content_type="multipart/form-data")
        self.assertIn("permission", r.text)
        r = self.post("/submit/new/facts", {"action": "draft", "fact_what": "Photo of the new mural", "photo_ok": "on",
                                            "photos": (jpeg_with_gps(), "m.jpg")}, client=m,
                      content_type="multipart/form-data")
        sub = d.one("SELECT * FROM submissions WHERE member_id=? ORDER BY id DESC", (mid,))
        self.assertEqual(sub["status"], "draft")
        photo = json.loads(sub["photos"])[0]
        self.assertEqual(m.get(f"/me/photo/{photo}").status_code, 200)
        self.assertEqual(other.get(f"/me/photo/{photo}").status_code, 404)
        self.assertEqual(other.get(f"/media/{photo}").status_code, 404)
        # trusted members publish straight away
        d.run("UPDATE members SET trusted=1 WHERE id=?", (mid,))
        r = self.post("/submit/new/article", {"action": "submit", "headline": "Trusted story goes live",
                                              "body": body, "credit": "1"}, client=m)
        self.assertIn("/story/", r.headers["Location"])
        s = d.one("SELECT * FROM stories WHERE headline='Trusted story goes live'")
        self.assertEqual((s["status"], s["trusted_publish"]), ("published", 1))
        self.assertIn("Trusted story goes live", self.c.get("/admin/members/submissions?tab=trusted").text)
        # members never get AI: there's no route for it, and the develop page is staff-only
        self.assertEqual(m.get(f"/admin/develop/{sid}").status_code, 302)

    def test_19_votes_comments_flags(self):
        d = self.db()
        from app import settings as st
        story = d.one("SELECT * FROM stories WHERE status='published' AND member_id=? ORDER BY id",
                      (self.mid("jamie"),))
        a = self.member_client("alex")
        b = self.member_client("blair")
        jamie_points = d.val("SELECT points FROM members WHERE username='jamie'")
        # upvote (JSON), toggle off, can't vote own
        r = self.post("/vote", {"target": "story", "id": story["id"]}, client=a, headers={"X-Requested-With": "fetch"})
        self.assertEqual(r.get_json(), {"ok": True, "voted": True, "count": 1})
        self.assertEqual(d.val("SELECT points FROM members WHERE username='jamie'"), jamie_points + 1)
        r = self.post("/vote", {"target": "story", "id": story["id"]}, client=a, headers={"X-Requested-With": "fetch"})
        self.assertEqual(r.get_json()["count"], 0)
        self.assertEqual(d.val("SELECT points FROM members WHERE username='jamie'"), jamie_points)
        jamie = self.app.test_client()
        self.post("/login", {"email": "jamie", "password": "member-pass-1"}, client=jamie)
        r = self.post("/vote", {"target": "story", "id": story["id"]}, client=jamie, headers={"X-Requested-With": "fetch"})
        self.assertFalse(r.get_json()["ok"])
        anon = self.app.test_client()
        self.assertEqual(self.post("/vote", {"target": "story", "id": story["id"]}, client=anon,
                                   headers={"X-Requested-With": "fetch"}).status_code, 403)
        # comments: a new member's first ones wait for approval
        self.post("/comment", {"story_id": story["id"], "body": "Congratulations to the team!"}, client=a)
        c1 = d.one("SELECT * FROM comments ORDER BY id DESC")
        self.assertEqual(c1["status"], "held")
        self.assertNotIn("Congratulations", anon.get("/story/" + story["slug"]).text)
        self.assertIn("Congratulations", a.get("/story/" + story["slug"]).text)  # the author sees it's waiting
        self.post("/admin/community/flags", {"action": "approve", "id": c1["id"]})
        self.assertIn("Congratulations", anon.get("/story/" + story["slug"]).text)
        self.assertIn("Town Talker", a.get("/u/alex").text)
        # replies are threaded and notify the parent's author
        self.post("/comment", {"story_id": story["id"], "parent_id": c1["id"], "body": "Agreed, great job."}, client=b)
        c2 = d.one("SELECT * FROM comments ORDER BY id DESC")
        self.assertEqual(c2["parent_id"], c1["id"])
        self.post("/admin/community/flags", {"action": "approve", "id": c2["id"]})
        self.assertTrue(d.val("SELECT 1 FROM notices WHERE member_id=? AND kind='reply'", (self.mid("alex"),)))
        # comment upvote gives the author a point
        self.post("/vote", {"target": "comment", "id": c1["id"]}, client=b)
        self.assertEqual(d.val("SELECT upvotes FROM comments WHERE id=?", (c1["id"],)), 1)
        # alex: 1 Work for the comment, 1 Social for blair's reply, 1 Social for the upvote
        alex = d.one("SELECT * FROM members WHERE username='alex'")
        self.assertEqual((alex["points"], alex["work_points"], alex["social_points"]), (3, 1, 2))
        # jamie wrote the story, so alex's comment on it counts as a reply to jamie
        self.assertTrue(d.val("SELECT 1 FROM points_log WHERE member_id=? AND reason='reply'", (self.mid("jamie"),)))
        # hiding a comment takes its points back, for the writer and for the person it answered
        self.post("/admin/community/flags", {"action": "hide", "id": c2["id"]})
        alex = d.one("SELECT * FROM members WHERE username='alex'")
        self.assertEqual((alex["work_points"], alex["social_points"]), (1, 1))
        self.assertFalse(d.val("SELECT 1 FROM points_log WHERE member_id=? AND reason='comment' AND ref=?",
                               (self.mid("blair"), f"comment:{c2['id']}")))
        self.post("/admin/community/flags", {"action": "approve", "id": c2["id"]})
        page = self.c.get(f"/admin/community/members/{self.mid('alex')}").text
        self.assertIn("Work 1 · Social 2", page)
        self.assertIn(">Work<", self.c.get("/admin/community/members?sort=social").text)
        # daily cap on points from one member to another
        st.put(d, "pts_daily_cap", 1)
        s2 = d.one("SELECT * FROM stories WHERE status='published' AND member_id=? AND id!=? ORDER BY id",
                   (self.mid("jamie"), story["id"]))
        before = d.val("SELECT points FROM members WHERE username='jamie'")
        self.post("/vote", {"target": "story", "id": story["id"]}, client=b)
        self.post("/vote", {"target": "story", "id": s2["id"]}, client=b)
        self.assertEqual(d.val("SELECT points FROM members WHERE username='jamie'"), before + 1)
        st.put(d, "pts_daily_cap", 5)
        # blocked words hold a comment; links only for members with enough points
        st.put(d, "blocked_words", ["scamlink"])
        from app import community
        community.award(d, self.mid("blair"), 30 - d.val("SELECT points FROM members WHERE username='blair'"),
                        "bonus", "test")  # points always come from the log
        self.post("/comment", {"story_id": story["id"], "body": "visit scamlink now"}, client=b)
        self.assertEqual(d.val("SELECT hold_reason FROM comments ORDER BY id DESC"), "Contains a blocked word (scamlink)")
        self.post("/comment", {"story_id": story["id"], "body": "More at https://example.org/info"}, client=b)
        c3 = d.one("SELECT * FROM comments ORDER BY id DESC")
        self.assertEqual(c3["status"], "visible")  # 25+ points: no longer held
        page = anon.get("/story/" + story["slug"]).text
        self.assertNotIn('href="https://example.org/info"', page)  # under 100 points: link not clickable
        # edit within the window, delete any time
        self.post(f"/comment/{c3['id']}/edit", {"body": "More at the library website"}, client=b)
        self.assertEqual(d.val("SELECT body FROM comments WHERE id=?", (c3["id"],)), "More at the library website")
        self.assertEqual(self.post(f"/comment/{c3['id']}/edit", {"body": "hacked"}, client=a).status_code, 403)
        self.post(f"/comment/{c3['id']}/delete", {}, client=b)
        self.assertEqual(d.val("SELECT status FROM comments WHERE id=?", (c3["id"],)), "deleted")
        # flags: 3 hide a comment for review
        flaggers = [self.member_client(f"flagger{i}") for i in range(3)]
        for fc in flaggers:
            self.post("/flag", {"target": "comment", "id": c2["id"], "reason": "offensive"}, client=fc)
        self.assertEqual(d.val("SELECT status FROM comments WHERE id=?", (c2["id"],)), "held")
        self.assertIn("Agreed, great job", self.c.get("/admin/community/flags?tab=flags").text)
        # a flag on a story that led to a correction thanks the flagger
        self.post("/flag", {"target": "story", "id": story["id"], "reason": "facts", "note": "Wrong date"},
                  client=flaggers[0])
        self.post("/admin/community/flags", {"action": "correction", "target": "story", "target_id": story["id"]})
        self.assertIn("Good Eye", flaggers[0].get("/u/flagger0").text)
        # comments can be locked or turned off per story
        d.run("UPDATE stories SET comments_mode='locked' WHERE id=?", (story["id"],))
        self.post("/comment", {"story_id": story["id"], "body": "Too late?"}, client=a)
        self.assertEqual(d.val("SELECT COUNT(*) FROM comments WHERE body='Too late?'"), 0)
        d.run("UPDATE stories SET comments_mode='open' WHERE id=?", (story["id"],))
        # moderation: suspend and ban
        mid = self.mid("blair")
        self.post(f"/admin/community/members/{mid}", {"action": "suspend", "days": "3"})
        self.post("/comment", {"story_id": story["id"], "body": "Can I still post?"}, client=b)
        self.assertEqual(d.val("SELECT COUNT(*) FROM comments WHERE body='Can I still post?'"), 0)
        self.post(f"/admin/community/members/{mid}", {"action": "ban"})
        banned = self.app.test_client()
        r = self.post("/login", {"email": "blair", "password": "member-pass-1"}, client=banned)
        self.assertIn("closed", r.text)
        self.assertEqual(b.get("/me").status_code, 302)  # logged out
        # featured gives points once; leaderboard lists members
        jp = d.val("SELECT points FROM members WHERE username='jamie'")
        for _ in range(2):
            self.post(f"/admin/story/{story['id']}", {"headline": story["headline"], "body": story["body"],
                                                      "category": story["category"], "action": "save", "featured": "on"})
        self.assertEqual(d.val("SELECT points FROM members WHERE username='jamie'"), jp + 15)
        self.assertIn("@jamie", self.c.get("/leaderboard?period=all").text)
        self.assertIn("@jamie", self.c.get("/leaderboard").text)

    def test_20_staff_profile_and_partners(self):
        d = self.db()
        self.post("/admin/account/profile", {"username": "pat_news"})
        mid = self.mid("pat_news")
        self.assertTrue(mid)
        story = d.one("SELECT * FROM stories WHERE status='published' AND kind='story' ORDER BY id")
        self.post("/comment", {"story_id": story["id"], "body": "Thanks for reading, everyone."})
        c = d.one("SELECT * FROM comments ORDER BY id DESC")
        self.assertEqual((c["member_id"], c["status"]), (mid, "visible"))  # staff aren't held
        self.assertIn("Staff", self.c.get("/story/" + story["slug"]).text)
        # partner application → approval → badge; no logo strip on the section page
        m = self.member_client("stannes")
        logo = io.BytesIO()
        Image.new("RGB", (60, 60), "blue").save(logo, "PNG")
        logo.seek(0)
        self.post("/partners", {"name": "St. Anne's Church", "category": "Faith & Churches",
                                "description": "Parish in Riverton", "calendar_url": "https://cal.example/st.ics",
                                "logo": (logo, "logo.png")}, client=m, content_type="multipart/form-data")
        org = d.one("SELECT * FROM orgs")
        self.assertEqual(org["status"], "pending")
        self.assertEqual(m.get("/media/" + org["logo"]).status_code, 404)  # private until approved
        self.post("/admin/community/partners", {"id": org["id"], "action": "approve"})
        self.assertIn("Community Partner", m.get("/u/stannes").text)
        page = self.c.get("/category/faith-churches").text.split('id="content"')[1]
        self.assertNotIn("St. Anne", page)
        self.assertIn("Become a community partner", page)
        self.assertEqual(self.c.get("/media/" + org["logo"]).status_code, 200)
        self.post("/admin/community/partners", {"id": org["id"], "action": "calendar"})
        self.assertTrue(d.val("SELECT 1 FROM sources WHERE type='calendar' AND name LIKE 'St. Anne%'"))
        # partners can send news for their organization
        body = "<p>" + "St. Anne's will hold a pancake breakfast Sunday from 8 to 11 a.m. in the hall. " * 2 + "</p>"
        self.post("/submit/new/article", {"action": "submit", "headline": "Pancake breakfast Sunday", "body": body,
                                          "org_id": str(org["id"]), "credit": "1"}, client=m)
        sub = d.one("SELECT * FROM submissions ORDER BY id DESC")
        self.assertEqual(sub["org_id"], org["id"])
        before = d.val("SELECT points FROM members WHERE username='stannes'")
        self.post(f"/admin/members/submissions/{sub['id']}", {"action": "publish"})
        s = d.one("SELECT * FROM stories WHERE submission_id=?", (sub["id"],))
        # posted for the organization: it gets the credit, the person earns no points
        self.assertEqual(d.val("SELECT points FROM members WHERE username='stannes'"), before)
        self.assertIn("Published for your organization", d.val(
            "SELECT text FROM notices WHERE member_id=? ORDER BY id DESC", (self.mid("stannes"),)))
        fan = self.member_client("parish_fan")
        self.post("/vote", {"target": "story", "id": s["id"]}, client=fan)
        self.assertEqual(d.val("SELECT upvotes FROM stories WHERE id=?", (s["id"],)), 1)   # the vote counts…
        self.assertEqual(d.val("SELECT points FROM members WHERE username='stannes'"), before)  # …but no points
        page = self.c.get("/story/" + s["slug"]).text
        self.assertIn("Church, a community partner", page)
        # their story ends with an author box: logo, about them, and more of their stories
        box = page.split('class="authorbox"')[1].split("</aside>")[0]
        self.assertIn("St. Anne&#39;s Church", box)
        self.assertIn("Parish in Riverton", box)
        self.assertIn("/media/" + org["logo"], box)
        self.assertNotIn("More from", box)
        t = dbm.now()
        d.insert("stories", headline="Fall festival at St. Anne's", slug="st-annes-fall-festival", status="published",
                 org_id=org["id"], published_at=t, created_at=t, updated_at=t)
        box = self.c.get("/story/" + s["slug"]).text.split('class="authorbox"')[1].split("</aside>")[0]
        self.assertIn("Fall festival at St. Anne", box)
        # the partner manages their own box, with a live preview; only they can
        self.assertIn(f"/partners/{org['id']}/manage", m.get("/me").text)
        page = m.get(f"/partners/{org['id']}/manage").text
        self.assertIn("data-ab-preview", page)
        self.assertEqual(self.member_client("other_parish").get(f"/partners/{org['id']}/manage").status_code, 403)
        r = self.post(f"/partners/{org['id']}/manage", {"description": "x" * 301}, client=m)
        self.assertIn("Keep “About us” to 300 characters", r.text)
        new_logo = io.BytesIO()
        Image.new("RGB", (60, 60), "green").save(new_logo, "PNG")
        new_logo.seek(0)
        self.post(f"/partners/{org['id']}/manage", {"description": "Catholic parish on Elm Street. Mass Sundays at 9.",
                                                   "address": "4 Elm St", "website": "https://stannes.example",
                                                   "calendar_url": "", "logo": (new_logo, "new.png")},
                  client=m, content_type="multipart/form-data")
        o2 = d.one("SELECT * FROM orgs WHERE id=?", (org["id"],))
        self.assertNotEqual(o2["logo"], org["logo"])
        self.assertEqual(o2["needs_look"], 1)
        box = self.c.get("/story/" + s["slug"]).text.split('class="authorbox"')[1].split("</aside>")[0]
        self.assertIn("Mass Sundays at 9", box)                                    # live on every story at once
        self.assertIn("https://stannes.example", box)
        self.assertIn("/media/" + o2["logo"], box)
        self.assertEqual(self.c.get("/media/" + o2["logo"]).status_code, 200)
        # the newsroom is told, and can clear it once they've looked
        admin_page = self.c.get("/admin/community/partners").text
        self.assertIn("changed their author box", admin_page)
        self.assertTrue(d.val("SELECT 1 FROM activity WHERE action='partner updated their author box'"))
        self.post("/admin/community/partners", {"id": org["id"], "action": "looked"})
        self.assertEqual(d.val("SELECT needs_look FROM orgs WHERE id=?", (org["id"],)), 0)
        # removing the logo falls back to their first letter; the newsroom can edit every field too
        self.post(f"/partners/{org['id']}/manage", {"description": "Parish.", "remove_logo": "on"}, client=m)
        box = self.c.get("/story/" + s["slug"]).text.split('class="authorbox"')[1].split("</aside>")[0]
        self.assertIn('class="ab-mark"', box)
        self.assertNotIn("stannes.example", box)                                    # they cleared the website
        self.post("/admin/community/partners", {"id": org["id"], "action": "edit", "name": "St. Anne's Church",
                                                "category": "Faith & Churches", "description": "Parish.",
                                                "website": "https://stannes.example/new"})
        self.assertEqual(d.val("SELECT website FROM orgs WHERE id=?", (org["id"],)), "https://stannes.example/new")

    # ── sharing, wire, directory ────────────────────────────
    def test_21_social_sharing(self):
        from app import social, settings as st
        d = self.db()
        r = self.post("/admin/social", {"platform": "bluesky", "bsky_handle": "riverton.bsky.social",
                                        "bsky_password": "app-pass-1", "bsky_service": "https://bsky.social"})
        self.assertTrue(social.connected(d, "bluesky"))
        self.assertNotIn("app-pass-1", self.c.get("/admin/social").text)
        self.assertFalse(social.connected(d, "facebook"))
        story = d.one("SELECT * FROM stories WHERE status='draft' AND kind='story' ORDER BY id DESC")
        page = self.c.get(f"/admin/story/{story['id']}").text
        self.assertIn('name="share_bluesky"', page)
        self.assertIn(">Connect<", page)  # unconnected platforms link to Social accounts
        posted = []

        def fake_poster(db, s, text, link):
            posted.append((s["id"], text, link))
            if "fail" in text:
                raise social.SocialError("400: bad request")
            return "https://bsky.app/profile/riverton.bsky.social/post/abc"
        self.approve(story["id"], share_form="1", share_bluesky="on", share_text_bluesky="Read this story")
        social.post_pending(d, poster=fake_poster)
        s = d.one("SELECT * FROM stories WHERE id=?", (story["id"],))
        share = json.loads(s["social"])
        self.assertEqual(share["bluesky"]["status"], "posted")
        self.assertEqual(posted[0][1], "Read this story")
        self.assertEqual(posted[0][2], f"https://news.example/story/{s['slug']}")
        social.post_pending(d, poster=fake_poster)
        self.assertEqual(len(posted), 1)  # never posted twice
        # a failure shows the reason and can be retried
        other = d.one("SELECT * FROM stories WHERE status='draft' AND kind='story' ORDER BY id DESC")
        self.approve(other["id"], share_form="1", share_bluesky="on", share_text_bluesky="this will fail")
        social.post_pending(d, poster=fake_poster)
        share = json.loads(d.val("SELECT social FROM stories WHERE id=?", (other["id"],)))
        self.assertEqual((share["bluesky"]["status"], share["bluesky"]["error"]), ("failed", "400: bad request"))
        self.assertIn("Try again", self.c.get(f"/admin/story/{other['id']}").text)
        self.post(f"/admin/story/{other['id']}", {"headline": other["headline"], "action": "retry_social",
                                                  "platform": "bluesky"})
        self.assertEqual(json.loads(d.val("SELECT social FROM stories WHERE id=?", (other["id"],)))["bluesky"]["status"],
                         "pending")
        # the X signature header is well formed
        for k in ("x_api_key", "x_api_secret", "x_access_token", "x_access_secret"):
            social.save(d, k, "k-" + k)
        h = social._oauth1(d, "POST", "https://api.twitter.com/2/tweets")
        self.assertTrue(h.startswith("OAuth ") and 'oauth_signature="' in h and 'oauth_token="k-x_access_token"' in h)
        # the platforms' limits are respected
        self.assertLessEqual(len(social.fit("word " * 100, 300)), 300)
        # Test button reports problems in plain words
        with mock.patch("app.social.requests.post", side_effect=social.requests.ConnectionError()):
            ok, msg = social.test(d, "bluesky")
        self.assertFalse(ok)
        self.assertIn("Couldn't reach", msg)
        # defaults for automatically published stories
        self.post("/admin/social", {"platform": "_defaults", "d|Weather|bluesky": "on"})
        self.assertEqual(st.get(d, "social_defaults")["Weather"], ["bluesky"])

    def test_22_wire_service(self):
        d = self.db()
        r = self.post("/admin/sources/new", {"type": "wire", "name": "Wire", "cfg_url": "https://wire.example/feed",
                                             "cfg_key_header": "x-api-key", "cfg_password": "wire-key",
                                             "cfg_credit": "The Associated Press", "cfg_scope": "world",
                                             "trust": "official", "category": "National & World", "interval_min": "30"})
        self.assertEqual(r.status_code, 302)
        wid = d.val("SELECT id FROM sources WHERE type='wire'")
        seen = {}

        def wire_get(url, headers=None, timeout=25):
            seen.update(headers or {})
            return FakeResp(RSS)
        with mock.patch.object(rss, "get", side_effect=wire_get):
            pipeline.process_source(d, d.one("SELECT * FROM sources WHERE id=?", (wid,)))
        self.assertEqual(seen.get("x-api-key"), "wire-key")
        item = d.val("SELECT id FROM items WHERE source_id=? AND status='new' ORDER BY id", (wid,))
        self.assertIn("Publish as written", self.c.get(f"/admin/queue/{wid}").text)
        with mock.patch("app.ai.call_claude") as never:
            self.post(f"/admin/items?one={item}&action=wire", {})
            never.assert_not_called()
        s = d.one("SELECT * FROM stories WHERE wire=1")
        self.assertEqual((s["status"], s["byline"], s["scope"]), ("published", "The Associated Press", "world"))
        self.assertIn("gift cards", s["body"])
        self.assertIn(s["headline"], self.c.get("/world").text)
        self.assertIn(s["headline"], self.c.get("/").text)  # homepage National & World box
        self.assertIn("The Associated Press", self.c.get("/story/" + s["slug"]).text)
        self.assertEqual(json.loads(s["social"]), {"_none": True})  # wire stories aren't shared

    def test_23_specials_from_partners(self):
        """The directory is gone; restaurants post specials as Community Partners."""
        from app import specials
        d = self.db()
        d.run("DELETE FROM rate")
        self.assertEqual(self.c.get("/directory").status_code, 404)
        self.assertEqual(self.c.get("/admin/directory").status_code, 404)
        self.assertNotIn("/directory", self.c.get("/").text)
        # only partners can post, from their manage page
        cafe = self.member_client("corner_cafe")
        t = dbm.now()
        oid = d.insert("orgs", member_id=self.mid("corner_cafe"), name="Corner Café", slug="corner-cafe",
                       category="Food & Specials", address="3 Main St", status="approved", created_at=t)
        today = specials.today(d)
        self.post(f"/partners/{oid}/manage", {"action": "special", "title": "Pot roast dinner", "price": "$11.99",
                                             "day": today, "description": "With a roll"}, client=cafe)
        page = self.c.get("/specials").text
        self.assertIn("Pot roast dinner", page)
        self.assertIn("Corner Café", page)
        self.assertIn("Pot roast dinner", self.c.get("/").text)                     # homepage box
        tomorrow = specials.days_ahead(d)[1][0]
        self.post(f"/partners/{oid}/manage", {"action": "special", "title": "Fish fry", "day": tomorrow}, client=cafe)
        self.assertNotIn("Fish fry", self.c.get("/specials").text)                  # shows on its day
        self.assertIn("Fish fry", cafe.get(f"/partners/{oid}/manage").text)         # listed as coming up
        self.post(f"/partners/{oid}/manage", {"action": "special", "title": "Old pie", "day": "2020-01-01"}, client=cafe)
        self.assertFalse(d.val("SELECT 1 FROM specials WHERE title='Old pie'"))     # only today and the next week
        other = self.member_client("not_a_partner")
        self.assertEqual(self.post(f"/partners/{oid}/manage", {"action": "special", "title": "Sneaky", "day": today},
                                   client=other).status_code, 403)
        # the partner can take one down, and so can the newsroom
        sp = d.one("SELECT * FROM specials WHERE title='Fish fry'")
        self.post(f"/partners/{oid}/manage", {"action": "delete_special", "id": sp["id"]}, client=cafe)
        self.assertEqual(d.val("SELECT status FROM specials WHERE id=?", (sp["id"],)), "removed")
        roast = d.one("SELECT * FROM specials WHERE title='Pot roast dinner'")
        self.assertIn("Pot roast dinner", self.c.get("/admin/community/partners").text)
        self.post("/admin/community/partners", {"id": oid, "action": "remove_special", "special_id": roast["id"]})
        self.assertNotIn("Pot roast dinner", self.c.get("/specials").text)
        # upgrading deletes the old directory's data and photos
        import sqlite3
        photo = dbm.UPLOADS / "oldbiz.jpg"
        photo.write_bytes(b"x")
        c = sqlite3.connect(":memory:")
        c.executescript("CREATE TABLE businesses(id INTEGER PRIMARY KEY, photo TEXT); INSERT INTO businesses VALUES(1,'oldbiz.jpg');"
                        "CREATE TABLE reviews(id INTEGER PRIMARY KEY, photos TEXT); CREATE TABLE claims(id INTEGER);"
                        "CREATE TABLE business_posts(id INTEGER, image TEXT); CREATE TABLE specials(id INTEGER, business_id INTEGER, image TEXT);")
        dbm._remove_directory(c)
        self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('businesses','reviews','claims','specials')").fetchone())
        self.assertFalse(photo.exists())

    def test_24_fact_check(self):
        d = self.db()
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        iid = d.insert("items", source_id=src, hash="fc1", title="Riverton parade route set", text="Parade at noon.",
                       fetched_at=dbm.now())
        sid = self.develop([iid], answers=["It has sixty officers"], factcheck=True)
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual(json.loads(s["factcheck"])[0]["text"], "sixty officers")
        self.assertIn("Fact-check", s["checklist"])
        page = self.c.get(f"/admin/story/{sid}").text
        self.assertIn('<mark class="fc">sixty officers</mark>', page)
        # the highlight is never saved into the article
        self.post(f"/admin/story/{sid}", {"headline": s["headline"], "action": "save",
                                          "body": '<p>There were <mark class="fc">sixty officers</mark>.</p>'})
        self.assertNotIn("<mark", d.val("SELECT body FROM stories WHERE id=?", (sid,)))
        # back to Develop to add more, then write again
        self.post(f"/admin/story/{sid}", {"headline": s["headline"], "action": "redevelop"})
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (sid,)), "developing")

    def test_25_migrations(self):
        from app import settings
        d = self.db()
        settings.put(d, "categories", settings.OLD_DEFAULT_CATEGORIES)
        settings.migrate(d)
        self.assertEqual(settings.get(d, "categories"), settings.DEFAULT_CATEGORIES)
        settings.put(d, "categories", ["Local News", "Sports"])
        settings.migrate(d)
        self.assertEqual(settings.get(d, "categories"), ["Local News", "Sports", "National & World"])
        self.assertGreaterEqual(d.val("SELECT COUNT(*) FROM badges"), 21)

    def test_26_review_fixes_stage2(self):
        from app import util
        d = self.db()
        d.run("DELETE FROM rate")
        # an anonymous guest tip isn't attached to the account created afterwards
        guest = self.app.test_client()
        ft = re.search(r'name="ft" value="([^"]+)"', guest.get("/tip").text).group(1)
        time.sleep(3.1)
        self.post("/tip", {"ft": ft, "text": "Something anonymous happened at the county building",
                           "anonymous": "on"}, client=guest)
        tid = d.val("SELECT id FROM tips ORDER BY id DESC")
        self.join({"username": "whistle", "email": "w@example.com", "password": "long-enough-1"}, client=guest)
        t = d.one("SELECT * FROM tips WHERE id=?", (tid,))
        self.assertEqual((t["member_id"], t["anonymous"], t["contact"]), (None, 1, ""))
        # going back to Develop and stopping never deletes a written story
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        iid = d.insert("items", source_id=src, hash="rv1", title="Riverton road work", text="Road work.",
                       fetched_at=dbm.now())
        sid = self.develop([iid])
        self.post(f"/admin/story/{sid}", {"headline": "Riverton road work", "action": "redevelop"})
        self.post(f"/admin/develop/{sid}", {"action": "abandon"})
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (sid,)), "draft")
        # tips: a used tip can't be put back; reviewers can't act on tips
        used = d.one("SELECT * FROM tips WHERE status='used' ORDER BY id")
        self.post(f"/admin/tips/{used['id']}", {"action": "restore"})
        self.assertEqual(d.val("SELECT status FROM tips WHERE id=?", (used["id"],)), "used")
        rev = self.app.test_client()
        self.post("/admin/login", {"email": "rev@example.com", "password": "reviewer-pass-1"}, client=rev)
        self.assertEqual(self.post(f"/admin/tips/{used['id']}", {"action": "dismiss"}, client=rev).status_code, 403)
        # staff public profiles can't be reset or banned from the members page
        staff = self.mid("pat_news")
        self.post(f"/admin/community/members/{staff}", {"action": "reset"})
        self.post(f"/admin/community/members/{staff}", {"action": "ban"})
        self.assertEqual(d.val("SELECT status FROM members WHERE id=?", (staff,)), "active")
        # a suspended member can't edit their comments
        a = self.app.test_client()
        self.post("/login", {"email": "alex", "password": "member-pass-1"}, client=a)
        c = d.one("SELECT * FROM comments WHERE member_id=? AND status='visible'", (self.mid("alex"),))
        d.run("UPDATE comments SET created_at=? WHERE id=?", (dbm.now(), c["id"]))
        self.post(f"/admin/community/members/{self.mid('alex')}", {"action": "suspend", "days": "2"})
        self.post(f"/comment/{c['id']}/edit", {"body": "spam spam http://evil.example"}, client=a)
        self.assertNotIn("evil", d.val("SELECT body FROM comments WHERE id=?", (c["id"],)))
        # unpublishing takes back the points, republishing doesn't notify twice
        s = d.one("SELECT * FROM stories WHERE status='published' AND credit='byline' AND member_id=? ORDER BY id",
                  (self.mid("jamie"),))
        before = d.val("SELECT points FROM members WHERE username='jamie'")
        self.post(f"/admin/story/{s['id']}", {"headline": s["headline"], "body": s["body"], "action": "unpublish"})
        after = d.val("SELECT points FROM members WHERE username='jamie'")
        self.assertLess(after, before)
        self.assertEqual(d.val("SELECT status FROM submissions WHERE id=?", (s["submission_id"],)), "developing")
        self.approve(s["id"])
        self.assertEqual(d.val("SELECT points FROM members WHERE username='jamie'"), before)
        self.assertEqual(d.val("SELECT COUNT(*) FROM notices WHERE member_id=? AND kind='published' AND link LIKE ?",
                               (self.mid("jamie"), f"%{s['slug']}")), 1)
        # only wire items can be published word for word
        other = d.insert("items", source_id=src, hash="rv2", title="Other paper story", text="Their words.",
                         fetched_at=dbm.now())
        self.assertIsNone(pipeline.wire_story(d, other))
        self.assertEqual(d.val("SELECT status FROM items WHERE id=?", (other,)), "new")
        # fact-check highlights never touch link addresses
        html = '<p>See <a href="https://riverton-council.example/x">riverton-council minutes</a> today.</p>'
        marked = util.mark_phrases(html, ["riverton-council"])
        self.assertIn('href="https://riverton-council.example/x"', marked)
        self.assertEqual(util.clean_html(marked), util.clean_html(html))

    def test_28_signup_bot_checks(self):
        from app.disposable_domains import is_disposable
        import app.views.members as mv
        d = self.db()
        d.run("DELETE FROM rate")
        c = self.app.test_client()
        # no form token (a bot posting directly) or too fast: refused
        r = self.post("/join", {"username": "botty", "email": "b@example.com", "password": "long-enough-1"}, client=c)
        self.assertIn("try that again", r.text)
        with mock.patch.object(mv, "JOIN_MIN_SECONDS", 60):
            r = self.join({"username": "botty", "email": "b@example.com", "password": "long-enough-1"}, client=c)
        self.assertIn("try that again", r.text)
        # throwaway addresses refused, including subdomains; more can be added in Settings
        r = self.join({"username": "botty", "email": "x@mailinator.com", "password": "long-enough-1"}, client=c)
        self.assertIn("temporary", r.text)
        self.assertTrue(is_disposable("a@inbox.guerrillamail.com"))
        self.assertFalse(is_disposable("a@gmail.com"))
        from app import settings as st
        st.put(d, "blocked_email_domains", ["spammy.example"])
        r = self.join({"username": "botty", "email": "x@spammy.example", "password": "long-enough-1"}, client=c)
        self.assertIn("temporary", r.text)
        self.assertIsNone(self.mid("botty"))
        r = self.join({"username": "realperson", "email": "real@example.com", "password": "long-enough-1"}, client=c)
        self.assertEqual(r.status_code, 302)

    def test_29_video_embeds(self):
        from app.util import video_embed as ve
        yt = "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
        for u in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "https://youtu.be/dQw4w9WgXcQ",
                  "https://m.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/live/dQw4w9WgXcQ"):
            self.assertEqual(ve(u)["src"], yt, u)
        self.assertTrue(ve("https://www.youtube.com/shorts/dQw4w9WgXcQ")["tall"])
        fb = ve("https://www.facebook.com/HillsdaleCollege/videos/123456789/")
        self.assertEqual(fb["provider"], "facebook")
        self.assertIn("plugins/video.php", fb["src"])
        self.assertTrue(ve("https://www.facebook.com/reel/987654321")["tall"])
        self.assertTrue(ve("https://fb.watch/abcDEF/"))
        for bad in ("https://www.youtube.com/channel/UCnNcD8Lfk5WuxbcsRIkj4BQ", "https://evil.example/watch?v=dQw4w9WgXcQ",
                    "javascript:alert(1)", "https://www.facebook.com/HillsdaleCollege/posts/1", "https://youtu.be/<x>"):
            self.assertIsNone(ve(bad), bad)
        d = self.db()
        # the source is a video: the story embeds it automatically
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        iid = d.insert("items", source_id=src, hash="vid1", title="Free-throw-a-thon fundraiser",
                       url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", text="Video.", fetched_at=dbm.now())
        with claude_patch():
            r = self.post(f"/admin/items?one={iid}&action=quick", {})
        sid = int(r.headers["Location"].rsplit("/", 1)[1])
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertEqual(s["video"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIn("Added automatically", self.c.get(f"/admin/story/{sid}").text)
        self.approve(sid)
        page = self.c.get("/story/" + d.val("SELECT slug FROM stories WHERE id=?", (sid,)))
        self.assertIn('src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"', page.text)
        self.assertIn("frame-src https://www.youtube-nocookie.com https://www.facebook.com",
                      page.headers["Content-Security-Policy"])
        # the editor can set, change or clear it; bad links are refused
        base = {"headline": s["headline"], "body": s["body"], "category": s["category"], "action": "save"}
        self.post(f"/admin/story/{sid}", {**base, "video": "https://evil.example/v.mp4"})
        self.assertEqual(d.val("SELECT video FROM stories WHERE id=?", (sid,)), s["video"])
        self.post(f"/admin/story/{sid}", {**base, "video": "https://www.facebook.com/reel/987654321"})
        self.assertIn("plugins/video.php", self.c.get("/story/" + d.val("SELECT slug FROM stories WHERE id=?", (sid,))).text)
        self.post(f"/admin/story/{sid}", {**base, "video": ""})
        self.assertIsNone(d.val("SELECT video FROM stories WHERE id=?", (sid,)))
        # members can attach a video to what they send
        m = self.member_client("vidfan")
        body = "<p>" + "The band played the halftime show on Friday night at the stadium. " * 3 + "</p>"
        r = self.post("/submit/new/article", {"action": "submit", "headline": "Band halftime show", "body": body,
                                              "video": "https://example.com/not-a-video"}, client=m)
        self.assertIn("one we can show", r.text)
        self.post("/submit/new/article", {"action": "submit", "headline": "Band halftime show", "body": body,
                                          "video": "https://youtu.be/dQw4w9WgXcQ"}, client=m)
        sub = d.one("SELECT * FROM submissions WHERE member_id=?", (self.mid("vidfan"),))
        self.post(f"/admin/members/submissions/{sub['id']}", {"action": "publish"})
        st = d.one("SELECT * FROM stories WHERE submission_id=?", (sub["id"],))
        self.assertEqual(st["video"], "https://youtu.be/dQw4w9WgXcQ")

    def test_30_ai_costs_down(self):
        from app import ai, settings as st
        d = self.db()
        st.put(d, "ai_research", True)
        st.put(d, "ai_research_max", 5)
        d.run("DELETE FROM settings WHERE key='_migrated_research_max'")
        st.migrate(d)
        self.assertEqual(st.get(d, "ai_research_max"), 2)   # old default lowered once
        st.put(d, "ai_research_max", 5)
        st.migrate(d)
        self.assertEqual(st.get(d, "ai_research_max"), 5)   # a choice made afterwards is kept
        st.put(d, "ai_research_max", 2)

        class R:
            def __init__(self, code, js=None, text=""):
                self.status_code, self._js, self.text = code, js, text

            def json(self):
                return self._js
        ok = lambda txt: R(200, {"content": [{"type": "text", "text": txt}], "stop_reason": "end_turn",  # noqa: E731
                                 "usage": {"input_tokens": 1000, "output_tokens": 100}})
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        iid = d.insert("items", source_id=src, hash="cost1", title="Library book sale moved", text="Moved.",
                       fetched_at=dbm.now())
        sent = []

        def fake_post(url, **k):
            body = json.loads(json.dumps(k["json"]))
            sent.append(body)
            if "develop a story" in body["system"]:
                return ok('{"known": [], "questions": ["When?"], "category": "Local News"}')
            return ok('{"headline": "Library book sale moved", "body": "<p>Moved.</p>", "summary": "s"}')
        with mock.patch("app.ai.requests.post", side_effect=fake_post):
            r = self.post(f"/admin/items?one={iid}&action=quick", {})
        sid = int(r.headers["Location"].rsplit("/", 1)[1])
        research, write = sent[0], sent[-1]
        self.assertEqual(research["model"], "claude-haiku-4-5-20251001")   # research on the cheap model
        self.assertEqual(research["tools"][0]["max_uses"], 2)
        self.assertEqual(write["model"], st.get(d, "ai_model"))            # the article on the main model
        # a source with plenty in it is written in one call, with no research, and long text is trimmed
        long_item = d.insert("items", source_id=src, hash="cost2", title="Council budget", text="word " * 4000,
                             fetched_at=dbm.now())
        sent.clear()
        with mock.patch("app.ai.requests.post", side_effect=fake_post):
            self.post(f"/admin/items?one={long_item}&action=quick", {})
        self.assertEqual(len(sent), 1)
        self.assertNotIn("tools", sent[0])
        self.assertLess(len(sent[0]["messages"][0]["content"]), 9000)
        rows = d.q("SELECT * FROM ai_usage WHERE story_id=?", (sid,))
        self.assertEqual({r_["purpose"] for r_ in rows}, {"develop", "write"})
        self.assertGreater(ai.story_cost(d, sid), 0)
        self.assertIn("AI cost for this story", self.c.get(f"/admin/story/{sid}").text)
        self.assertIn("This month by step", self.c.get("/admin/settings/backups").text)
        # if the cheaper model isn't available on the account, it falls back to the main one
        replies = [R(404, text='{"error":{"message":"model not found"}}'), ok("{}")]
        models = []
        with mock.patch("app.ai.requests.post", side_effect=lambda url, **k: (models.append(k["json"]["model"]),
                                                                              replies.pop(0))[1]):
            ai.call_claude(d, "s", "u", model="claude-haiku-4-5-20251001")
        self.assertEqual(models, ["claude-haiku-4-5-20251001", st.get(d, "ai_model")])

    def test_31_draft_first_fix_after(self):
        from app import settings as st
        d = self.db()
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        iid = d.insert("items", source_id=src, hash="fix1", title="Free-throw-a-thon fundraiser", text="Video.",
                       fetched_at=dbm.now())
        with claude_patch():
            r = self.post(f"/admin/items?one={iid}&action=quick", {})
        sid = int(r.headers["Location"].rsplit("/", 1)[1])
        page = self.c.get(f"/admin/story/{sid}").text
        self.assertIn("Missing from this story", page)
        self.assertIn("What time does it start?", page)
        before = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        with claude_patch() as call:
            self.post(f"/admin/story/{sid}", {"headline": before["headline"], "body": before["body"],
                                              "category": before["category"], "action": "revise",
                                              "note": "It starts at 7 p.m. Friday in the fieldhouse."})
            self.assertIn("EDITOR'S NOTE", call.call_args[0][2])
        after = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertIn("7 p.m. Friday in the fieldhouse", after["body"])
        dev = json.loads(after["dev"])
        self.assertEqual(dev["gaps"], [])
        self.assertIn("7 p.m. Friday", dev["details"])  # remembered if it's ever rewritten
        self.post(f"/admin/story/{sid}", {"headline": after["headline"], "body": after["body"],
                                          "category": after["category"], "action": "undo"})
        self.assertEqual(d.val("SELECT body FROM stories WHERE id=?", (sid,)), before["body"])
        # the improved style replaces the old default, but never an owner's own wording
        from app import writing_defaults as WD
        st.put(d, "write_style", WD.OLD_STYLE)
        st.migrate(d)
        self.assertEqual(st.get(d, "write_style"), WD.STYLE)
        st.put(d, "write_style", "My own style")
        st.migrate(d)
        self.assertEqual(st.get(d, "write_style"), "My own style")
        st.put(d, "write_style", WD.STYLE)
        from app import writing
        system = writing.write_system(d, "general")
        self.assertIn("Never write that something is unknown", system)
        self.assertIn('"gaps"', system)

    def test_32_footer_pages(self):
        from app import settings as st, util, page_defaults as PD
        d = self.db()
        about = self.c.get("/page/about").text
        self.assertIn("About Riverton Daily", about)
        self.assertIn("Riverton Daily is Polk County", about)      # names filled in
        self.assertNotIn("{site}", about)
        self.assertNotIn("Who we are", about)                       # unwritten placeholder section hidden
        self.assertIn("Be part of Riverton Daily", about)
        st.put(d, "page_about", PD.ABOUT.replace("[A few sentences about you: who you are, your ties to {town}, "
                                                 "and why you started {site}.]", "I grew up on Main Street."))
        self.assertIn("I grew up on Main Street.", self.c.get("/page/about").text)
        self.assertIn("Who we are", self.c.get("/page/about").text)
        self.assertIn("never", self.c.get("/page/ai").text)
        self.assertNotIn("Be part of", self.c.get("/page/privacy").text)
        # untouched Stage 1 texts are upgraded; edited ones are kept
        st.put(d, "page_contact", PD.OLD["page_contact"])
        st.put(d, "page_corrections", "Our own policy.")
        st.migrate(d)
        self.assertEqual(st.get(d, "page_contact"), PD.CONTACT)
        self.assertEqual(st.get(d, "page_corrections"), "Our own policy.")
        self.assertEqual(util.fill_page(d, "## Heading\n\n[fill me]"), "")



    def test_35_sports_scores_and_schedules(self):
        from app import sports, util, settings as st
        d = self.db()
        d.run("DELETE FROM rate")
        today = datetime.now(util.tz(d)).date()
        day = lambda n: (today + timedelta(days=n)).isoformat()
        schedule = {"games": [
            {"date": day(-3), "time": "19:00", "opponent": "Jonesville", "home": "home", "sport": "Football",
             "gender": "Boys", "level": "Varsity", "our_score": 28, "their_score": 14, "note": ""},
            {"date": day(0), "time": "19:00", "opponent": "@ Quincy", "home": "away", "sport": "Football",
             "gender": "Boys", "level": "Varsity", "our_score": None, "their_score": None, "note": "Homecoming"},
            {"date": day(2), "time": "", "opponent": "Reading", "home": "home", "sport": "Volleyball",
             "gender": "Girls", "level": "JV", "our_score": None, "their_score": None},
            {"date": "", "opponent": "No date"}]}
        seen = []
        fake = lambda db, system, user, **k: seen.append((system, user, k)) or ("Here you go:\n" + json.dumps(schedule))
        # 1) the newsroom pastes a messy schedule; the AI lays it out, nothing is saved until it's checked
        with mock.patch("app.ai.call_claude", side_effect=fake):
            r = self.post("/admin/sports/import", {"action": "read", "new_school": "Hillsdale High School",
                                                   "new_town": "Hillsdale", "text": "FB schedule... vs Jonesville W 28-14"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("3 games found", r.text)
        self.assertEqual(seen[0][2]["model"], st.get(d, "ai_model_research"))       # the cheap model
        self.assertEqual(d.val("SELECT COUNT(*) FROM games"), 0)
        sid = d.val("SELECT id FROM sports_schools WHERE name='Hillsdale High School'")
        form = {"action": "save", "school_id": sid, "team_id": "", "keep": ["0", "1", "2"],
                "date": [day(-3), day(0), day(2)], "time": ["19:00", "19:00", ""],
                "opponent": ["Jonesville", "Quincy", "Reading"], "home": ["home", "away", "home"],
                "sport": ["Football", "Football", "Volleyball"], "gender": ["Boys", "Boys", "Girls"],
                "level": ["Varsity", "Varsity", "JV"], "our_score": ["28", "", ""], "their_score": ["14", "", ""],
                "location": ["", "", ""], "note": ["", "Homecoming", ""]}
        self.post("/admin/sports/import", form)
        self.assertEqual(d.val("SELECT COUNT(*) FROM games"), 3)
        self.assertEqual(d.val("SELECT COUNT(*) FROM sports_teams"), 2)            # teams made automatically
        self.post("/admin/sports/import", form)                                     # pasting again doesn't double up
        self.assertEqual(d.val("SELECT COUNT(*) FROM games"), 3)
        fb = d.one("SELECT * FROM sports_teams WHERE sport='Football'")
        self.assertEqual(sports.record(d, fb["id"]), "1-0")
        # 2) the public pages
        page = self.c.get("/sports").text
        self.assertIn("Hillsdale High School", page)
        self.assertIn("Quincy", page)
        self.assertIn("Jonesville", page)
        self.assertIn("Boys Football", self.c.get("/sports/" + d.val("SELECT slug FROM sports_schools WHERE id=?", (sid,))).text)
        self.assertIn("1-0", self.c.get("/sports/team/" + fb["slug"]).text)
        self.assertIn("Scores", self.c.get("/").text)
        quincy = d.one("SELECT * FROM games WHERE opponent='Quincy'")
        self.assertIn("<span>Quincy</span>", self.c.get("/").text)                 # homepage strip shows today's game
        # 3) any member posts the score, instantly, with credit and points
        self.assertIn("Know the score?", self.app.test_client().get(f"/sports/game/{quincy['id']}").text)
        fan = self.member_client("hornet_fan")
        pts0 = d.val("SELECT points FROM members WHERE username='hornet_fan'")
        self.post(f"/sports/game/{quincy['id']}", {"action": "report", "our_score": "21", "their_score": "20",
                                                   "status": "final", "detail": "OT"}, client=fan)
        g1 = d.one("SELECT * FROM games WHERE id=?", (quincy["id"],))
        self.assertEqual((g1["our_score"], g1["their_score"], g1["status"]), (21, 20, "final"))
        self.assertEqual(d.val("SELECT points FROM members WHERE username='hornet_fan'"), pts0 + st.get(d, "pts_score"))
        page = self.c.get(f"/sports/game/{quincy['id']}").text
        self.assertIn("@hornet_fan", page)
        self.assertIn("Final · OT", page)
        # someone else confirms; the reporter can't confirm their own
        mom = self.member_client("volley_mom")
        self.post(f"/sports/game/{quincy['id']}", {"action": "confirm"}, client=mom)
        self.post(f"/sports/game/{quincy['id']}", {"action": "confirm"}, client=fan)
        self.assertEqual(d.val("SELECT COUNT(*) FROM game_confirms WHERE game_id=?", (quincy["id"],)), 1)
        # a wrong correction... and the newsroom undoes it (points go back too)
        self.post(f"/sports/game/{quincy['id']}", {"action": "report", "our_score": "0", "their_score": "99",
                                                   "status": "final"}, client=mom)
        self.assertEqual(d.val("SELECT their_score FROM games WHERE id=?", (quincy["id"],)), 99)
        self.assertEqual(d.val("SELECT COUNT(*) FROM game_confirms WHERE game_id=?", (quincy["id"],)), 0)
        self.assertIn("volley_mom", self.c.get("/admin/sports").text)
        self.post("/admin/sports", {"action": "revert", "id": quincy["id"]})
        g2 = d.one("SELECT * FROM games WHERE id=?", (quincy["id"],))
        self.assertEqual((g2["our_score"], g2["their_score"]), (21, 20))
        self.assertEqual(d.val("SELECT COUNT(*) FROM points_log WHERE member_id=? AND reason='score'",
                               (self.mid("volley_mom"),)), 0)
        # locked games can't be changed by members
        self.post("/admin/sports", {"action": "lock", "id": quincy["id"]})
        r = self.post(f"/sports/game/{quincy['id']}", {"action": "report", "our_score": "1", "their_score": "2",
                                                       "status": "final"}, client=mom)
        self.assertIn("locked", r.text)
        self.assertEqual(d.val("SELECT our_score FROM games WHERE id=?", (quincy["id"],)), 21)
        # 4) members add a missing game, and paste schedules too
        self.post(f"/sports/team/{fb['slug']}", {"date": day(9), "time": "19:00", "opponent": "Litchfield",
                                                 "home": "home"}, client=fan)
        self.assertEqual(d.val("SELECT source FROM games WHERE opponent='Litchfield'"), "member-one")
        t = dbm.now()
        d.insert("stories", kind="event", status="published", headline="Pumpkin festival", slug="pumpkin-festival",
                 event_start=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds"),
                 published_at=t, created_at=t, updated_at=t)
        ev = self.c.get("/events").text                                             # games show up with events
        self.assertIn("Boys Football vs. Litchfield", ev)
        self.assertIn("Pumpkin festival", ev)
        self.assertLess(ev.index("Pumpkin festival"), ev.index("Litchfield"))       # soonest first
        st.put(d, "events_show_games", False)
        self.assertNotIn("Litchfield", self.c.get("/events").text)
        st.put(d, "events_show_games", True)
        with mock.patch("app.ai.call_claude", side_effect=fake):
            r = self.post("/sports/add", {"action": "read", "team_id": fb["id"], "text": "Oct 9 vs Litchfield 7pm"},
                          client=fan)
        self.assertIn("games found for", r.text)
        self.assertIn("Boys Football", r.text)
        self.assertNotIn('name="sport"><option', r.text)                          # sport comes from the team
        anon = self.app.test_client()
        self.assertEqual(anon.get("/sports/add").status_code, 302)                  # members only
        # 5) the admin game editor, and the scoreboard when both schools are on the site
        self.post(f"/admin/sports/game/{quincy['id']}", {"action": "save", "opponent": "Quincy", "home": "away",
                                                         "date": day(0), "time": "18:30", "status": "final",
                                                         "our_score": "21", "their_score": "20", "locked": "on"})
        self.assertIn("6:30 PM", self.c.get(f"/sports/game/{quincy['id']}").text)
        qid = self.db().insert("sports_schools", name="Quincy", slug="quincy", created_at=dbm.now())
        qt = sports.find_or_make_team(d, qid, "Football", "Boys", "Varsity")
        starts = d.val("SELECT starts_at FROM games WHERE id=?", (quincy["id"],))
        d.insert("games", team_id=qt, opponent="Hillsdale High School", home="home", starts_at=starts, status="scheduled", updated_at=dbm.now(),
                 created_at=dbm.now())
        board = self.c.get("/sports").text
        today_part = board.split('id="today-h"')[1].split("</section>")[0]
        self.assertEqual(today_part.count('class="gm '), 1)                        # the same game isn't shown twice
        st.put(d, "home_show_scores", False)
        self.assertNotIn("scorestrip", self.c.get("/").text)
        st.put(d, "home_show_scores", True)

    def test_36_logged_out_pages_send_you_to_log_in(self):
        anon = self.app.test_client()
        for url in ("/directory/add", "/directory/broad-street-diner/manage", "/sports/add", "/me"):
            r = anon.get(url)
            self.assertIn(r.status_code, (302, 404), url)
            if r.status_code == 302:
                self.assertIn("/login", r.headers["Location"], url)

    def test_37_source_folders(self):
        from app import folders
        d = self.db()
        t = dbm.now()
        ids = [d.insert("sources", name=f"Township {n}", type="rss", config='{"url": "https://t%s.example/feed"}' % n,
                        created_at=t) for n in range(3)]
        # everything starts Unsorted
        page = self.c.get("/admin/sources").text
        self.assertIn("Unsorted", page)
        # make folders, one inside another
        self.post("/admin/sources/organize", {"action": "new_folder", "name": "Government"})
        gov = d.val("SELECT id FROM source_folders WHERE name='Government'")
        self.post("/admin/sources/organize", {"action": "new_folder", "name": "Townships", "parent_id": gov})
        twp = d.val("SELECT id FROM source_folders WHERE name='Townships'")
        self.assertEqual([f["label"] for f in folders.flat(d)], ["Government", "Government / Townships"])
        # move several at once
        self.post("/admin/sources/organize", {"action": "move", "to": twp, "sid": [str(i) for i in ids[:2]]})
        self.assertEqual(d.val("SELECT COUNT(*) FROM sources WHERE folder_id=?", (twp,)), 2)
        self.post("/admin/sources/organize", {"action": "move", "to": "", "sid": [str(ids[0])]})   # no folder picked
        self.assertEqual(d.val("SELECT folder_id FROM sources WHERE id=?", (ids[0],)), twp)
        # the folder page gathers items from every source inside, subfolders included
        d.insert("items", source_id=ids[0], title="Township board meets Monday", url="https://t0.example/1",
                 hash="t0-1", status="new", fetched_at=t)
        d.insert("items", source_id=ids[2], title="Unrelated item", url="https://t2.example/1", hash="t2-1",
                 status="new", fetched_at=t)
        page = self.c.get(f"/admin/queue/folder/{gov}").text
        self.assertIn("Township board meets Monday", page)
        self.assertNotIn("Unrelated item", page)
        self.assertIn("Townships", page)                                            # subfolder shortcut
        self.assertIn("Unrelated item", self.c.get("/admin/queue/folder/0").text)  # Unsorted
        # Sources are a resource: one link in the sidebar, lit up while you're in any source or folder
        page = self.c.get(f"/admin/queue/{ids[0]}").text
        side = page.split('id="sidebar"')[1].split("</nav>")[0]
        self.assertNotIn("Township", side)
        self.assertRegex(side, r'class="navl on"[^>]*href="/admin/sources"')
        # search, pause several, a folder can't go inside itself, removing a folder loses nothing
        self.assertIn("Township 1", self.c.get("/admin/sources?q=t1.example").text)
        self.post("/admin/sources/organize", {"action": "pause", "sid": [str(i) for i in ids[:2]]})
        self.assertEqual(d.val("SELECT COUNT(*) FROM sources WHERE enabled=0 AND id IN (?,?)", tuple(ids[:2])), 2)
        self.post("/admin/sources/organize", {"action": "move_folder", "id": gov, "parent_id": twp})
        self.assertIsNone(d.val("SELECT parent_id FROM source_folders WHERE id=?", (gov,)))
        self.post("/admin/sources/organize", {"action": "delete_folder", "id": twp})
        self.assertEqual(d.val("SELECT COUNT(*) FROM sources WHERE folder_id=?", (gov,)), 2)
        # new sources can be filed as they're added
        self.assertIn(f'value="{gov}" selected', self.c.get(f"/admin/sources/new?folder={gov}").text)

    def test_38_content_section_and_finder(self):
        d = self.db()
        t = dbm.now()
        side = self.c.get("/admin/").text.split('id="sidebar"')[1].split("</nav>")[0]
        content_part = side.split(">Content<")[1].split(">Community<")[0]
        for label in ("All content", "New from sources", "Member submissions", "Reader tips", "Manual add"):
            self.assertIn(label, content_part)
        self.assertIn(">Sources<", side.split(">Manage<")[1])
        # the finder: search, filter by where it came from, section, status and dates
        mid = self.mid("jamie")
        d.insert("stories", headline="Zebra crossing painted downtown", slug="zebra-crossing", status="published",
                 category="Local News", member_id=mid, published_at=t, created_at=t, updated_at=t)
        d.insert("stories", headline="Zebra mussels found in Baw Beese Lake", slug="zebra-mussels", status="draft",
                 category="Outdoors", created_at=t, updated_at=t)
        page = self.c.get("/admin/content?q=Zebra").text
        self.assertIn("Zebra crossing", page)
        self.assertIn("Zebra mussels", page)
        self.assertIn("@jamie", page)                                               # where it came from
        page = self.c.get("/admin/content?q=Zebra&origin=member").text
        self.assertIn("Zebra crossing", page)
        self.assertNotIn("Zebra mussels", page)
        page = self.c.get("/admin/content?q=Zebra&status=draft").text
        self.assertNotIn("Zebra crossing", page)
        self.assertIn("Zebra mussels", page)
        self.assertIn("Published (1)", page)                                        # counts follow the filters
        self.assertNotIn("Zebra", self.c.get("/admin/content?q=Zebra&cat=Sports").text.split("<tbody>")[1])
        self.assertNotIn("Zebra crossing", self.c.get("/admin/content?q=Zebra&to=2020-01-01").text)
        self.assertIn("Zebra crossing", self.c.get("/admin/content?author=jamie&q=Zebra").text)
        self.assertEqual(self.c.get("/admin/content?page=2&sort=comments&origin=bogus").status_code, 200)

    def test_39_game_night_videos_team_info_subcategories(self):
        from app import sports, settings as st
        d = self.db()
        d.run("DELETE FROM rate")
        t = dbm.now()
        sid = sports.add_school(d, "Reading High School", "Reading", "Rangers", "High school", "Reading")
        tid = sports.find_or_make_team(d, sid, "Basketball", "Girls", "Varsity")
        start = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
        gid = d.insert("games", team_id=tid, opponent="Camden", home="home", starts_at=start, status="scheduled",
                       created_at=t, updated_at=t)
        nxt = d.insert("games", team_id=tid, opponent="Pittsford", home="away", starts_at=later, status="scheduled",
                       created_at=t, updated_at=t)
        self.assertEqual(sports.live_state(d.one("SELECT * FROM games WHERE id=?", (gid,))), "live")
        self.assertEqual(sports.live_state(d.one("SELECT * FROM games WHERE id=?", (nxt,))), "upcoming")
        # Live mode: the page turns live and fans post updates, with the score
        page = self.app.test_client().get(f"/sports/game/{gid}").text
        self.assertIn("● LIVE", page)
        self.assertIn("data-live-feed", page)
        self.assertIn("Stream it!", page)                                           # the stream call-out
        fan = self.member_client("ranger_mom")
        work0 = d.val("SELECT work_points FROM members WHERE username='ranger_mom'")
        self.post(f"/sports/game/{gid}", {"action": "update", "body": "Tip-off! Rangers win the jump.",
                                         "our_score": "", "their_score": ""}, client=fan)
        d.run("DELETE FROM rate WHERE bucket LIKE 'live-fast:%'")
        self.post(f"/sports/game/{gid}", {"action": "update", "body": "Three from the corner!",
                                         "our_score": "3", "their_score": "0"}, client=fan)
        g1 = d.one("SELECT * FROM games WHERE id=?", (gid,))
        self.assertEqual((g1["our_score"], g1["their_score"], g1["status"]), (3, 0, "live"))
        self.assertEqual(d.val("SELECT work_points FROM members WHERE username='ranger_mom'"),
                         work0 + 2 * st.get(d, "pts_live"))
        self.post(f"/sports/game/{gid}", {"action": "update", "body": "too fast"}, client=fan)   # 15-second pause
        self.assertFalse(d.val("SELECT 1 FROM game_updates WHERE body='too fast'"))
        first = d.val("SELECT MIN(id) FROM game_updates WHERE game_id=?", (gid,))
        feed = self.app.test_client().get(f"/sports/game/{gid}/feed.json?after={first}").get_json()
        self.assertEqual(feed["live"], "live")
        self.assertEqual([u["body"] for u in feed["updates"]], ["Three from the corner!"])
        self.assertEqual((feed["ours"], feed["theirs"]), (3, 0))
        self.assertIn("Tip-off!", self.app.test_client().get(f"/sports/game/{gid}").text)
        # the newsroom can hide an update (and its point goes back)
        uid_ = d.val("SELECT id FROM game_updates WHERE body LIKE 'Tip-off%'")
        self.post(f"/admin/sports/game/{gid}", {"action": "hide_update", "item": uid_})
        self.assertNotIn("Tip-off!", self.app.test_client().get(f"/sports/game/{gid}").text)
        self.assertFalse(d.val("SELECT 1 FROM points_log WHERE reason='live' AND ref=?", (f"game:{gid}:u{uid_}",)))
        # upcoming games aren't live yet
        self.post(f"/sports/game/{nxt}", {"action": "update", "body": "Early!"}, client=fan)
        self.assertFalse(d.val("SELECT 1 FROM game_updates WHERE game_id=?", (nxt,)))
        # a live stream: big points for the first one, embedded on the page
        r = self.post(f"/sports/game/{gid}", {"action": "video", "url": "https://example.com/x", "kind": "stream"},
                      client=fan, follow_redirects=True)
        self.assertIn("Paste a YouTube or Facebook video link", r.text)
        before = d.val("SELECT points FROM members WHERE username='ranger_mom'")
        self.post(f"/sports/game/{gid}", {"action": "video", "url": "https://www.youtube.com/live/abcdefghijk",
                                         "kind": "stream"}, client=fan)
        self.assertEqual(d.val("SELECT points FROM members WHERE username='ranger_mom'"),
                         before + st.get(d, "pts_stream"))
        page = self.app.test_client().get(f"/sports/game/{gid}").text
        self.assertIn("youtube-nocookie.com/embed/abcdefghijk", page)
        self.assertNotIn("Stream it!", page)                                        # already streaming
        self.assertIn("▶ Live stream", self.app.test_client().get("/sports").text)  # marked on the scoreboard
        dad = self.member_client("ranger_dad")
        before = d.val("SELECT points FROM members WHERE username='ranger_dad'")
        self.post(f"/sports/game/{gid}", {"action": "video", "kind": "stream",
                                         "url": "https://www.facebook.com/readingrangers/videos/1234567890"}, client=dad)
        self.assertEqual(d.val("SELECT points FROM members WHERE username='ranger_dad'"),
                         before + st.get(d, "pts_video"))                          # only the first stream is big
        vid = d.val("SELECT id FROM game_videos WHERE url LIKE '%youtube%'")
        self.post(f"/admin/sports/game/{gid}", {"action": "hide_video", "item": vid})
        self.assertFalse(d.val("SELECT 1 FROM points_log WHERE reason='stream'"))
        # a final score ends Live mode; the feed stays as the record
        self.post(f"/sports/game/{gid}", {"action": "report", "our_score": "48", "their_score": "41",
                                         "status": "final"}, client=fan)
        page = self.app.test_client().get(f"/sports/game/{gid}").text
        self.assertNotIn("● LIVE", page)
        self.assertIn("Three from the corner!", page)
        self.assertIn("Game feed", page)
        # the team's "Good to know" box: any member can edit, unless the newsroom locks it
        slug = d.val("SELECT slug FROM sports_teams WHERE id=?", (tid,))
        self.post(f"/sports/team/{slug}", {"action": "info", "info": "Home games in the Reading HS gym.\nAdults $5."},
                  client=fan)
        page = self.app.test_client().get(f"/sports/team/{slug}").text
        self.assertIn("Home games in the Reading HS gym.", page)
        self.assertIn("by @ranger_mom", page)
        self.assertIn("Pittsford", page.split("Up next")[1].split("Good to know")[0])
        self.post("/admin/sports?tab=schools", {"action": "team_info", "id": tid, "info": "Checked by newsroom",
                                                "info_locked": "on"})
        r = self.post(f"/sports/team/{slug}", {"action": "info", "info": "Vandalized"}, client=fan)
        self.assertIn("The newsroom looks after this box", r.text)
        self.assertEqual(d.val("SELECT info FROM sports_teams WHERE id=?", (tid,)), "Checked by newsroom")
        # subcategories: the Sports page shows everything, each subcategory has its own page
        self.post("/admin/sports?tab=subcats", {"action": "save_subcats",
                                                "subcats": ["Middle School", "JV/Varsity", "College"],
                                                "subcats_new": "Youth/Rec"})
        self.assertEqual(st.subcategories(d, "Sports"), ["Middle School", "JV/Varsity", "College", "Youth/Rec"])
        s1 = d.insert("stories", headline="Chargers win homecoming", slug="chargers-homecoming", status="published",
                      category="Sports", subcategory="College", published_at=t, created_at=t, updated_at=t)
        d.insert("stories", headline="Eighth graders take title", slug="eighth-grade-title", status="published",
                 category="Sports", subcategory="Middle School", published_at=t, created_at=t, updated_at=t)
        allpage = self.app.test_client().get("/category/sports").text
        self.assertIn("Chargers win homecoming", allpage)
        self.assertIn("Eighth graders take title", allpage)
        self.assertIn('href="/category/sports/college"', allpage)
        college = self.app.test_client().get("/category/sports/college").text
        self.assertIn("Chargers win homecoming", college)
        self.assertNotIn("Eighth graders", college)
        self.assertEqual(self.app.test_client().get("/category/sports/curling").status_code, 404)
        self.assertIn('href="/category/sports/college">College</a>', self.app.test_client().get(
            "/story/chargers-homecoming").text)
        # the story editor sets it; a subcategory from another section isn't allowed
        s2 = d.insert("stories", headline="JV draft", slug=None, status="draft", category="Sports", created_at=t,
                      updated_at=t)
        self.assertIn('name="subcategory"', self.c.get(f"/admin/story/{s2}").text)
        self.post(f"/admin/story/{s2}", {"headline": "JV draft", "body": "<p>x</p>", "category": "Sports",
                                        "subcategory": "JV/Varsity", "action": "save"})
        self.assertEqual(d.val("SELECT subcategory FROM stories WHERE id=?", (s2,)), "JV/Varsity")
        self.post(f"/admin/story/{s2}", {"headline": "JV draft", "body": "<p>x</p>", "category": "Local News",
                                        "subcategory": "JV/Varsity", "action": "save"})
        self.assertEqual(d.val("SELECT subcategory FROM stories WHERE id=?", (s2,)), "")
        self.assertIn("Chargers win homecoming", self.c.get("/admin/content?sub=College").text)
        self.assertNotIn("Eighth graders", self.c.get("/admin/content?sub=College").text.split("<tbody>")[1])

    def test_40_crowns_secrets_and_call_it(self):
        from app import callit, community, crowns, sports, util, settings as st
        d = self.db()
        d.run("DELETE FROM rate")
        t = dbm.now()
        ann = self.member_client("annie_fan")
        bo = self.member_client("bo_fan")
        aid, bid = self.mid("annie_fan"), self.mid("bo_fan")
        sid = sports.add_school(d, "Camden-Frontier", "Camden", "Redskins", "High school", "Camden")
        tid = sports.find_or_make_team(d, sid, "Football", "Boys", "Varsity")
        start = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
        gid = d.insert("games", team_id=tid, opponent="Pittsford", home="home", starts_at=start, status="scheduled",
                       created_at=t, updated_at=t)
        game = lambda: d.one("SELECT * FROM games WHERE id=?", (gid,))
        # ── tiers: badges level up, with a celebration the first time you see it ──
        sports.post_update(d, game(), d.one("SELECT * FROM members WHERE id=?", (aid,)), "Kickoff!")
        self.assertEqual(d.val("SELECT mb.tier FROM member_badges mb JOIN badges b ON b.id=mb.badge_id "
                               "WHERE b.slug='sideline-reporter' AND mb.member_id=?", (aid,)), 1)
        page = ann.get("/sports").text
        self.assertIn("data-celebrate", page)
        self.assertIn("Sideline Reporter", page.split("data-celebrate")[1][:600])
        self.assertNotIn("data-celebrate", ann.get("/sports").text)                 # only once
        for i in range(24):
            d.insert("game_updates", game_id=gid, member_id=aid, body=f"play {i}", created_at=t)
        community.check_badges(d, aid)
        self.assertEqual(d.val("SELECT mb.tier FROM member_badges mb JOIN badges b ON b.id=mb.badge_id "
                               "WHERE b.slug='sideline-reporter' AND mb.member_id=?", (aid,)), 2)
        self.assertIn("leveled up to Silver", d.val("SELECT text FROM notices WHERE member_id=? ORDER BY id DESC", (aid,)))
        book = ann.get("/badges").text
        self.assertIn("Silver", book)
        self.assertIn("25 of 100 to Gold", book)
        # ── secret badges: ??? until you find one; the whole site hears about it ──
        late = datetime.now(util.tz(d)).replace(hour=23, minute=30).astimezone(timezone.utc).isoformat(timespec="seconds")
        d.insert("game_updates", game_id=gid, member_id=bid, body="Still going in 3OT!", created_at=late)
        community.check_badges(d, bid)
        self.assertTrue(d.val("SELECT 1 FROM member_badges mb JOIN badges b ON b.id=mb.badge_id WHERE b.slug='night-owl' "
                              "AND mb.member_id=?", (bid,)))
        self.assertIn("@bo_fan just discovered a secret badge: 🌙 Night Owl. First in the county!",
                      d.val("SELECT text FROM shoutouts ORDER BY id DESC"))
        book = ann.get("/badges").text                                             # annie hasn't found it
        owl = book.split("Past bedtime, and still posting.")[0][-400:]
        self.assertIn("???", owl)
        self.assertNotIn("Posted a live update after 11 p.m.", book)              # how to get it stays secret
        self.assertIn("Found by 1 person", book)
        self.assertIn("Night Owl", bo.get("/badges").text)
        self.assertIn("data-shout", self.app.test_client().get("/sports").text)    # site-wide shout-out
        share = self.app.test_client().get("/u/bo_fan/badge/night-owl")
        self.assertEqual(share.status_code, 200)
        self.assertIn('og:image', share.text)
        self.assertNotIn("after 11 p.m.", share.text)                               # sharing keeps the secret
        self.assertIn("Past bedtime", share.text)
        img = self.app.test_client().get("/u/bo_fan/badge/night-owl.png")
        self.assertEqual((img.status_code, img.mimetype), (200, "image/png"))
        self.assertEqual(self.app.test_client().get("/u/annie_fan/badge/night-owl").status_code, 404)
        # ── crowns: the #1 fan of a team, until someone passes them ──
        community.award(d, aid, 20, "score", f"game:{gid}")                         # annie posts the final
        king = d.one("SELECT * FROM crowns WHERE kind='team' AND key=?", (str(tid),))
        self.assertEqual(king["member_id"], aid)                                    # annie's update points
        self.assertIn("👑 #1 Fan · Camden Boys Football", self.app.test_client().get("/leaderboard?board=week").text)
        community.award(d, bid, 30, "stream", f"game:{gid}:v999")                  # bo streams the game
        king = d.one("SELECT * FROM crowns WHERE kind='team' AND key=?", (str(tid),))
        self.assertEqual(king["member_id"], bid)
        self.assertIn("@bo_fan just took your crown", d.val("SELECT text FROM notices WHERE member_id=? AND kind='crown' "
                                                            "ORDER BY id DESC", (aid,)))
        team_page = self.app.test_client().get("/sports/team/" + d.val("SELECT slug FROM sports_teams WHERE id=?", (tid,))).text
        self.assertIn("@bo_fan", team_page.split('id="fans"')[1][:900])
        self.assertIn("needs", team_page.split('id="fans"')[1])
        self.assertIn("#1 Fan · Camden Boys Football", self.app.test_client().get("/crowns").text)
        self.assertIn("#1 Fan", bo.get("/u/bo_fan").text)
        # a crown never gives away an anonymous writer
        s_anon = d.insert("stories", headline="Anonymous scoop", slug="anon-scoop", status="published", category="Health",
                          member_id=aid, credit="byline", credit_public=0, published_at=t, created_at=t, updated_at=t)
        community.award(d, aid, 20, "article", f"story:{s_anon}")
        self.assertFalse(d.val("SELECT member_id FROM crowns WHERE kind='section' AND key='Health'"))
        # ── Call It on a story: closest wins, and the Oracle crown ──
        story = d.insert("stories", headline="Winter storm watch", slug="storm-watch", status="published",
                         category="Weather", published_at=t, created_at=t, updated_at=t)
        soon = (datetime.now(util.tz(d)) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        self.post(f"/admin/story/{story}/callit", {"action": "create", "question": "How much snow will Hillsdale get?",
                                                  "kind": "number", "lo": "0", "hi": "12", "step": "0.5",
                                                  "unit": "inches", "closes_at": soon})
        pid = d.val("SELECT id FROM predictions WHERE story_id=?", (story,))
        self.assertIn("Call It: How much snow", self.app.test_client().get("/story/storm-watch").text)
        self.post(f"/callit/{pid}", {"value": "4.5"}, client=ann)
        self.post(f"/callit/{pid}", {"value": "7"}, client=bo)
        r = self.post(f"/callit/{pid}", {"value": "9"}, client=bo, follow_redirects=True)
        self.assertIn("already called", r.text)                                      # one guess each
        page = ann.get("/story/storm-watch").text
        self.assertIn("You called it", page)
        self.assertIn("ci-dist", page)                                               # the spread, once you've guessed
        d.run("UPDATE predictions SET closes_at=? WHERE id=?", (t, pid))
        self.assertEqual(self.post(f"/callit/{pid}", {"value": "5"}, client=self.member_client("late_larry"),
                                   follow_redirects=True).status_code, 200)
        self.assertEqual(d.val("SELECT COUNT(*) FROM guesses WHERE prediction_id=?", (pid,)), 2)   # closed
        self.assertIn("Needs the answer", self.c.get("/admin/callit").text)
        self.post(f"/admin/story/{story}/callit", {"action": "resolve", "pid": pid, "answer": "4.5"})
        win = d.one("SELECT * FROM guesses WHERE prediction_id=? AND member_id=?", (pid, aid))
        self.assertEqual((win["won"], win["exact"], win["points"]), (1, 1, st.get(d, "pts_callit_win")))
        self.assertEqual(d.val("SELECT points FROM guesses WHERE prediction_id=? AND member_id=?", (pid, bid)),
                         st.get(d, "pts_callit_close"))
        self.assertEqual(d.val("SELECT member_id FROM crowns WHERE kind='oracle'"), aid)
        self.assertTrue(d.val("SELECT 1 FROM member_badges mb JOIN badges b ON b.id=mb.badge_id WHERE b.slug='bullseye' "
                              "AND mb.member_id=?", (aid,)))
        self.assertIn("The answer: 4.5 inches", self.app.test_client().get("/story/storm-watch").text)
        # fixing the answer redoes the points
        self.post(f"/admin/story/{story}/callit", {"action": "resolve", "pid": pid, "answer": "7"})
        self.assertEqual(d.val("SELECT won FROM guesses WHERE prediction_id=? AND member_id=?", (pid, bid)), 1)
        self.assertEqual(d.val("SELECT COUNT(*) FROM points_log WHERE ref=?", (f"callit:{pid}",)), 2)
        # ── Call the score on a game: settles once the final is checked ──
        later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        g2 = d.insert("games", team_id=tid, opponent="Addison", home="away", starts_at=later, status="scheduled",
                      created_at=t, updated_at=t)
        self.assertIn("Call the score", self.app.test_client().get(f"/sports/game/{g2}").text)
        gp = d.val("SELECT id FROM predictions WHERE game_id=?", (g2,))
        self.post(f"/callit/{gp}", {"ours": "28", "theirs": "14"}, client=ann)
        self.post(f"/callit/{gp}", {"ours": "21", "theirs": "20"}, client=bo)
        d.run("UPDATE games SET starts_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(
            timespec="seconds"), g2))
        self.post(f"/sports/game/{g2}", {"action": "report", "our_score": "28", "their_score": "14", "status": "final"},
                  client=bo)
        self.assertEqual(d.val("SELECT status FROM predictions WHERE id=?", (gp,)), "open")   # not checked yet
        self.post(f"/sports/game/{g2}", {"action": "confirm"}, client=ann)
        self.assertEqual(d.val("SELECT status FROM predictions WHERE id=?", (gp,)), "resolved")
        self.assertEqual(d.val("SELECT points FROM guesses WHERE prediction_id=? AND member_id=?", (gp, aid)),
                         st.get(d, "pts_callit_exact"))
        self.assertEqual(d.val("SELECT points FROM guesses WHERE prediction_id=? AND member_id=?", (gp, bid)),
                         st.get(d, "pts_callit_winner"))
        self.assertIn("Exactly right!", ann.get(f"/sports/game/{g2}").text)
        # ── leaderboards and levels ──
        for b in ("week", "month", "all", "sports", "callit"):
            self.assertIn("@annie_fan", self.app.test_client().get(f"/leaderboard?board={b}").text, b)
        prof = self.app.test_client().get("/u/annie_fan").text
        self.assertIn("Level", prof)
        self.assertIn(community.level_of(d.val("SELECT points FROM members WHERE id=?", (aid,)))["name"], prof)
        # the newsroom can give a badge levels, a rarity and a hint
        b = d.one("SELECT * FROM badges WHERE slug='helpful'")
        self.post("/admin/community/badges", {"id": b["id"], "name": "Helpful", "icon": "👍", "grp": "community",
                                              "rule": "comment_upvotes_max", "tiers": "3, 9, 27", "rarity": "epic",
                                              "active": "on"})
        b = d.one("SELECT * FROM badges WHERE slug='helpful'")
        self.assertEqual((b["tiers"], b["rarity"], b["n"]), ("[3, 9, 27]", "epic", 3))

    def test_27_ai_does_the_legwork(self):
        from app import ai, writing, settings as st
        d = self.db()
        st.put(d, "ai_research", True)
        src = d.val("SELECT id FROM sources WHERE type='rss' ORDER BY id")
        iid = d.insert("items", source_id=src, hash="lw1", title="Riverton fall parade announced", text="Parade.",
                       fetched_at=dbm.now())
        sid = self.develop([iid], write=False)
        dev = json.loads(d.val("SELECT dev FROM stories WHERE id=?", (sid,)))
        self.assertTrue(dev["researched"])
        self.assertEqual(dev["questions"][0]["found"], "Monday at 7 p.m.")
        self.assertEqual(dev["questions"][0]["src"], "https://city.example/news")
        page = self.c.get(f"/admin/develop/{sid}").text
        self.assertIn("answered 1 of 2 questions", page)
        self.assertIn("✓ Monday at 7 p.m.", page)
        with claude_patch() as call:
            self.post(f"/admin/develop/{sid}", {"action": "write"})
            user = call.call_args[0][2]
        self.assertIn("FOUND BY RESEARCH ON THE WEB", user)
        self.assertIn("Monday at 7 p.m. (source: https://city.example/news)", user)
        self.assertNotIn("When did it happen?", user.split("QUESTIONS NOBODY ANSWERED")[-1])  # answered by research
        s = d.one("SELECT * FROM stories WHERE id=?", (sid,))
        self.assertIn("web research", s["checklist"])
        self.assertIn("city.example", s["cites"])
        # Just write it: research and write in one click, straight to the draft
        j = d.insert("items", source_id=src, hash="lw2", title="Riverton parade float contest", text="Floats.",
                     fetched_at=dbm.now())
        with claude_patch():
            r = self.post(f"/admin/items?one={j}&action=quick", {})
        self.assertIn("/admin/story/", r.headers["Location"])
        sid2 = int(r.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual(d.val("SELECT status FROM stories WHERE id=?", (sid2,)), "draft")
        # research can be switched off; then the AI only asks
        st.put(d, "ai_research", False)
        self.assertNotIn("Use web search", writing.analyze_system(d, False))
        st.put(d, "ai_research", True)
        self.assertIn("Use web search", writing.analyze_system(d, True))
        # the reply's JSON is found after the AI's notes
        self.assertEqual(writing.parse_json('Notes {not json} then {"a": {"b": 1}}'), {"a": {"b": 1}})

        # the real API call: search tool sent, a paused turn is continued, searches are costed,
        # and if web search is off for the account it falls back and says so
        class R:
            def __init__(self, code, js=None, text=""):
                self.status_code, self._js, self.text = code, js, text

            def json(self):
                return self._js
        replies = [R(400, text='{"error":{"message":"web search is not enabled for this organization"}}'),
                   R(200, {"content": [{"type": "text", "text": "part one "}], "stop_reason": "pause_turn",
                           "usage": {"input_tokens": 10, "output_tokens": 5}}),
                   R(200, {"content": [{"type": "text", "text": '{"ok": 1}'}], "stop_reason": "end_turn",
                           "usage": {"input_tokens": 10, "output_tokens": 5}})]
        sent = []
        with mock.patch("app.ai.requests.post", side_effect=lambda *a, **k: (sent.append(json.loads(json.dumps(k["json"]))), replies.pop(0))[1]):
            out = ai.call_claude(d, "sys", "user", purpose="develop", web_search=3)
        self.assertEqual(sent[0]["tools"][0]["type"], "web_search_20250305")
        self.assertEqual(sent[0]["tools"][0]["user_location"]["city"], "Riverton")
        self.assertNotIn("tools", sent[1])
        self.assertEqual(sent[2]["messages"][-1]["role"], "assistant")
        self.assertIn('{"ok": 1}', out)
        self.assertIn("Privacy", st.get(d, "web_search_problem"))
        replies[:] = [R(200, {"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn",
                              "usage": {"input_tokens": 0, "output_tokens": 0,
                                        "server_tool_use": {"web_search_requests": 4}}})]
        before = ai.month_spend(d)
        with mock.patch("app.ai.requests.post", side_effect=lambda *a, **k: replies.pop(0)):
            ai.call_claude(d, "sys", "user", web_search=5)
        self.assertAlmostEqual(ai.month_spend(d) - before, 0.04, places=2)


if __name__ == "__main__":
    unittest.main()
