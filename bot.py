#!/usr/bin/env python3
"""The Abundance Challenge — official Bluesky ticker bot.

One automated account (@abundancechallenge.ai), three behaviours, per
SPEC-ticker-bot.md (2026-07-19, FINAL):

  post      post the daily day-count once (idempotent)
  mentions  reply to unanswered @-summons with the canonical line
  discover  witness-follow on-topic accounts (rate-limited, logged)
  digest    weekly GitHub issue listing new follows

AT Protocol over plain HTTPS against https://bsky.social. Standard library
plus `requests` only. Everything the bot says is canonical text — no
generative step anywhere.

Standing caution: additive infrastructure only. This repo never touches the
site repo (abundance-challenge), AIC-WG-Tool, or the aicframework.net zone.
"""
import argparse
import datetime as dt
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests

PDS = "https://bsky.social"
BRUSSELS = ZoneInfo("Europe/Brussels")
DEADLINE = dt.datetime(2030, 12, 31, 23, 59, 59, tzinfo=BRUSSELS)
HASHTAG = "AbundanceChallenge"
MAX_GRAPHEMES = 300          # Bluesky hard limit; never truncate — fail loudly
TIMEOUT = 30

# Witness-following limits (SPEC "Witness following")
MAX_FOLLOWS_PER_DAY = 10
FOLLOW_CEILING = 500         # discovery pauses for Em's review here
MIN_ON_TOPIC_POSTS = 3       # distinct on-topic posts within 30 days
MIN_ACCOUNT_AGE_DAYS = 60
MIN_FOLLOWERS = 50
MIN_POSTS = 50
BIO_EXCLUSIONS = ("parody", "fan account")
ADULT_LABELS = {"porn", "sexual", "nudity", "nsfw", "sexual-figurative"}

