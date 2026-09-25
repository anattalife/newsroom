"""Calling Claude: key, spending cap, cost tracking. The prompts live in writing.py."""
import logging
import re
from datetime import datetime, timezone

import requests

from . import settings, util
from .db import loads, now
from .security import get_secret

log = logging.getLogger("newsroom.ai")

class AIUnavailable(Exception):
    pass


def month_spend(db):
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return round(db.val("SELECT COALESCE(SUM(cost),0) FROM ai_usage WHERE at >= ?", (start,)) or 0, 2)


def status(db):
    """(ready, reason) for the dashboard."""
    if not get_secret(db, "setting:ai_key"):
        return False, "No Claude API key yet. Add one in Settings → AI writing."
    if settings.get(db, "ai_paused"):
        return False, "The AI is paused in Settings → AI writing."
    cap = float(settings.get(db, "ai_cap") or 0)
    if cap and month_spend(db) >= cap:
        return False, f"This month's AI spending cap (${cap:g}) has been reached."
    return True, ""


# (input, output) dollars per million tokens, for estimates; the main model uses the prices in Settings.
# Check current prices at anthropic.com/pricing.
CHEAP_PRICES = {"claude-haiku-4-5-20251001": (1.0, 5.0)}
SEARCH_PRICE = 0.01  # dollars per web search (check current prices at anthropic.com/pricing)


def prices(db, model):
    if model in CHEAP_PRICES and model != settings.get(db, "ai_model"):
        return CHEAP_PRICES[model]
    return float(settings.get(db, "ai_price_in") or 0), float(settings.get(db, "ai_price_out") or 0)


def story_cost(db, sid):
    return round(db.val("SELECT COALESCE(SUM(cost),0) FROM ai_usage WHERE story_id=?", (sid,)) or 0, 3)


def _post(db, body):
    try:
        return requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": get_secret(db, "setting:ai_key"), "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body, timeout=300)
    except requests.RequestException as e:
        raise AIUnavailable(f"Couldn't reach the Claude API: {type(e).__name__}") from e


def search_tool(db, max_uses):
    tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": int(max_uses)}
    town, state = settings.get(db, "town"), settings.get(db, "state")
    if town:
        tool["user_location"] = {"type": "approximate", "city": town, "country": "US",
                                 "timezone": settings.get(db, "timezone") or "America/Chicago",
                                 **({"region": state} if state else {})}
    return tool


def call_claude(db, system, user, purpose="draft", max_tokens=8000, web_search=0, model=None, story_id=None):
    """One request to Claude. web_search > 0 lets it search the web up to that many times.
    model overrides the main model (research and fact-checks use the cheaper research model)."""
    ready, reason = status(db)
    if not ready:
        raise AIUnavailable(reason)
    main = settings.get(db, "ai_model")
    model = model or main
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    if web_search:
        body["tools"] = [search_tool(db, web_search)]
    texts, used = [], {"input_tokens": 0, "output_tokens": 0, "searches": 0}
    for _ in range(4):  # a long search turn can pause; send it back to continue
        r = _post(db, body)
        if r.status_code in (400, 404) and body["model"] != main and "web search" not in r.text.lower():
            log.warning("research model %s refused (%s); using %s", body["model"], r.status_code, main)
            body["model"] = model = main  # the cheaper model isn't available on this account
            continue
        if r.status_code == 400 and web_search and "web search" in r.text.lower():
            settings.put(db, "web_search_problem", "Web search is turned off for your Claude account. Turn it on "
                                                   "in the Claude Console → Settings → Privacy.")
            body.pop("tools", None)
            web_search = 0
            continue
        if r.status_code == 401:
            raise AIUnavailable("The Claude API key was rejected. Check it in Settings → AI writing.")
        if r.status_code >= 400:
            raise AIUnavailable(f"Claude API error {r.status_code}: {r.text[:200]}")
        data = r.json()
        u = data.get("usage", {})
        used["input_tokens"] += u.get("input_tokens", 0)
        used["output_tokens"] += u.get("output_tokens", 0)
        used["searches"] += (u.get("server_tool_use") or {}).get("web_search_requests", 0)
        texts.append("".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"))
        if data.get("stop_reason") != "pause_turn":
            break
        body["messages"] = body["messages"] + [{"role": "assistant", "content": data.get("content", [])}]
    if web_search and used["searches"]:
        settings.put(db, "web_search_problem", "")
    p_in, p_out = prices(db, model)
    cost = (used["input_tokens"] * p_in + used["output_tokens"] * p_out) / 1_000_000 + used["searches"] * SEARCH_PRICE
    db.insert("ai_usage", at=now(), model=model, input_tokens=used["input_tokens"],
              output_tokens=used["output_tokens"], cost=round(cost, 5), purpose=purpose, story_id=story_id,
              searches=used["searches"])
    return "\n".join(texts)


def item_trust(item):
    return (loads(item.get("extra"), {}) or {}).get("trust") or item.get("source_trust") or "tip"


COPY_RUN = 8  # this many identical words in a row counts as copying


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def copied_passages(draft_html, sources, run=COPY_RUN):
    """Passages of the draft that repeat a source word-for-word (outside quotation marks)."""
    draft = re.sub(r"[\u201c\"][^\u201d\"]{0,600}[\u201d\"]", " ", util.text_only(draft_html))  # quotes are allowed
    dw = _words(draft)
    grams = set()
    for src in sources:
        sw = _words(src)
        grams.update(tuple(sw[i:i + run]) for i in range(len(sw) - run + 1))
    hits, i = [], 0
    while i <= len(dw) - run:
        if tuple(dw[i:i + run]) in grams:
            j = i + run
            while j < len(dw) and tuple(dw[j - run + 1:j + 1]) in grams:
                j += 1
            hits.append(" ".join(dw[i:j]))
            i = j
        else:
            i += 1
    return hits
