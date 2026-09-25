# Local Newsroom

A self-contained local news platform with a community built in. It works like this:

- It collects headlines from your sources for free.
- You pick what to develop. The AI tells you what it knows and asks what's missing, then writes the article with your answers.
- Readers join free to send tips and stories, upvote, comment and earn points and badges.
- Everything publishes to its own website and installable phone app, and on to your social accounts.

**To put it online, follow [INSTALL-AWS.md](INSTALL-AWS.md).** **To connect Facebook, Instagram, Threads, Bluesky and X, follow [SOCIAL-SETUP.md](SOCIAL-SETUP.md).**

## Version 2.0: what's new

| Area | What works |
|---|---|
| Just write it | One click: the AI reads the item, researches the missing facts on the web (with sources), and writes the draft for you to check. |
| Develop this story | Replaces Write story, on every source, on tips, on Manual add (text or a link), and on members' submissions. **Step 1:** the AI reads everything, including the links, researches on the web, and answers its own questions with sources. It only asks you what it couldn't find. **Step 2:** you answer, paste text and add details. **Step 3:** **Write the article**. Save part-way as **In development**. The AI never refuses, skips or judges what you chose: it writes even with questions unanswered, and never guesses the missing parts. Optional **Check facts after writing** flags anything the material doesn't support, highlighted in yellow. |
| Writing instructions | Structure, style, banned phrases, headline rules, 11 story-type templates and up to 5 style examples, all editable in Settings → Writing instructions. The locked accuracy, privacy and always-write rules are shown there but can't be edited. |
| Members | Free accounts on the public site only: no dashboard, no sources, no AI. **Write it myself** (editor plus writing guide) or **Just the facts**. Everything waits for you unless you mark a member trusted. Your options: Publish, Edit then publish (adds "Edited by the newsroom"), Develop, Send back with a note, Decline. Credit is chosen on every submission. Photos need a permission tick and are credited. Guests can still send tips, and their tips move into the account they create. |
| Community | Featured stories on the homepage and category pages; **All articles** and **Top** pages. Upvotes, flags and threaded comments, with a new member's first comments held for you. Points, 21 badges (editable), a leaderboard and privileges that unlock with points. Warn, hold, suspend or ban. Staff get public profiles with a Staff badge. |
| Categories and partners | 18 community categories. Organizations apply free to be **community partners**: they get a badge, their logo on their category page, and their name on what they send. |
| National and world | Your own developed stories tagged National or World, plus a **Wire service** source type that publishes licensed wire copy as written, with no AI. National, World and combined pages, a homepage box, and optional mixing into the main list. |
| Where to share | Really posts to Facebook Page, Instagram, Threads, Bluesky and X. You choose platforms and text per story; the story shows Posted (with link) or Failed (with Try again). |
| Directory and specials | Free business directory: import from OpenStreetMap, members add missing businesses, owners claim theirs, reviews are moderated. Owners post daily specials for free, and they show on the Specials page and the homepage. |

Everything from Stage 1 still works: sources, per-source approval pages, scheduling, corrections, tips, weather, backups and the phone app.

## How it's built

- **Python + Flask** web app (`app/`), **SQLite** database, all in one `data/` folder.
- A separate **worker** process (`python -m app worker`) checks sources, drafts, publishes scheduled stories and makes backups.
- **Docker Compose** runs the web app, the worker and **Caddy** (automatic HTTPS).

```
app/
  __init__.py      app setup, security headers, login loading
  db.py            database schema and helpers
  security.py      passwords, roles, CSRF, encryption, rate limits
  settings.py      every setting, described once (the Settings pages are generated from this)
  sources/         one file per source type + registry
  ai.py            calling Claude: key, spending cap, cost tracking, copying check
  writing.py       Develop this story, Write the article, fact-check (all the AI instructions)
  writing_defaults.py  the editable default writing instructions
  pipeline.py      items → development → drafts → published stories
  community.py     points, badges, privileges, notices, votes, comment threads
  social.py        posting to Facebook, Instagram, Threads, Bluesky and X
  directory.py     business directory, OpenStreetMap import, specials
  weather.py       weather data, caching and alerts
  worker.py        background loop, notifications, backups
  graphics.py      headline graphics and app icons
  views/admin.py   the dashboard
  views/admin_community.py  dashboard pages for members, moderation, badges, partners, social, directory
  views/public.py  the website and app
  views/members.py member accounts, submissions, comments, votes, flags, partners
  views/directory.py  the public directory and specials
deploy/            Caddy config, installer, updater
tests/             end-to-end tests (no network or API key needed)
```

## Running it on your own computer (optional)

```
pip install -r requirements.txt
python -m app serve      # website + dashboard on http://localhost:8000
python -m app worker     # in a second terminal
python -m unittest discover tests
```
