#!/usr/bin/env python3
"""The Abundance Challenge — official Bluesky ticker bot.

One automated account (@abundancechallenge.ai), per SPEC-ticker-bot.md and
SPEC-zero-hour.md (both 2026-07-19, FINAL). Subcommands:

  post           post the daily line once (idempotent)
  mentions       reply to unanswered @-summons with the canonical line
  discover       witness-follow on-topic accounts (rate-limited, logged)
  digest         weekly GitHub issue listing new follows
  reality-check  annual data-day post + private "still true?" issue

Three states, one instrument (SPEC-zero-hour):
  countdown  now < 2030-12-31 23:59:59 Europe/Brussels  → "{n} days remain."
  overtime   after the deadline, not yet commenced      → "{n} days past the deadline."
  commenced  commenced.txt holds a date                 → one record post, then retire

The definition never changes in any state — it carries no date, so lateness
measures lateness and never expires the challenge. Everything the bot says is
canonical text; no generative step anywhere.

Standing caution: additive infrastructure only. This repo never touches the
site repo (abundance-challenge), AIC-WG-Tool, or the aicframework.net zone.
"""
import argparse
import datetime as dt
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests

PDS = "https://bsky.social"
BRUSSELS = ZoneInfo("Europe/Brussels")
DEADLINE = dt.datetime(2030, 12, 31, 23, 59, 59, tzinfo=BRUSSELS)
HASHTAG = "AbundanceChallenge"
SITE = "https://www.abundancechallenge.ai/"
MAX_GRAPHEMES = 300          # Bluesky hard limit; never truncate — fail loudly
TIMEOUT = 30
COMMENCED_FILE = "commenced.txt"

# The share card, attached to daily posts and the annual data day as an external
# embed (SPEC amendment 2026-07-19, Em-approved — supersedes the earlier
# text-only-daily-post rule; mentions replies stay text-only). Number-free, so
# it never goes stale. Title/description mirror the site's OG tags (canonical).
CARD_FILE = "abundance-challenge-card.png"
CARD_TITLE = "Abundance Will Not Distribute Itself"
CARD_DESC = ("To the builders and implementers of artificial intelligence: the "
             "universal abundance era commences on the day extreme poverty ends "
             "— everywhere on the planet. Whatever abundance turns out to mean, "
             "that is its opening day. It has a deadline.")

# The definition — identical across countdown and overtime (carries no date).
DEFINITION = ("The universal abundance era commences on the day extreme poverty "
              "ends — everywhere on the planet. Whatever abundance turns out to "
              "mean, that is its opening day.")

# Annual reality check (SPEC-zero-hour "Against forgetting").
SDG_REPORT = "https://unstats.un.org/sdgs/report/{year}/"
SDG_FALLBACK = "https://unstats.un.org/sdgs/"
WORLD_BANK = "https://pip.worldbank.org/"

# Witness-following limits (SPEC-ticker-bot "Witness following").
MAX_FOLLOWS_PER_DAY = 10
FOLLOW_CEILING = 500
MIN_ON_TOPIC_POSTS = 3
MIN_ACCOUNT_AGE_DAYS = 60
MIN_FOLLOWERS = 50
MIN_POSTS = 50
BIO_EXCLUSIONS = ("parody", "fan account")
ADULT_LABELS = {"porn", "sexual", "nudity", "nsfw", "sexual-figurative"}

# Reply-on-summons limits (SPEC-ticker-bot "Reply on summons").
MAX_REPLIES_PER_DAY = 30
NOTIF_PAGE_CAP = 8           # bounded pagination: up to 8×50 = 400 notifications
NOTIF_LOOKBACK_DAYS = 3      # stop paging once notifications predate this window

