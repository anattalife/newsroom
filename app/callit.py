"""Call It: quick predictions on stories and games. Everyone can play, even people who never post.

  number   "How much snow will Hillsdale get?"  closest guess wins; the next 9 closest earn some points
  choice   "Will the millage pass?"             everyone who picked right earns points
  score    "Call the score" on every game       exact score wins big; picking the winner earns a little
"""
import json
from datetime import datetime, timedelta, timezone

from . import community, util
from .db import loads, now

TOP_CLOSE = 10


def _pts(db, key):
    return community.pts(db, key)


def get(db, pid):
    p = db.one("SELECT * FROM predictions WHERE id=?", (pid,))
    return decorate(db, p) if p else None


def decorate(db, p, member=None):
    p["choices_list"] = loads(p["choices"], [])
    closes = util.parse_iso(p["closes_at"])
    p["closed"] = p["status"] != "open" or (closes is not None and datetime.now(timezone.utc) >= closes)
    p["closes_label"] = util.fmt_date(util.local(db, p["closes_at"])) if closes else ""
    p["count"] = db.val("SELECT COUNT(*) FROM guesses WHERE prediction_id=?", (p["id"],))
    p["mine"] = db.one("SELECT * FROM guesses WHERE prediction_id=? AND member_id=?",
                       (p["id"], member["id"])) if member else None
    return p


def for_story(db, story_id, member=None):
    p = db.one("SELECT * FROM predictions WHERE story_id=? AND status!='void' ORDER BY id DESC LIMIT 1", (story_id,))
    return decorate(db, p, member) if p else None


def for_game(db, game, member=None, create=True):
    """Every game gets 'Call the score' automatically; it closes at the start time."""
    p = db.one("SELECT * FROM predictions WHERE game_id=? AND kind='score' ORDER BY id DESC LIMIT 1", (game["id"],))
    if not p and create and game["status"] == "scheduled":
        pid = db.insert("predictions", game_id=game["id"], kind="score", question="Call the score",
                        closes_at=game["starts_at"], created_at=now())
        p = db.one("SELECT * FROM predictions WHERE id=?", (pid,))
    if p and p["status"] == "open" and p["closes_at"] != game["starts_at"]:
        db.run("UPDATE predictions SET closes_at=? WHERE id=?", (game["starts_at"], p["id"]))  # the time moved
        p["closes_at"] = game["starts_at"]
    return decorate(db, p, member) if p else None


def create(db, story_id, question, kind, closes_at, unit="", lo=None, hi=None, step=None, choices=(), user_id=None):
    question = util.text_only(question, 200)
    if len(question) < 5:
        raise ValueError("Write the question.")
    if kind not in ("number", "choice"):
        raise ValueError("Pick a kind of question.")
    choices = [util.text_only(c, 60) for c in choices if util.text_only(c, 60)]
    if kind == "choice" and len(choices) < 2:
        raise ValueError("Give at least two answers to choose from.")
    if kind == "number":
        lo, hi = float(lo if lo not in (None, "") else 0), float(hi if hi not in (None, "") else 100)
        if hi <= lo:
            raise ValueError("The highest guess has to be more than the lowest.")
        step = float(step) if step not in (None, "") and float(step) > 0 else (0.5 if hi - lo <= 20 else 1)
    if not closes_at:
        raise ValueError("Say when guessing closes.")
    return db.insert("predictions", story_id=story_id, kind=kind, question=question, unit=util.text_only(unit, 20),
                     lo=lo, hi=hi, step=step, choices=json.dumps(choices[:8]), closes_at=closes_at,
                     created_by=user_id, created_at=now())


