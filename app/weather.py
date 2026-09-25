"""Weather page data from the US National Weather Service (free, official, no key).

The worker refreshes each location every 15 minutes and stores the result, so visitors never
wait on the weather service and a busy day can't get the site rate-limited.
"""
import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from . import settings, util
from .db import now
from .sources.http import FetchError, get

log = logging.getLogger("newsroom.weather")
API = "https://api.weather.gov"
GEO = {"Accept": "application/geo+json"}
REFRESH_MIN = 15

DEFAULTS = {
    "weather_enabled": True,
    "weather_locations": [],          # [{slug, name, lat, lon}]
    "weather_units": "F",
    "weather_show_current": True,
    "weather_show_hourly": True,
    "weather_show_daily": True,
    "weather_show_radar": True,
    "weather_show_alerts": True,
    "weather_home_strip": True,
    "weather_alert_banner": "severe",  # none | severe | all
}


def cfg(db):
    stored = {r["key"]: json.loads(r["value"]) for r in
              db.q("SELECT key, value FROM settings WHERE key LIKE 'weather_%'")}
    return {k: stored.get(k, v) for k, v in DEFAULTS.items()}


def save_cfg(db, **values):
    for k, v in values.items():
        if k in DEFAULTS:
            settings.put(db, k, v)


# ── fetching ────────────────────────────────────────────
def geocode(query):
    """Town name → up to 5 US matches, via OpenStreetMap's free Nominatim service."""
    r = get("https://nominatim.openstreetmap.org/search?format=json&limit=5&countrycodes=us&q=" + quote(query))
    return [{"name": x.get("display_name", ""), "lat": round(float(x["lat"]), 4), "lon": round(float(x["lon"]), 4)}
            for x in r.json()]


def _j(url):
    return get(url, headers=GEO).json()


def fetch(lat, lon):
    pts = _j(f"{API}/points/{lat:.4f},{lon:.4f}").get("properties") or {}
    if not pts.get("forecast"):
        raise FetchError("The weather service has no forecast for that spot. US locations only.")
    daily = _j(pts["forecast"]).get("properties", {}).get("periods", [])
    hourly = _j(pts["forecastHourly"]).get("properties", {}).get("periods", [])[:24]
    current = {}
    try:
        stations = _j(pts["observationStations"]).get("features", [])
        if stations:
            sid = stations[0]["properties"]["stationIdentifier"]
            obs = _j(f"{API}/stations/{sid}/observations/latest").get("properties", {})
            current = {
                "station": sid,
                "text": obs.get("textDescription") or "",
                "temp_c": (obs.get("temperature") or {}).get("value"),
                "feels_c": ((obs.get("heatIndex") or {}).get("value") or (obs.get("windChill") or {}).get("value")),
                "wind_kmh": (obs.get("windSpeed") or {}).get("value"),
                "wind_dir": (obs.get("windDirection") or {}).get("value"),
                "humidity": (obs.get("relativeHumidity") or {}).get("value"),
                "at": obs.get("timestamp"),
            }
    except Exception as e:  # the forecast is still useful without current conditions
        log.info("no current observation: %s", e)
    alerts = []
    for f in _j(f"{API}/alerts/active?point={lat:.4f},{lon:.4f}").get("features", []):
        p = f.get("properties", {})
        alerts.append({k: p.get(k) for k in ("id", "event", "headline", "severity", "urgency", "description",
                                               "instruction", "expires", "ends", "areaDesc")})
    rel = (pts.get("relativeLocation") or {}).get("properties", {})
    return {"daily": daily, "hourly": hourly, "current": current, "alerts": alerts,
            "radar": pts.get("radarStation"), "place": f"{rel.get('city', '')}, {rel.get('state', '')}".strip(", ")}


def refresh(db, loc):
    try:
        data = fetch(float(loc["lat"]), float(loc["lon"]))
        db.run("INSERT INTO weather(slug, data, updated_at, attempted_at, error) VALUES(?,?,?,?,NULL) "
               "ON CONFLICT(slug) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at, "
               "attempted_at=excluded.attempted_at, error=NULL", (loc["slug"], json.dumps(data), now(), now()))
        return True
    except Exception as e:
        msg = str(e) if isinstance(e, (FetchError, ValueError)) else f"Weather service problem ({type(e).__name__})"
        db.run("INSERT INTO weather(slug, data, updated_at, attempted_at, error) VALUES(?,NULL,NULL,?,?) "
               "ON CONFLICT(slug) DO UPDATE SET attempted_at=excluded.attempted_at, error=excluded.error",
               (loc["slug"], now(), msg))
        return False


def refresh_if_due(db, force=False):
    """Refresh each location every 15 minutes; after a failure, wait the same 15 minutes before retrying."""
    c = cfg(db)
    if not c["weather_enabled"]:
        return 0
    n = 0
    for loc in c["weather_locations"]:
        row = db.one("SELECT attempted_at, updated_at FROM weather WHERE slug=?", (loc["slug"],))
        last = util.parse_iso((row["attempted_at"] or row["updated_at"]) if row else None)
        if force or not last or (datetime.now(timezone.utc) - last).total_seconds() >= REFRESH_MIN * 60:
            n += refresh(db, loc)
    return n


def current_alerts(alerts, updated_at):
    """Drop alerts that have ended, and all alerts if the data is too old to trust."""
    fresh = util.parse_iso(updated_at)
    t = datetime.now(timezone.utc)
    if not fresh or (t - fresh).total_seconds() > 2 * 3600:
        return []
    out = []
    for a in alerts:
        end = util.parse_iso(a.get("ends") or a.get("expires"))
        if not end or end > t:
            out.append(a)
    return out