# Always-on loop (public-repo Actions; unlimited free minutes). One run polls
# for LOOP_DURATION_SECONDS then exits so a queued watchdog run takes over.
# Overridable via env for testing.
LOOP_DURATION_SECONDS = int(os.environ.get("LOOP_DURATION_SECONDS", "3180"))  # ~53 min
LOOP_INTERVAL_SECONDS = int(os.environ.get("LOOP_INTERVAL_SECONDS", "60"))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def die(msg):
    """Fail loudly: print to stderr and exit non-zero (Actions email fires)."""
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(1)


def now_utc_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(s):
    """Tolerant ISO-8601 parser (handles 'Z' and variable fractional digits;
    works on Python 3.9 locally and 3.11 in Actions)."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"(.*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
        if not m:
            return None
        base, frac, tz = m.groups()
        frac = (frac or "0")[:6].ljust(6, "0")
        try:
            return dt.datetime.fromisoformat("{}.{}{}".format(base, frac, tz))
        except ValueError:
            return None


def brussels_date(iso):
    ts = parse_ts(iso)
    return ts.astimezone(BRUSSELS).date() if ts else None


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------- #
# state — countdown / overtime / commenced (SPEC-zero-hour)
# --------------------------------------------------------------------------- #
def day_count(now=None):
    """Whole days remaining (signed): positive before the deadline, negative
    after. DST-proof — never subtract calendar dates."""
    now = now or dt.datetime.now(BRUSSELS)
    return int((DEADLINE - now).total_seconds() // 86400)


def read_commenced():
    """Return the commencement date string (YYYY-MM-DD) if commenced.txt holds a
    valid date, else None. The human switch — backdated to the measured day."""
    for line in read_lines(COMMENCED_FILE):
        try:
            dt.date.fromisoformat(line)
            return line
        except ValueError:
            continue
    return None


def current_state(now=None):
    """Return (kind, value): ('commenced', 'YYYY-MM-DD') |
    ('countdown', days_remaining) | ('overtime', days_past)."""
    now = now or dt.datetime.now(BRUSSELS)
    commenced = read_commenced()
    if commenced:
        return ("commenced", commenced)
    if now < DEADLINE:
        return ("countdown", int((DEADLINE - now).total_seconds() // 86400))
    return ("overtime", int((now - DEADLINE).total_seconds() // 86400))


# --------------------------------------------------------------------------- #
# canonical text + richtext facets (byte offsets on UTF-8, not char offsets)
# --------------------------------------------------------------------------- #
def byte_span(text, sub):
    i = text.index(sub)
    start = len(text[:i].encode("utf-8"))
    return start, start + len(sub.encode("utf-8"))


def link(uri):
    return {"$type": "app.bsky.richtext.facet#link", "uri": uri}


def tag(name):
    return {"$type": "app.bsky.richtext.facet#tag", "tag": name}


def facets_for(text, specs):
    """specs: list of (substring, feature_dict). Byte-aligned, sorted."""
    out = []
    for sub, feature in specs:
        s, e = byte_span(text, sub)
        out.append({"index": {"byteStart": s, "byteEnd": e}, "features": [feature]})
    out.sort(key=lambda f: f["index"]["byteStart"])
    return out


def guard_length(text):
    if len(text) > MAX_GRAPHEMES:
        die("Composed text is {} graphemes (>{}); refusing to truncate."
            .format(len(text), MAX_GRAPHEMES))
    return text


def daily_payload(state):
    """(text, facets, prefix) for countdown/overtime states."""
    kind, value = state
    if kind == "countdown":
        url = "{}?d={}".format(SITE, value)
        prefix = "{:,} days remain".format(value)
    else:  # overtime
        url = "{}?d=-{}".format(SITE, value)
        prefix = "{:,} days past the deadline".format(value)
    text = "{}. {} {}\n\n#{}".format(prefix, DEFINITION, url, HASHTAG)
    guard_length(text)
    facets = facets_for(text, [(url, link(url)), ("#" + HASHTAG, tag(HASHTAG))])
    return text, facets, prefix


def commencement_payload(date):
    """(text, facets) for the one-time commencement post / record line."""
    text = "The universal abundance era commenced on {}. abundancechallenge.ai #{}".format(
        date, HASHTAG)
    guard_length(text)
    facets = facets_for(text, [("abundancechallenge.ai", link(SITE)),
                               ("#" + HASHTAG, tag(HASHTAG))])
    return text, facets


def spoken_line(state):
    """What the bot says right now (daily line, or the record line if commenced)."""
    if state[0] == "commenced":
        return commencement_payload(state[1])
    text, facets, _ = daily_payload(state)
    return text, facets


# --------------------------------------------------------------------------- #
# AT Protocol client
# --------------------------------------------------------------------------- #
def session():
    handle = os.environ.get("BSKY_HANDLE")
    password = os.environ.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        die("BSKY_HANDLE / BSKY_APP_PASSWORD not set in environment.")
    r = requests.post("{}/xrpc/com.atproto.server.createSession".format(PDS),
                      json={"identifier": handle, "password": password}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return d["accessJwt"], d["did"]


def api_get(token, method, params):
    r = requests.get("{}/xrpc/{}".format(PDS, method), params=params,
                     headers={"Authorization": "Bearer {}".format(token)}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def api_post(token, method, body):
    r = requests.post("{}/xrpc/{}".format(PDS, method), json=body,
                      headers={"Authorization": "Bearer {}".format(token)}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def create_post(token, did, text, facets, reply=None, embed=None):
    record = {"$type": "app.bsky.feed.post", "text": text,
              "createdAt": now_utc_iso(), "facets": facets, "langs": ["en"]}
    if reply:
        record["reply"] = reply
    if embed:
        record["embed"] = embed
    return api_post(token, "com.atproto.repo.createRecord",
                    {"repo": did, "collection": "app.bsky.feed.post", "record": record})


def upload_blob(token, path, mime):
    if not os.path.exists(path):
        die("Card file {} missing; cannot attach embed.".format(path))
    with open(path, "rb") as fh:
        data = fh.read()
    r = requests.post("{}/xrpc/com.atproto.repo.uploadBlob".format(PDS), data=data,
                      headers={"Authorization": "Bearer {}".format(token),
                               "Content-Type": mime}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["blob"]


def card_embed(token):
    """app.bsky.embed.external carrying the number-free card as an uploaded thumb.
    Attached to daily posts and the annual data day; never to mention replies."""
    return {
        "$type": "app.bsky.embed.external",
        "external": {"uri": SITE, "title": CARD_TITLE, "description": CARD_DESC,
                     "thumb": upload_blob(token, CARD_FILE, "image/png")},
    }


def author_feed(token, did, limit=30, filt=None):
    params = {"actor": did, "limit": limit}
    if filt:
        params["filter"] = filt
    return api_get(token, "app.bsky.feed.getAuthorFeed", params).get("feed", [])


def feed_has_prefix_today(token, did, prefix):
    today = dt.datetime.now(BRUSSELS).date()
    for item in author_feed(token, did, limit=10):
        rec = item.get("post", {}).get("record", {})
        if rec.get("text", "").startswith(prefix) and \
                brussels_date(rec.get("createdAt", "")) == today:
            return True
    return False


def feed_has_prefix_ever(token, did, prefix, year=None):
    for item in author_feed(token, did, limit=30):
        rec = item.get("post", {}).get("record", {})
        if not rec.get("text", "").startswith(prefix):
            continue
        if year is None:
            return True
        d = brussels_date(rec.get("createdAt", ""))
        if d and d.year == year:
            return True
    return False


# --------------------------------------------------------------------------- #
# post — the daily line (state-aware)
# --------------------------------------------------------------------------- #
def cmd_post(args):
    state = current_state()

    if state[0] == "commenced":
        text, facets = commencement_payload(state[1])
        if args.dry_run:
            print("[dry-run] commenced; would post record line once (+card):\n" + text)
            return
        token, did = session()
        if feed_has_prefix_ever(token, did, "The universal abundance era commenced on"):
            print("Commenced; commencement post already made. Daily posts retired.")
            return
        res = create_post(token, did, text, facets, embed=card_embed(token))
        print("Posted commencement record: {}".format(res.get("uri")))
        return

    text, facets, prefix = daily_payload(state)
    if args.dry_run:
        print("[dry-run] {} state; would post ({} graphemes, +card):\n{}"
              .format(state[0], len(text), text))
        return
    token, did = session()
    if feed_has_prefix_today(token, did, prefix):
        print("Today's line already posted ({}); exiting clean.".format(prefix))
        return
    res = create_post(token, did, text, facets, embed=card_embed(token))
    print("Posted [{}]: {}".format(prefix, res.get("uri")))


# --------------------------------------------------------------------------- #
# mentions — reply on summons (state-aware)
# --------------------------------------------------------------------------- #
def own_reply_exists(token, did, uri):
    try:
        th = api_get(token, "app.bsky.feed.getPostThread", {"uri": uri, "depth": 1})
    except requests.HTTPError:
        # Can't verify (transient error, or the post was deleted): skip this
        # round rather than risk a double reply. The loop retries next poll.
        return True
    for r in th.get("thread", {}).get("replies", []) or []:
        if r.get("post", {}).get("author", {}).get("did") == did:
            return True
    return False


def count_todays_replies(token, did):
    today = dt.datetime.now(BRUSSELS).date()
    n = 0
    for item in author_feed(token, did, limit=50, filt="posts_with_replies"):
        post = item.get("post", {})
        rec = post.get("record", {})
        if rec.get("reply") and post.get("author", {}).get("did") == did \
                and brussels_date(rec.get("createdAt", "")) == today:
            n += 1
    return n


def is_summons(notif, did):
    """True if this notification is a summons — the bot @-mentioned in a post.

    Bluesky files a standalone/third-party mention as reason 'mention', but a
    reply to the bot's OWN post that also @-mentions it as reason 'reply'. The
    old reason=='mention'-only filter silently dropped the latter. Replies with
    no mention facet, quote-posts, likes and follows carry no bot mention and
    are ignored, per spec ("no other trigger exists")."""
    reason = notif.get("reason")
    if reason == "mention":
        return True
    if reason == "reply":
        for facet in (notif.get("record") or {}).get("facets") or []:
            for feat in facet.get("features") or []:
                if feat.get("$type") == "app.bsky.richtext.facet#mention" \
                        and feat.get("did") == did:
                    return True
    return False


def gather_summons(token, did):
    """Bounded pagination over notifications so a summons buried under newer
    likes/follows/reposts is still found (a single limit=50 page missed them).
    Caps at NOTIF_PAGE_CAP pages and stops once notifications predate the
    lookback window."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=NOTIF_LOOKBACK_DAYS)
    cursor, pages, summons = None, 0, []
    while pages < NOTIF_PAGE_CAP:
        params = {"limit": 50}
        if cursor:
            params["cursor"] = cursor
        res = api_get(token, "app.bsky.notification.listNotifications", params)
        notifs = res.get("notifications", [])
        if not notifs:
            break
        summons.extend(n for n in notifs if is_summons(n, did))
        cursor = res.get("cursor")
        pages += 1
        oldest = parse_ts(notifs[-1].get("indexedAt"))
        if not cursor or (oldest and oldest < cutoff):
            break
    return summons


