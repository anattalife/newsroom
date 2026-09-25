"""US National Weather Service alerts for the coverage area (free, official, no key needed)."""
from .http import get


def _county_match(area_desc, county):
    if not county:
        return True
    c = county.lower().replace(" county", "").replace(" parish", "").strip()
    return any(c == part.strip().lower().replace(" county", "").replace(" parish", "")
               for part in area_desc.replace(",", ";").split(";"))


def fetch(source, config, secret):
    state = (config.get("state") or "").upper()
    county = config.get("county") or ""
    if not state:
        raise ValueError("Set your state (and county) under Settings → Coverage area, or on this source.")
    r = get(f"https://api.weather.gov/alerts/active?area={state}", headers={"Accept": "application/geo+json"})
    items = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        if not _county_match(p.get("areaDesc", ""), county):
            continue
        text = "\n\n".join(x for x in (p.get("description"), p.get("instruction")) if x)
        items.append({
            "key": p.get("id") or f.get("id"),
            "title": p.get("headline") or p.get("event") or "Weather alert",
            "url": p.get("@id") or f.get("id") or "",
            "text": f"{p.get('event', '')} — {p.get('severity', '')}. Areas: {p.get('areaDesc', '')}. "
                    f"Effective {p.get('effective', '')} until {p.get('expires') or p.get('ends') or 'further notice'}.\n\n{text}",
            "published": p.get("sent", ""),
            "extra": {"event": p.get("event"), "severity": p.get("severity"), "expires": p.get("expires")},
        })
    return items
