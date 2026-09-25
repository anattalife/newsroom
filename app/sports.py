"""Sports: schools, teams, games. Schedules come in by pasting any text (the AI turns it into games);
scores come from members, instantly, credited and correctable, with a history the newsroom can undo."""
import json
import re
from datetime import datetime, timedelta, timezone

from . import ai, community, settings, util
from .db import loads, now

SPORTS = ["Football", "Basketball", "Volleyball", "Soccer", "Baseball", "Softball", "Wrestling", "Track & Field",
          "Cross Country", "Golf", "Tennis", "Swimming & Diving", "Bowling", "Competitive Cheer", "Hockey",
          "Lacrosse", "Equestrian", "Other"]
GENDERS = ["Boys", "Girls", "Men's", "Women's", "Coed", ""]
LEVELS = ["Varsity", "JV", "Freshman", "Middle school", "Club"]
SCHOOL_LEVELS = ["High school", "Middle school", "College", "Club / rec"]
STATUSES = {"scheduled": "Scheduled", "live": "In progress", "final": "Final", "postponed": "Postponed",
            "cancelled": "Cancelled"}


def unique(db, table, base):
    slug, n = base, 2
    while db.val(f"SELECT 1 FROM {table} WHERE slug=?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def school_name(s):
    return s.get("short") or s["name"]


def team_label(t, with_school=True):
    """'Hillsdale Girls Varsity Basketball' (varsity is left out when it's the only kind)."""
    parts = [school_name(t) if with_school and t.get("name") else ""]
    parts += [t.get("gender") or "", "" if t.get("level") == "Varsity" else t.get("level") or "", t["sport"]]
    return " ".join(p for p in parts if p).strip()


TEAM_SQL = ("SELECT t.*, s.name, s.short, s.mascot, s.slug AS school_slug, s.color, s.level AS school_level "
            "FROM sports_teams t JOIN sports_schools s ON s.id=t.school_id")
GAME_SQL = ("SELECT g.*, t.school_id, t.sport, t.gender, t.level, t.slug AS team_slug, s.name, s.short, s.mascot, "
            "s.slug AS school_slug, s.color, m.username AS reporter FROM games g "
            "JOIN sports_teams t ON t.id=g.team_id JOIN sports_schools s ON s.id=t.school_id "
            "LEFT JOIN members m ON m.id=g.reported_by")


def find_or_make_team(db, school_id, sport, gender="", level="Varsity"):
    sport = sport if sport in SPORTS else (next((x for x in SPORTS if x.lower() == (sport or "").lower()), None)
                                           or (sport or "Other").strip()[:40])
    gender = gender if gender in GENDERS else ""
    level = level if level in LEVELS else "Varsity"
    t = db.one("SELECT * FROM sports_teams WHERE school_id=? AND sport=? AND gender=? AND level=?",
               (school_id, sport, gender, level))
    if t:
        return t["id"]
    s = db.one("SELECT * FROM sports_schools WHERE id=?", (school_id,))
    slug = unique(db, "sports_teams", util.slugify(f"{s['slug']} {gender} {level} {sport}", 80))
    return db.insert("sports_teams", school_id=school_id, sport=sport, gender=gender, level=level, slug=slug,
                     created_at=now())


def record(db, team_id):
    w = l_ = t = 0
    for g in db.q("SELECT our_score, their_score FROM games WHERE team_id=? AND status='final' AND our_score IS NOT NULL "
                  "AND their_score IS NOT NULL", (team_id,)):
        if g["our_score"] > g["their_score"]:
            w += 1
        elif g["our_score"] < g["their_score"]:
            l_ += 1
        else:
            t += 1
    return f"{w}-{l_}" + (f"-{t}" if t else "")


def result_letter(g):
    if g["status"] != "final" or g["our_score"] is None or g["their_score"] is None:
        return ""
    return "W" if g["our_score"] > g["their_score"] else ("L" if g["our_score"] < g["their_score"] else "T")


def when_utc(db, date, time):
    """'2026-10-02' + '19:00' (local) → UTC ISO. No time → noon, flagged as time to be decided."""
    try:
        d = datetime.fromisoformat(date.strip()[:10])
    except (ValueError, AttributeError):
        return None, True
    tbd = True
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", time or "")
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        d = d.replace(hour=int(m.group(1)), minute=int(m.group(2)))
        tbd = False
    else:
        d = d.replace(hour=12)
    return d.replace(tzinfo=util.tz(db)).astimezone(timezone.utc).isoformat(timespec="seconds"), tbd


def local_parts(db, iso):
    d = util.local(db, iso)
    return (d.strftime("%Y-%m-%d"), d.strftime("%H:%M")) if d else ("", "")


# ── pasting a schedule ───────────────────────────────────
def parse_schedule(db, text, school, team=None, year_hint="", writer=None):
    """Any pasted schedule (copied from a website, a PDF, a message) → rows to review before saving."""
    today = datetime.now(util.tz(db)).date().isoformat()
    who = (f"The team is {team_label(team)}." if team else
           f"The school is {school['name']}. Work out the sport, gender (Boys, Girls, Men's, Women's, Coed or empty) "
           f"and level (Varsity, JV, Freshman, Middle school or Club) for each game from the text; if the text "
           f"doesn't say, use Varsity.")
    system = f"""You turn a pasted sports schedule into structured games for a local news site. {who}
Today is {today}. {('The season is ' + year_hint + '. ') if year_hint else ''}If a date has no year, choose the year \
that puts it in the current or coming season.
Read every game in the text. Ignore headings, ads, navigation and anything that isn't a game.
For each game give the opponent's school name only (no "vs", "@" or "at"), whether it's home, away or neutral \
("@", "at" and "away" mean away; "vs" usually means home), the date, the start time in 24-hour form if given, \
the place if given, and the score if the game was already played (our score first).
Return ONLY one JSON object, no other text:
{{"games": [{{"date": "YYYY-MM-DD", "time": "HH:MM or empty", "opponent": "…", "home": "home|away|neutral",
 "location": "…", "sport": "one of: {', '.join(SPORTS)}", "gender": "…", "level": "…",
 "our_score": null or number, "their_score": null or number, "note": "tournament name, 'Senior night', etc."}}]}}"""
    if writer:
        out_text = writer(system, text[:20000])
    else:
        out_text = ai.call_claude(db, system, text[:20000], purpose="schedule", max_tokens=6000,
                                  model=settings.get(db, "ai_model_research"))
    from .writing import parse_json
    out = parse_json(out_text) or {}
    rows = []
    for g in out.get("games") or []:
        if not isinstance(g, dict) or not str(g.get("opponent", "")).strip() or not str(g.get("date", "")).strip():
            continue
        opp = util.text_only(str(g["opponent"]), 120)
        home = g.get("home") if g.get("home") in ("home", "away", "neutral") else "home"
        m = re.match(r"^(vs\.?|v\.|@|at)\s+", opp, re.I)
        if m:
            opp = opp[m.end():].strip()
            home = "away" if m.group(1).lower() in ("@", "at") else home
        row = {"date": str(g.get("date", ""))[:10], "time": str(g.get("time") or "")[:5], "opponent": opp,
               "home": home,
               "location": util.text_only(str(g.get("location") or ""), 160),
               "note": util.text_only(str(g.get("note") or ""), 160),
               "sport": team["sport"] if team else util.text_only(str(g.get("sport") or "Other"), 40),
               "gender": team["gender"] if team else (g.get("gender") if g.get("gender") in GENDERS else ""),
               "level": team["level"] if team else (g.get("level") if g.get("level") in LEVELS else "Varsity")}
        for k in ("our_score", "their_score"):
            try:
                row[k] = int(g.get(k)) if g.get(k) not in (None, "") else None
            except (TypeError, ValueError):
                row[k] = None
        rows.append(row)
    return rows


def save_rows(db, school_id, rows, team_id=None, user_id=None, member_id=None, source="import"):
    """Add the reviewed games. A game already there (same team, day and opponent) is updated, not doubled.
    Returns (added, updated)."""
    added = updated = 0
    for r in rows:
        starts, tbd = when_utc(db, r.get("date", ""), r.get("time", ""))
        if not starts or not r.get("opponent"):
            continue
        tid = team_id or find_or_make_team(db, school_id, r.get("sport"), r.get("gender", ""), r.get("level"))
        day = r["date"][:10]
        fields = {"opponent": r["opponent"][:120], "home": r.get("home", "home"), "starts_at": starts,
                  "time_tbd": 1 if tbd else 0, "location": r.get("location", "")[:160], "note": r.get("note", "")[:160],
                  "updated_at": now()}
        if r.get("our_score") is not None and r.get("their_score") is not None:
            fields.update(our_score=r["our_score"], their_score=r["their_score"], status="final")
        old = next((g for g in db.q("SELECT * FROM games WHERE team_id=? AND lower(opponent)=lower(?)",
                                    (tid, fields["opponent"])) if local_parts(db, g["starts_at"])[0] == day), None)
        if old:
            if old["reported_by"] and "our_score" in fields:  # a member already reported it: keep theirs
                for k in ("our_score", "their_score", "status"):
                    fields.pop(k, None)
            db.update("games", old["id"], **fields)
            updated += 1
        else:
            gid = db.insert("games", team_id=tid, source=source, created_at=now(), **fields)
            db.insert("game_log", game_id=gid, user_id=user_id, member_id=member_id, created_at=now(),
                      change="added from a pasted schedule" if source != "member-one" else "added")
            added += 1
    return added, updated


# ── members reporting scores ─────────────────────────────
def report(db, game, member, our, their, status, detail=""):
    """A member posts or corrects a score. Instant, logged, credited."""
    before = {k: game[k] for k in ("our_score", "their_score", "status", "detail", "reported_by")}
    db.update("games", game["id"], our_score=our, their_score=their, status=status, detail=detail[:40],
              reported_by=member["id"], reported_at=now(), updated_at=now())
    db.insert("game_log", game_id=game["id"], member_id=member["id"], before=json.dumps(before), created_at=now(),
              change=f"{STATUSES.get(status, status)} {our}-{their}" + (f" ({detail})" if detail else ""))
    db.run("DELETE FROM game_confirms WHERE game_id=?", (game["id"],))  # confirmations were for the old score
    if status == "final":
        community.award(db, member["id"], community.pts(db, "pts_score"), "score", f"game:{game['id']}")
    community.check_badges(db, member["id"])
    _settle(db, game["id"])


def _settle(db, game_id):
    """Keep the game's 'Call the score' results in step with its score."""
    from . import callit
    g = db.one("SELECT * FROM games WHERE id=?", (game_id,))
    if g:
        callit.settle_game(db, g)


def confirm(db, game, member):
    score = f"{game['our_score']}-{game['their_score']}"
    db.run("INSERT OR IGNORE INTO game_confirms(game_id, member_id, score, created_at) VALUES(?,?,?,?)",
           (game["id"], member["id"], score, now()))
    community.check_badges(db, member["id"])
    _settle(db, game["id"])


def revert(db, game_id, user_id):
    """Undo the last score change (the newsroom's button)."""
    last = db.one("SELECT * FROM game_log WHERE game_id=? AND before!='{}' ORDER BY id DESC LIMIT 1", (game_id,))
    if not last:
        return False
    before = loads(last["before"], {})
    db.update("games", game_id, updated_at=now(), **{k: before.get(k) for k in
                                                    ("our_score", "their_score", "status", "detail", "reported_by")})
    db.run("DELETE FROM game_log WHERE id=?", (last["id"],))
    db.insert("game_log", game_id=game_id, user_id=user_id, change="newsroom undid the last change", created_at=now())
    if last["member_id"]:
        community.revoke(db, last["member_id"], "score", f"game:{game_id}")
    _settle(db, game_id)
    return True


def upcoming_and_recent(db, where="1=1", args=(), days_back=2, days_ahead=7, limit=200):
    t = datetime.now(timezone.utc)
    return db.q(f"{GAME_SQL} WHERE {where} AND g.starts_at BETWEEN ? AND ? ORDER BY g.starts_at LIMIT ?",
                (*args, (t - timedelta(days=days_back)).isoformat(timespec="seconds"),
                 (t + timedelta(days=days_ahead)).isoformat(timespec="seconds"), limit))


def strip(db, limit=14):
    """The homepage strip: finals from the last day and a half, then today's games."""
    t = datetime.now(timezone.utc)
    end_today = datetime.now(util.tz(db)).replace(hour=23, minute=59).astimezone(timezone.utc)
    rows = db.q(f"{GAME_SQL} WHERE g.status IN ('final','live') AND g.starts_at >= ? ORDER BY g.starts_at DESC LIMIT ?",
                ((t - timedelta(hours=36)).isoformat(timespec="seconds"), limit))
    rows += db.q(f"{GAME_SQL} WHERE g.status='scheduled' AND g.starts_at BETWEEN ? AND ? ORDER BY g.starts_at LIMIT ?",
                 ((t - timedelta(hours=3)).isoformat(timespec="seconds"), end_today.isoformat(timespec="seconds"),
                  limit))
    return rows[:limit]


# ── showing games ────────────────────────────────────────
def _time(d):
    return d.strftime("%I:%M %p").lstrip("0")


def decorate(db, rows):
    """Adds the words a page needs: day, time, 'Hillsdale 28, Jonesville 14', W/L."""
    today = datetime.now(util.tz(db)).date()
    for g in rows:
        d = util.local(db, g["starts_at"])
        g["day"] = d.date().isoformat() if d else ""
        if d:
            delta = (d.date() - today).days
            g["day_label"] = ("Today" if delta == 0 else "Tomorrow" if delta == 1 else "Yesterday" if delta == -1
                              else d.strftime("%a, %b %d").replace(" 0", " "))
            g["time_label"] = "Time TBA" if g["time_tbd"] else _time(d)
        else:
            g["day_label"] = g["time_label"] = ""
        g["team"] = team_label(g)
        g["us"] = school_name(g)
        g["sport_label"] = team_label(g, with_school=False)
        g["result"] = result_letter(g)
        has_score = g["our_score"] is not None and g["their_score"] is not None
        g["has_score"] = has_score
        joiner = {"home": "vs.", "away": "at", "neutral": "vs."}.get(g["home"], "vs.")
        g["matchup"] = f"{g['us']} {joiner} {g['opponent']}"
        g["score_line"] = (f"{g['us']} {g['our_score']}, {g['opponent']} {g['their_score']}" if has_score else g["matchup"])
        g["status_label"] = STATUSES.get(g["status"], g["status"])
        g["live"] = live_state(g)
    return rows


def dedupe(db, rows):
    """When both schools are on the site, the same game is listed twice. Keep the home team's copy."""
    names = {}
    for s in db.q("SELECT id, name, short FROM sports_schools"):
        for n in (s["name"], s["short"]):
            if n:
                names[n.strip().lower()] = s["id"]
    seen, out = {}, []
    for g in rows:
        opp = names.get(g["opponent"].strip().lower())
        if opp is None:
            out.append(g)
            continue
        key = (g.get("day") or g["starts_at"][:10], g["sport"], g["gender"], g["level"],
               frozenset((opp, g["school_id"])))
        if key in seen:
            if g["home"] == "home" or (g["has_score"] and not out[seen[key]]["has_score"]):
                out[seen[key]] = g
            continue
        seen[key] = len(out)
        out.append(g)
    return out


def rows_from_form(f):
    """The editable preview table posts parallel lists; keep only ticked rows."""
    keep = set(f.getlist("keep"))
    cols = ("date", "time", "opponent", "home", "location", "sport", "gender", "level", "our_score", "their_score",
            "note")
    lists = {c: f.getlist(c) for c in cols}
    rows = []
    for i in range(len(lists["opponent"])):
        if str(i) not in keep:
            continue
        r = {c: (lists[c][i] if i < len(lists[c]) else "").strip() for c in cols}
        r["opponent"] = util.text_only(r["opponent"], 120)
        if not r["opponent"] or not r["date"]:
            continue
        r["home"] = r["home"] if r["home"] in ("home", "away", "neutral") else "home"
        r["gender"] = r["gender"] if r["gender"] in GENDERS else ""
        r["level"] = r["level"] if r["level"] in LEVELS else "Varsity"
        for k in ("our_score", "their_score"):
            r[k] = int(r[k]) if r[k].isdigit() else None
        rows.append(r)
    return rows


def add_school(db, name, short="", mascot="", level="High school", town="", color=""):
    name = util.text_only(name, 120)
    if not name:
        raise ValueError("Give the school a name.")
    old = db.one("SELECT id FROM sports_schools WHERE lower(name)=lower(?)", (name,))
    if old:
        return old["id"]
    color = color if re.match(r"^#[0-9A-Fa-f]{6}$", color or "") else "#1B2433"
    return db.insert("sports_schools", name=name, short=util.text_only(short, 40), mascot=util.text_only(mascot, 60),
                     level=level if level in SCHOOL_LEVELS else "High school", town=util.text_only(town, 80),
                     color=color, slug=unique(db, "sports_schools", util.slugify(short or name, 60)),
                     created_at=now())


# ── game night: Live mode ────────────────────────────────
LIVE_BEFORE = timedelta(minutes=30)   # Live mode starts this long before the listed start
LIVE_AFTER = timedelta(hours=4)       # …and ends at the final score, or this long after the start
LIVE_POINTS_PER_GAME = 10             # most update points one member can earn at one game


def live_state(g, at=None):
    """'upcoming', 'live' or 'over'."""
    if g["status"] in ("final", "cancelled", "postponed"):
        return "over"
    start = util.parse_iso(g["starts_at"])
    t = at or datetime.now(timezone.utc)
    if not start:
        return "upcoming"
    if g["status"] == "live" and t <= start + LIVE_AFTER + timedelta(hours=2):
        return "live"
    if start - LIVE_BEFORE <= t <= start + LIVE_AFTER:
        return "live"
    return "over" if t > start + LIVE_AFTER else "upcoming"


def updates(db, game_id, after=0, include_hidden=False):
    shown = "" if include_hidden else "AND u.status='visible'"
    return db.q("SELECT u.*, m.username, m.display_name FROM game_updates u LEFT JOIN members m ON m.id=u.member_id "
                f"WHERE u.game_id=? AND u.id>? {shown} ORDER BY u.id DESC", (game_id, after))


def post_update(db, game, member, body, our=None, their=None):
    """A member's line in the game feed. With a score, it also updates the game's score (as 'In progress')."""
    from . import weather
    uid = db.insert("game_updates", game_id=game["id"], member_id=member["id"], body=body[:280],
                    our_score=our, their_score=their, temp_f=weather.current_temp_f(db), created_at=now())
    if our is not None and their is not None and (our, their) != (game["our_score"], game["their_score"]):
        report(db, game, member, our, their, "live", game.get("detail") or "")
    earned = db.val("SELECT COUNT(*) FROM points_log WHERE member_id=? AND reason='live' AND ref LIKE ?",
                    (member["id"], f"game:{game['id']}:%"))
    if earned < LIVE_POINTS_PER_GAME:
        community.award(db, member["id"], community.pts(db, "pts_live"), "live", f"game:{game['id']}:u{uid}")
    community.check_badges(db, member["id"])
    return uid


def hide_update(db, update_id):
    u = db.one("SELECT * FROM game_updates WHERE id=?", (update_id,))
    if u:
        db.run("UPDATE game_updates SET status='hidden' WHERE id=?", (update_id,))
        community.revoke(db, u["member_id"], "live", f"game:{u['game_id']}:u{u['id']}")


# ── live streams and videos of a game ─────────────────────
def videos(db, game_id):
    rows = db.q("SELECT v.*, m.username FROM game_videos v LEFT JOIN members m ON m.id=v.member_id "
                "WHERE v.game_id=? AND v.status='visible' ORDER BY v.kind='stream' DESC, v.id DESC", (game_id,))
    for v in rows:
        v["embed"] = util.video_embed(v["url"])
    return [v for v in rows if v["embed"]]


def add_video(db, game, member, url, kind):
    """Share a live stream or a video. A game's first live stream earns big points; other videos earn some."""
    url = (url or "").strip()[:500]
    if not util.video_embed(url):
        raise ValueError("Paste a YouTube or Facebook video link, like youtube.com/live/… or facebook.com/…/videos/….")
    if db.val("SELECT 1 FROM game_videos WHERE game_id=? AND url=? AND status='visible'", (game["id"], url)):
        raise ValueError("That video is already on this game's page.")
    kind = "stream" if kind == "stream" else "video"
    vid = db.insert("game_videos", game_id=game["id"], member_id=member["id"], url=url, kind=kind, created_at=now())
    first_stream = kind == "stream" and not db.val(
        "SELECT 1 FROM points_log WHERE reason='stream' AND ref LIKE ?", (f"game:{game['id']}:%",))
    if first_stream:
        community.award(db, member["id"], community.pts(db, "pts_stream"), "stream", f"game:{game['id']}:v{vid}")
    else:
        community.award(db, member["id"], community.pts(db, "pts_video"), "video", f"game:{game['id']}:v{vid}")
    community.check_badges(db, member["id"])
    return vid, first_stream


def hide_video(db, video_id):
    v = db.one("SELECT * FROM game_videos WHERE id=?", (video_id,))
    if v:
        db.run("UPDATE game_videos SET status='hidden' WHERE id=?", (video_id,))
        for reason in ("stream", "video"):
            community.revoke(db, v["member_id"], reason, f"game:{v['game_id']}:v{v['id']}")


def video_counts(db, game_ids):
    if not game_ids:
        return {}
    marks = ",".join("?" * len(game_ids))
    return {r["game_id"]: r["kind"] for r in db.q(
        f"SELECT game_id, MAX(kind) AS kind FROM game_videos WHERE status='visible' AND game_id IN ({marks}) "
        f"GROUP BY game_id", game_ids)}


# ── the team's "Good to know" box ────────────────────────
INFO_MAX = 1500


def save_info(db, team, text, member=None, user_id=None):
    text = (text or "").strip()[:INFO_MAX]
    db.run("UPDATE sports_teams SET info=?, info_by=?, info_at=? WHERE id=?",
           (text, member["id"] if member else None, now(), team["id"]))