def poll_once(token, did, dry_run=False):
    """One mentions pass. Returns (summons_found, replies_made, cap_hit).
    Idempotent (skips threads already answered), 1 reply/thread, canonical
    text-only reply. Shared by the single-shot `mentions` and the `loop`."""
    text, facets = spoken_line(current_state())
    summons = gather_summons(token, did)
    todays = count_todays_replies(token, did)
    replied_roots, made, cap_hit = set(), 0, False

    for n in summons:
        uri, cid = n.get("uri"), n.get("cid")
        reply_ref = (n.get("record") or {}).get("reply") or {}
        root = reply_ref.get("root") or {"uri": uri, "cid": cid}
        if root.get("uri") in replied_roots:            # max 1 reply per thread
            continue
        if own_reply_exists(token, did, uri):            # idempotent
            replied_roots.add(root.get("uri"))
            continue
        if todays + made >= MAX_REPLIES_PER_DAY:
            cap_hit = True
            break
        if dry_run:
            print("[dry-run] would reply to {}".format(uri))
        else:
            create_post(token, did, text, facets,
                        reply={"root": root, "parent": {"uri": uri, "cid": cid}})
            print("Replied to summons: {}".format(uri))
        replied_roots.add(root.get("uri"))
        made += 1
    return len(summons), made, cap_hit


