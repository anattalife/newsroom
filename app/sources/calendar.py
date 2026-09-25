"""Community calendars in the standard iCalendar (.ics) format. Events skip the AI and go
straight to the source's approval page as event listings."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .http import get


def _unfold(text):
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(v):
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _dt(value, params, default_tz):
    v = value.strip()
    try:
        if params.get("VALUE") == "DATE" or (len(v) == 8 and v.isdigit()):
            d = date(int(v[:4]), int(v[4:6]), int(v[6:8]))
            return datetime(d.year, d.month, d.day, tzinfo=default_tz), True
        if v.endswith("Z"):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
        tzname = params.get("TZID")
        try:
            tzinfo = ZoneInfo(tzname) if tzname else default_tz
        except Exception:
            tzinfo = default_tz
        return datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tzinfo), False
    except ValueError:
        return None, False


def parse(text, default_tz, days_ahead=60):
    events, cur = [], None
    horizon = datetime.now(timezone.utc) + timedelta(days=days_ahead)
    start_cut = datetime.now(timezone.utc) - timedelta(hours=12)
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("start") and start_cut <= cur["start"] <= horizon and cur.get("title"):
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *pp = head.split(";")
            params = dict(p.split("=", 1) for p in pp if "=" in p)
            name = name.upper()
            if name == "SUMMARY":
                cur["title"] = _unescape(value).strip()
            elif name == "DESCRIPTION":
                cur["text"] = _unescape(value).strip()
            elif name == "LOCATION":
                cur["location"] = _unescape(value).strip()
            elif name == "URL":
                cur["url"] = value.strip()
            elif name == "UID":
                cur["uid"] = value.strip()
            elif name in ("DTSTART", "DTEND"):
                d, all_day = _dt(value, params, default_tz)
                cur["start" if name == "DTSTART" else "end"] = d
                if name == "DTSTART":
                    cur["all_day"] = all_day
    return events


def fetch(source, config, secret, default_tz=None):
    tzinfo = default_tz or ZoneInfo("America/Chicago")
    r = get(config["url"])
    items = []
    for e in parse(r.content.decode("utf-8", "replace"), tzinfo):
        items.append({
            "key": (e.get("uid") or e["title"]) + "|" + e["start"].isoformat(),
            "title": e["title"],
            "url": e.get("url", ""),
            "text": e.get("text", "")[:3000],
            "published": "",
            "extra": {"event_start": e["start"].astimezone(timezone.utc).isoformat(),
                      "event_end": e["end"].astimezone(timezone.utc).isoformat() if e.get("end") else None,
                      "all_day": e.get("all_day", False),
                      "location": e.get("location", "")},
        })
    return items
