"""Today's specials. Posted by Community Partners (restaurants, cafés, church suppers), free."""
from datetime import datetime, timedelta

from . import util

MAX_PER_DAY = 10


def today(db):
    return datetime.now(util.tz(db)).date().isoformat()


def days_ahead(db, n=7):
    """[(iso date, 'Today' / 'Tomorrow' / 'Sat, Oct 3'), ...] for the posting form."""
    d0 = datetime.now(util.tz(db)).date()
    out = []
    for i in range(n):
        d = d0 + timedelta(days=i)
        out.append((d.isoformat(), "Today" if i == 0 else "Tomorrow" if i == 1 else d.strftime("%a, %b %d").replace(" 0", " ")))
    return out


SQL = ("SELECT sp.*, o.name AS partner, o.logo AS partner_logo, o.address, o.website FROM specials sp "
       "JOIN orgs o ON o.id=sp.org_id WHERE sp.status='live' AND o.status='approved'")


def on_day(db, day=None, limit=100):
    return db.q(f"{SQL} AND sp.day=? ORDER BY sp.id DESC LIMIT ?", (day or today(db), limit))


def upcoming_for(db, org_id):
    return db.q("SELECT * FROM specials WHERE org_id=? AND status='live' AND day>=? ORDER BY day, id",
                (org_id, today(db)))


def upcoming_all(db):
    return db.q(f"{SQL} AND sp.day>=? ORDER BY sp.day, sp.id DESC LIMIT 300", (today(db),))