def guess(db, p, member, value):
    """Lock in one guess. Returns the guess row."""
    if p["closed"]:
        raise ValueError("Guessing has closed for this one.")
    if db.val("SELECT 1 FROM guesses WHERE prediction_id=? AND member_id=?", (p["id"], member["id"])):
        raise ValueError("You've already called this one.")
    num = None
    if p["kind"] == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError("Pick a number.")
        num = min(max(num, p["lo"]), p["hi"])
        value = _fmt(num)
    elif p["kind"] == "choice":
        if value not in p["choices_list"]:
            raise ValueError("Pick one of the answers.")
    else:
        a, b = value
        if not (str(a).isdigit() and str(b).isdigit()):
            raise ValueError("Enter both scores.")
        value = f"{int(a)}-{int(b)}"
    db.insert("guesses", prediction_id=p["id"], member_id=member["id"], value=value, num=num, created_at=now())
    community.check_badges(db, member["id"])
    return value


def _fmt(x):
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def distribution(db, p, buckets=24):
    """Counts for the little chart: numbers in buckets, choices by answer, scores by who wins."""
    rows = db.q("SELECT value, num FROM guesses WHERE prediction_id=?", (p["id"],))
    if p["kind"] == "number":
        lo, hi = p["lo"], p["hi"]
        width = (hi - lo) / buckets or 1
        counts = [0] * buckets
        for r in rows:
            counts[min(buckets - 1, int(((r["num"] or lo) - lo) / width))] += 1
        top = max(counts) or 1
        return [{"n": c, "h": int(c * 100 / top), "from": lo + i * width} for i, c in enumerate(counts)]
    if p["kind"] == "choice":
        total = len(rows) or 1
        return [{"label": c, "n": sum(1 for r in rows if r["value"] == c),
                 "pct": int(sum(1 for r in rows if r["value"] == c) * 100 / total)} for c in p["choices_list"]]
    ours = sum(1 for r in rows if _winner(r["value"]) == "us")
    theirs = sum(1 for r in rows if _winner(r["value"]) == "them")
    total = len(rows) or 1
    return {"us": int(ours * 100 / total), "them": int(theirs * 100 / total), "total": len(rows)}


def _winner(v):
    try:
        a, b = (int(x) for x in v.split("-"))
    except (ValueError, AttributeError):
        return None
    return "us" if a > b else "them" if b > a else "tie"


def _clear(db, p):
    for g_ in db.q("SELECT * FROM guesses WHERE prediction_id=?", (p["id"],)):
        community.revoke(db, g_["member_id"], "callit", f"callit:{p['id']}")
    db.run("UPDATE guesses SET rank=NULL, won=0, exact=0, points=0 WHERE prediction_id=?", (p["id"],))


def resolve(db, p, answer, announce=True):
    """Score everyone. Safe to run again with a corrected answer: earlier points are taken back first."""
    _clear(db, p)
    rows = db.q("SELECT g.*, m.username FROM guesses g JOIN members m ON m.id=g.member_id WHERE g.prediction_id=? "
                "ORDER BY g.id", (p["id"],))
    results = []  # (guess, rank, points, won, exact)
    if p["kind"] == "number":
        answer_num = float(answer)
        answer = _fmt(answer_num)
        ranked = sorted(rows, key=lambda r: (abs(r["num"] - answer_num), r["id"]))
        rank, last = 0, None
        for i, r in enumerate(ranked):
            diff = abs(r["num"] - answer_num)
            if diff != last:
                rank, last = i + 1, diff
            pts = _pts(db, "pts_callit_win") if rank == 1 else _pts(db, "pts_callit_close") if rank <= TOP_CLOSE else 0
            results.append((r, rank, pts, rank == 1, diff == 0))
    elif p["kind"] == "choice":
        for r in rows:
            right = r["value"] == answer
            results.append((r, 1 if right else None, _pts(db, "pts_callit_choice") if right else 0, False, False))
    else:
        for r in rows:
            exact = r["value"] == answer
            right = _winner(r["value"]) == _winner(answer)
            pts = _pts(db, "pts_callit_exact") if exact else _pts(db, "pts_callit_winner") if right else 0
            results.append((r, 1 if exact else (2 if right else None), pts, exact, exact))
    for r, rank, pts, won, exact in results:
        db.run("UPDATE guesses SET rank=?, won=?, exact=?, points=? WHERE id=?",
               (rank, 1 if won else 0, 1 if exact else 0, pts, r["id"]))
        if pts:
            community.award(db, r["member_id"], pts, "callit", f"callit:{p['id']}")
    db.run("UPDATE predictions SET answer=?, status='resolved', resolved_at=? WHERE id=?", (answer, now(), p["id"]))
    for r, *_ in results:
        community.check_badges(db, r["member_id"])
    winners = [r for r, rank, pts, won, exact in results if won]
    if announce and winners:
        link = _link(db, p)
        names = ", ".join(f"@{w['username']}" for w in winners[:3]) + (f" and {len(winners) - 3} more" if len(winners) > 3 else "")
        text = (f"🎯 {names} called the exact score!" if p["kind"] == "score"
                else f"🎯 {names} called it: {p['question']} {answer}{(' ' + p['unit']) if p['unit'] else ''}")
        community.shoutout(db, text, link, kind="callit", member_id=winners[0]["member_id"])
        for w in winners:
            community.notice(db, w["member_id"], f"🎯 You called it! “{p['question']}”", link, kind="callit")
    return results