def cmd_mentions(args):
    if args.dry_run and not (os.environ.get("BSKY_HANDLE")
                             and os.environ.get("BSKY_APP_PASSWORD")):
        text, _ = spoken_line(current_state())
        print("[dry-run] would reply with:\n" + text)
        return
    token, did = session()
    found, made, cap_hit = poll_once(token, did, args.dry_run)
    print("Summons found: {}; new repl{}: {}."
          .format(found, "y" if made == 1 else "ies", made))
    if cap_hit:
        die("Daily reply cap ({}) reached; failing loudly rather than flood."
            .format(MAX_REPLIES_PER_DAY))


def cmd_loop(args):
    """Always-on poller: poll every LOOP_INTERVAL_SECONDS for
    LOOP_DURATION_SECONDS, then exit 0 so a queued watchdog run takes over.
    Resilient — a transient error is logged and the loop continues (session
    refreshed); only a hard misconfig (missing creds) exits non-zero."""
    end = time.monotonic() + LOOP_DURATION_SECONDS
    print("Mentions loop: polling every {}s for ~{}s."
          .format(LOOP_INTERVAL_SECONDS, LOOP_DURATION_SECONDS))
    token = did = None
    polls = total = 0
    while time.monotonic() < end:
        t0 = time.monotonic()
        try:
            if token is None:
                token, did = session()
            found, made, cap_hit = poll_once(token, did)
            total += made
            if made:
                print("  poll {}: {} summons, {} replied.".format(polls + 1, found, made))
            if cap_hit:
                print("  daily reply cap reached; pausing replies until tomorrow.",
                      file=sys.stderr)
        except Exception as e:               # noqa: BLE001 — resilient by design
            print("  poll error (continuing): {!r}".format(e), file=sys.stderr)
            token = None                     # force a fresh session next iteration
        polls += 1
        nap = max(0, LOOP_INTERVAL_SECONDS - (time.monotonic() - t0))
        if time.monotonic() + nap >= end:
            break
        time.sleep(nap)
    print("Loop window done: {} polls, {} replies. Exiting for handoff."
          .format(polls, total))