# ── presenting ──────────────────────────────────────────
def _temp(c_or_f, unit_in, unit_out):
    if c_or_f is None:
        return None
    if unit_in == unit_out:
        return round(c_or_f)
    return round(c_or_f * 9 / 5 + 32) if unit_out == "F" else round((c_or_f - 32) * 5 / 9)


def icon_for(text, night=False):
    t = (text or "").lower()
    if "thunder" in t or "storm" in t:
        return "storm"
    if any(w in t for w in ("snow", "flurr", "sleet", "ice", "freezing")):
        return "snow"
    if any(w in t for w in ("rain", "shower", "drizzle")):
        return "rain"
    if any(w in t for w in ("fog", "haze", "smoke", "mist")):
        return "fog"
    if "partly" in t or "mostly sunny" in t or "mostly clear" in t:
        return "partly-night" if night else "partly"
    if "cloud" in t or "overcast" in t:
        return "cloud"
    return "moon" if night else "sun"


def view(db, loc):
    """Everything a weather template needs, in the chosen units. None if not loaded yet."""
    c = cfg(db)
    row = db.one("SELECT * FROM weather WHERE slug=?", (loc["slug"],))
    if not row or not row["data"]:
        return {"ready": False, "error": row["error"] if row else None}
    d = json.loads(row["data"])
    u = c["weather_units"]
    tzinfo = util.tz(db)
    cur = d["current"] or {}
    first = d["daily"][0] if d["daily"] else {}
    current = {
        "temp": _temp(cur.get("temp_c"), "C", u) if cur.get("temp_c") is not None
        else _temp(first.get("temperature"), first.get("temperatureUnit", "F"), u),
        "feels": _temp(cur.get("feels_c"), "C", u),
        "text": cur.get("text") or first.get("shortForecast", ""),
        "wind": (f"{round(cur['wind_kmh'] / 1.609)} mph" if u == "F" else f"{round(cur['wind_kmh'])} km/h")
        if cur.get("wind_kmh") is not None else first.get("windSpeed", ""),
        "humidity": round(cur["humidity"]) if cur.get("humidity") is not None else None,
        "at": cur.get("at"),
    }
    current["icon"] = icon_for(current["text"], night=not first.get("isDaytime", True))
    hourly = []
    for h in d["hourly"]:
        t = util.parse_iso(h.get("startTime"))
        hourly.append({"time": t.astimezone(tzinfo).strftime("%-I %p").replace("AM", "a.m.").replace("PM", "p.m.") if t else "",
                       "temp": _temp(h.get("temperature"), h.get("temperatureUnit", "F"), u),
                       "icon": icon_for(h.get("shortForecast"), not h.get("isDaytime", True)),
                       "text": h.get("shortForecast", ""),
                       "rain": (h.get("probabilityOfPrecipitation") or {}).get("value")})
    days, pending = [], None
    for p in d["daily"]:
        entry = {"name": p.get("name", ""), "temp": _temp(p.get("temperature"), p.get("temperatureUnit", "F"), u),
                 "text": p.get("shortForecast", ""), "detail": p.get("detailedForecast", ""),
                 "icon": icon_for(p.get("shortForecast"), not p.get("isDaytime", True)),
                 "rain": (p.get("probabilityOfPrecipitation") or {}).get("value"), "wind": p.get("windSpeed", "")}
        if p.get("isDaytime"):
            pending = {"day": entry, "night": None}
            days.append(pending)
        elif pending and pending["night"] is None:
            pending["night"] = entry
        else:
            days.append({"day": None, "night": entry})
    today = days[0] if days else {}
    return {
        "ready": True, "error": row["error"], "updated_at": row["updated_at"], "unit": u,
        "current": current, "hourly": hourly, "days": days[:7], "alerts": current_alerts(d["alerts"], row["updated_at"]),
        "place": d.get("place"), "stale": _is_stale(row["updated_at"]),
        "high": (today.get("day") or {}).get("temp"), "low": (today.get("night") or {}).get("temp"),
        "radar": d.get("radar"),
    }


def _is_stale(updated_at):
    fresh = util.parse_iso(updated_at)
    return not fresh or (datetime.now(timezone.utc) - fresh).total_seconds() > 2 * 3600


def banner_alert(db):
    """The alert to show across the top of the site, per the weather settings."""
    c = cfg(db)
    mode = c["weather_alert_banner"]
    if not c["weather_enabled"] or mode == "none" or not c["weather_locations"]:
        return None
    row = db.one("SELECT data, updated_at FROM weather WHERE slug=?", (c["weather_locations"][0]["slug"],))
    if not row or not row["data"]:
        return None
    alerts = current_alerts(json.loads(row["data"]).get("alerts", []), row["updated_at"])
    if mode == "severe":
        alerts = [a for a in alerts if a.get("severity") in ("Severe", "Extreme")]
    return alerts[0] if alerts else None


def current_temp_f(db):
    """The latest temperature at the main location, in °F, from what's already loaded. None if unknown."""
    try:
        locs = cfg(db)["weather_locations"]
        if not locs:
            return None
        row = db.one("SELECT data FROM weather WHERE slug=?", (locs[0]["slug"],))
        cur = (json.loads(row["data"]) or {}).get("current") or {} if row and row["data"] else {}
        return round(cur["temp_c"] * 9 / 5 + 32, 1) if cur.get("temp_c") is not None else None
    except Exception:
        return None