# Reply-on-summons limits (SPEC "Reply on summons")
MAX_REPLIES_PER_DAY = 30


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
    works on both Python 3.9 locally and 3.11 in Actions)."""
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
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------- #
# day count — canonical, DST-proof (never subtract calendar dates)
# --------------------------------------------------------------------------- #
def day_count(now=None):
    now = now or dt.datetime.now(BRUSSELS)
    return int((DEADLINE - now).total_seconds() // 86400)


# --------------------------------------------------------------------------- #
# canonical text + richtext facets (byte offsets on UTF-8, not char offsets)
# --------------------------------------------------------------------------- #
def compose(count):
    """Return (text, url). Display count is comma-grouped; the ?d= URL carries
    the raw integer (self-busting fresh URL, same pattern as the site panel)."""
    url = "https://www.abundancechallenge.ai/?d={}".format(count)
    text = (
        "{:,} days remain. The universal abundance era commences on the day "
        "extreme poverty ends — everywhere on the planet. Whatever abundance "
        "turns out to mean, that is its opening day. {}\n\n#{}"
    ).format(count, url, HASHTAG)
    return text, url


def byte_span(text, sub):
    i = text.index(sub)
    start = len(text[:i].encode("utf-8"))
    return start, start + len(sub.encode("utf-8"))


def build_facets(text, url):
    facets = []
    s, e = byte_span(text, url)
    facets.append({
        "index": {"byteStart": s, "byteEnd": e},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
    })
    tag = "#" + HASHTAG
    s, e = byte_span(text, tag)
    facets.append({
        "index": {"byteStart": s, "byteEnd": e},
        "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": HASHTAG}],
    })
    return facets


def canonical_payload():
    """Compose today's text with the zero-hour and 300-grapheme guards applied.
    Returns (count, text, facets) or exits non-zero."""
    count = day_count()
    if count <= 0:
        die("Zero-hour interim rule: count={} — post/reply nothing.".format(count))
    text, url = compose(count)
    if len(text) > MAX_GRAPHEMES:
        die("Composed text is {} graphemes (>{}); refusing to truncate."
            .format(len(text), MAX_GRAPHEMES))
    return count, text, build_facets(text, url)


# --------------------------------------------------------------------------- #
# AT Protocol client
# --------------------------------------------------------------------------- #
def session():
    handle = os.environ.get("BSKY_HANDLE")
    password = os.environ.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        die("BSKY_HANDLE / BSKY_APP_PASSWORD not set in environment.")
    r = requests.post(
        "{}/xrpc/com.atproto.server.createSession".format(PDS),
        json={"identifier": handle, "password": password}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    d = r.json()
    return d["accessJwt"], d["did"]


def api_get(token, method, params):
    r = requests.get(
        "{}/xrpc/{}".format(PDS, method), params=params,
        headers={"Authorization": "Bearer {}".format(token)}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def api_post(token, method, body):
    r = requests.post(
        "{}/xrpc/{}".format(PDS, method), json=body,
        headers={"Authorization": "Bearer {}".format(token)}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def create_post(token, did, text, facets, reply=None):
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": now_utc_iso(),
        "facets": facets,
        "langs": ["en"],
    }
    if reply:
        record["reply"] = reply
    return api_post(token, "com.atproto.repo.createRecord", {
        "repo": did, "collection": "app.bsky.feed.post", "record": record,
    })


# --------------------------------------------------------------------------- #
# post — the daily count
# --------------------------------------------------------------------------- #
def already_posted_today(token, did, count):
    feed = api_get(token, "app.bsky.feed.getAuthorFeed",
                   {"actor": did, "limit": 5})
    today = dt.datetime.now(BRUSSELS).date()
    prefix = "{:,} days remain".format(count)
    for item in feed.get("feed", []):
        rec = item.get("post", {}).get("record", {})
        if rec.get("text", "").startswith(prefix) and \
                brussels_date(rec.get("createdAt", "")) == today:
            return True
    return False


def cmd_post(args):
    count, text, facets = canonical_payload()
    if args.dry_run:
        print("[dry-run] would post ({} graphemes):\n{}".format(len(text), text))
        return
    token, did = session()
    if already_posted_today(token, did, count):
        print("Today's count ({:,}) already posted; exiting clean.".format(count))
        return
    res = create_post(token, did, text, facets)
    print("Posted {:,}-day count: {}".format(count, res.get("uri")))


# --------------------------------------------------------------------------- #
# mentions — reply on summons
# --------------------------------------------------------------------------- #
def own_reply_exists(token, did, uri):
    try:
        th = api_get(token, "app.bsky.feed.getPostThread",
                     {"uri": uri, "depth": 1})
    except requests.HTTPError:
        return False
    for r in th.get("thread", {}).get("replies", []) or []:
        if r.get("post", {}).get("author", {}).get("did") == did:
            return True
    return False


def count_todays_replies(token, did):
    feed = api_get(token, "app.bsky.feed.getAuthorFeed",
                   {"actor": did, "limit": 50, "filter": "posts_with_replies"})
    today = dt.datetime.now(BRUSSELS).date()
    n = 0
    for item in feed.get("feed", []):
        post = item.get("post", {})
        rec = post.get("record", {})
        if rec.get("reply") and post.get("author", {}).get("did") == did \
                and brussels_date(rec.get("createdAt", "")) == today:
            n += 1
    return n


def cmd_mentions(args):
    count, text, facets = canonical_payload()
    token, did = session()
    notifs = api_get(token, "app.bsky.notification.listNotifications",
                     {"limit": 50}).get("notifications", [])
    mentions = [n for n in notifs if n.get("reason") == "mention"]

    todays = 0 if args.dry_run else count_todays_replies(token, did)
    replied_roots = set()
    made = 0
    for n in mentions:
        uri, cid = n.get("uri"), n.get("cid")
        reply_ref = (n.get("record") or {}).get("reply") or {}
        root = reply_ref.get("root") or {"uri": uri, "cid": cid}
        if root.get("uri") in replied_roots:          # max 1 reply per thread
            continue
        if own_reply_exists(token, did, uri):          # idempotent
            replied_roots.add(root.get("uri"))
            continue
        if todays + made >= MAX_REPLIES_PER_DAY:
            die("Daily reply cap ({}) reached; failing loudly rather than flood."
                .format(MAX_REPLIES_PER_DAY))
        if args.dry_run:
            print("[dry-run] would reply to {}".format(uri))
        else:
            create_post(token, did, text, facets,
                        reply={"root": root, "parent": {"uri": uri, "cid": cid}})
            print("Replied to summons: {}".format(uri))
        replied_roots.add(root.get("uri"))
        made += 1
    print("Mentions handled: {} new repl{}.".format(made, "y" if made == 1 else "ies"))


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
                   "subject": subject, "createdAt": now_utc_iso()},
    })


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
        print("[dry-run] {} candidate author(s) with >={} on-topic posts; "
              "{} qualified; would follow {} (cap {}/day, {} logged of {}):"
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
    )
    if args.dry_run:
        print(title)
        print(body)
        return

    repo = os.environ.get("GITHUB_REPOSITORY")
    tok = os.environ.get("GITHUB_TOKEN")
    if not repo or not tok:
        die("GITHUB_REPOSITORY / GITHUB_TOKEN not set (Actions provides both).")
    r = requests.post(
        "https://api.github.com/repos/{}/issues".format(repo),
        headers={"Authorization": "Bearer {}".format(tok),
                 "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    print("Opened digest issue:", r.json().get("html_url"))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="Abundance Challenge ticker bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("post", cmd_post), ("mentions", cmd_mentions),
                     ("discover", cmd_discover), ("digest", cmd_digest)):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true",
                       help="compute and print actions without posting/following")
        p.set_defaults(func=fn)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