# --------------------------------------------------------------------------- #
# discover — witness following
# --------------------------------------------------------------------------- #
def get_following(token, did):
    dids, cursor = set(), None
    while True:
        params = {"actor": did, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        res = api_get(token, "app.bsky.graph.getFollows", params)
        follows = res.get("follows", [])
        for f in follows:
            dids.add(f.get("did"))
        cursor = res.get("cursor")
        if not cursor or not follows:
            break
    return dids


def qualifies(p):
    age = parse_ts(p.get("createdAt"))
    if not age or (dt.datetime.now(dt.timezone.utc) - age).days < MIN_ACCOUNT_AGE_DAYS:
        return False
    if p.get("followersCount", 0) < MIN_FOLLOWERS:
        return False
    if p.get("postsCount", 0) < MIN_POSTS:
        return False
    bio = (p.get("description") or "").lower()
    if any(bad in bio for bad in BIO_EXCLUSIONS):
        return False
    for lab in p.get("labels", []) or []:
        if lab.get("val") in ADULT_LABELS:
            return False
    return True


def follow_actor(token, did, subject):
    return api_post(token, "com.atproto.repo.createRecord", {
        "repo": did, "collection": "app.bsky.graph.follow",
        "record": {"$type": "app.bsky.graph.follow",
                   "subject": subject, "createdAt": now_utc_iso()}})


def cmd_discover(args):
    existing = len(read_lines("follows.log"))
    if existing >= FOLLOW_CEILING:
        die("Follow ceiling ({}) reached; discovery paused for Em's review."
            .format(FOLLOW_CEILING))

    token, did = session()
    queries = read_lines("queries.txt")
    never = set(read_lines("never-again.txt"))
    following = get_following(token, did)

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    author_posts, author_handle = {}, {}
    for q in queries:
        try:
            res = api_get(token, "app.bsky.feed.searchPosts",
                          {"q": q, "sort": "latest", "limit": 100})
        except requests.HTTPError as e:
            print("search failed for {!r}: {}".format(q, e))
            continue
        for post in res.get("posts", []):
            author = post.get("author", {})
            adid = author.get("did")
            created = parse_ts((post.get("record") or {}).get("createdAt"))
            if not adid or not created or created < cutoff:
                continue
            author_posts.setdefault(adid, set()).add(post.get("uri"))
            author_handle[adid] = author.get("handle")

    candidates = [
        d for d, posts in author_posts.items()
        if len(posts) >= MIN_ON_TOPIC_POSTS and d != did and d not in following
        and d not in never and author_handle.get(d) not in never
    ]

    qualified = []
    for batch in chunk(candidates, 25):
        profiles = api_get(token, "app.bsky.actor.getProfiles",
                           {"actors": batch}).get("profiles", [])
        qualified.extend(p for p in profiles if qualifies(p))

    room = min(MAX_FOLLOWS_PER_DAY, FOLLOW_CEILING - existing)
    to_follow = qualified[:room]

    if args.dry_run:
        print("[dry-run] {} candidate author(s) with >={} on-topic posts; {} qualified; "
              "would follow {} (cap {}/day, {} logged of {}):"
              .format(len(candidates), MIN_ON_TOPIC_POSTS, len(qualified),
                      len(to_follow), MAX_FOLLOWS_PER_DAY, existing, FOLLOW_CEILING))
        for p in to_follow:
            print("  @{}  ({} followers, {} posts)".format(
                p.get("handle"), p.get("followersCount"), p.get("postsCount")))
        return

    followed = []
    for p in to_follow:
        follow_actor(token, did, p["did"])
        followed.append("{} {} {}".format(
            dt.date.today().isoformat(), p.get("handle"), p["did"]))
    if followed:
        with open("follows.log", "a", encoding="utf-8") as f:
            f.write("\n".join(followed) + "\n")
    print("Followed {} account(s).".format(len(followed)))
    if existing + len(followed) >= FOLLOW_CEILING:
        die("Follow ceiling ({}) reached after this batch; pausing for review."
            .format(FOLLOW_CEILING))


# --------------------------------------------------------------------------- #
# digest — weekly GitHub issue of new follows
# --------------------------------------------------------------------------- #
def open_issue(title, body):
    repo = os.environ.get("GITHUB_REPOSITORY")
    tok = os.environ.get("GITHUB_TOKEN")
    if not repo or not tok:
        die("GITHUB_REPOSITORY / GITHUB_TOKEN not set (Actions provides both).")
    r = requests.post("https://api.github.com/repos/{}/issues".format(repo),
                      headers={"Authorization": "Bearer {}".format(tok),
                               "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": body}, timeout=TIMEOUT)
    r.raise_for_status()
    print("Opened issue:", r.json().get("html_url"))


def cmd_digest(args):
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    entries = []
    for line in read_lines("follows.log"):
        parts = line.split()
        if len(parts) >= 3:
            try:
                d = dt.date.fromisoformat(parts[0])
            except ValueError:
                continue
            if d >= week_ago:
                entries.append(line)

    title = "Follow digest — week of {}".format(week_ago.isoformat())
    body = (
        "New witness-follows this week (SPEC-ticker-bot, witness following):\n\n"
        + ("\n".join("- `{}`".format(e) for e in entries) if entries
           else "_No new follows this week._")
        + "\n\nTo prune any: unfollow in the Bluesky app **and** add the handle "
          "to `never-again.txt` (so discovery never re-follows it)."
        + "\n\nStanding check: if the commencement condition is met, "
          "see SPEC-zero-hour.md."
    )
    if args.dry_run:
        print(title)
        print(body)
        return
    open_issue(title, body)


# --------------------------------------------------------------------------- #
# reality-check — annual data day (SPEC-zero-hour "Against forgetting")
# --------------------------------------------------------------------------- #
REALITY_ISSUE_TITLE = "Annual reality check — {year}"
REALITY_ISSUE_BODY = (
    "Annual check: has extreme poverty ended everywhere? Is the page still "
    "telling the truth? If the era has commenced: SPEC-zero-hour.md, section 3."
)


def http_ok(url):
    try:
        return requests.get(url, timeout=TIMEOUT, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def reality_payload(state, year, sdg_url):
    kind, value = state
    if kind == "countdown":
        count_phrase = "{:,} days remain".format(value)
    else:  # overtime
        count_phrase = "{:,} days past the deadline".format(value)
    text = (
        "Annual data day. {}. The official measure of extreme poverty: the UN "
        "SDG progress report and the World Bank poverty data. The universal "
        "abundance era commences on the day the measure reads zero — everywhere "
        "on the planet. abundancechallenge.ai #{}"
    ).format(count_phrase, HASHTAG)
    guard_length(text)
    facets = facets_for(text, [
        ("UN SDG progress report", link(sdg_url)),
        ("World Bank poverty data", link(WORLD_BANK)),
        ("abundancechallenge.ai", link(SITE)),
        ("#" + HASHTAG, tag(HASHTAG)),
    ])
    return text, facets


def cmd_reality_check(args):
    now = dt.datetime.now(BRUSSELS)
    state = current_state(now)
    if state[0] == "commenced":
        print("Era commenced; annual reality-check retired (the record stands).")
        return

    year = now.year
    year_url = SDG_REPORT.format(year=year)
    sdg_url = year_url
    if not args.dry_run:
        if not http_ok(year_url):
            sdg_url = SDG_FALLBACK
            if not http_ok(SDG_FALLBACK):
                die("SDG report URL and fallback both unreachable; not posting.")
        if not http_ok(WORLD_BANK):
            die("World Bank data URL unreachable; refusing a broken data-day post.")

    text, facets = reality_payload(state, year, sdg_url)
    if args.dry_run:
        print("[dry-run] would post ({} graphemes, +card):\n{}".format(len(text), text))
        print("[dry-run] would open issue:", REALITY_ISSUE_TITLE.format(year=year))
        return

    token, did = session()
    if feed_has_prefix_ever(token, did, "Annual data day.", year=year):
        print("Annual data day already posted this year; skipping post and issue.")
        return
    res = create_post(token, did, text, facets, embed=card_embed(token))
    print("Posted annual data day: {}".format(res.get("uri")))
    open_issue(REALITY_ISSUE_TITLE.format(year=year), REALITY_ISSUE_BODY)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="Abundance Challenge ticker bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    commands = (
        ("post", cmd_post), ("mentions", cmd_mentions), ("loop", cmd_loop),
        ("discover", cmd_discover), ("digest", cmd_digest),
        ("reality-check", cmd_reality_check),
    )
    for name, fn in commands:
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true",
                       help="compute and print actions without posting/following")
        p.set_defaults(func=fn)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