def _link(db, p):
    if p["game_id"]:
        return f"/sports/game/{p['game_id']}#callit"
    slug = db.val("SELECT slug FROM stories WHERE id=?", (p["story_id"],))
    return f"/story/{slug}#callit" if slug else "/"


def results(db, p, limit=5):
    return db.q("SELECT g.*, m.username FROM guesses g JOIN members m ON m.id=g.member_id WHERE g.prediction_id=? "
                "AND g.points > 0 ORDER BY g.rank, g.id LIMIT ?", (p["id"], limit))


# ── games resolve themselves, once the final score is trustworthy ─────────
def game_score_trusted(db, game):
    """A member posts the final instantly, so wait for a check: locked by the newsroom, confirmed by someone
    else, or left unchanged for 6 hours."""
    if game["status"] != "final" or game["our_score"] is None or game["their_score"] is None:
        return False
    if game["locked"]:
        return True
    if db.val("SELECT COUNT(*) FROM game_confirms WHERE game_id=?", (game["id"],)):
        return True
    reported = util.parse_iso(game["reported_at"] or game["updated_at"])
    return bool(reported) and datetime.now(timezone.utc) - reported >= timedelta(hours=6)


def settle_game(db, game):
    p = db.one("SELECT * FROM predictions WHERE game_id=? AND kind='score' AND status!='void' ORDER BY id DESC "
               "LIMIT 1", (game["id"],))
    if not p:
        return False
    answer = f"{game['our_score']}-{game['their_score']}"
    if game_score_trusted(db, game) and (p["status"] != "resolved" or p["answer"] != answer):
        resolve(db, decorate(db, p), answer)
        return True
    if p["status"] == "resolved" and game["status"] != "final":   # the newsroom undid the final
        _clear(db, p)
        db.run("UPDATE predictions SET status='open', answer=NULL WHERE id=?", (p["id"],))
    return False


def settle_due(db):
    """Worker: settle games whose final score has become trustworthy."""
    for g_ in db.q("SELECT g.* FROM games g JOIN predictions p ON p.game_id=g.id AND p.kind='score' "
                   "WHERE g.status='final' AND (p.status='open' OR p.answer != (g.our_score || '-' || g.their_score))"):
        settle_game(db, g_)


def waiting_for_answer(db):
    """Story predictions that have closed and need the newsroom to enter the real answer."""
    return db.q("SELECT p.*, s.headline, s.slug FROM predictions p JOIN stories s ON s.id=p.story_id "
                "WHERE p.status='open' AND p.closes_at <= ? ORDER BY p.closes_at", (now(),))
