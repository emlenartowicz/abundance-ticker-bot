# abundance-ticker-bot

The official automated Bluesky account for **The Abundance Challenge**
([abundancechallenge.ai](https://www.abundancechallenge.ai)) — handle
**[@abundancechallenge.ai](https://bsky.app/profile/abundancechallenge.ai)**.

A single, visibly-automated account with exactly three behaviours (nothing
else — no unsolicited replies, no engagement mechanics, no generative step):

1. **Daily post** — the day-count to 31 December 2030 (the deadline 193 nations
   set for ending extreme poverty), once a day at 15:00 Europe/Brussels. The
   number-free share card (`abundance-challenge-card.png`) rides along as an
   `app.bsky.embed.external` with an uploaded thumb; the annual data-day post
   carries it too. Mention replies stay text-only.
2. **Witness following** — follows accounts substantively posting on AI, the AI
   economy, and poverty; rate-limited (≤10/day), fully logged, never
   auto-unfollows.
3. **Reply on summons** — when a human writes `@abundancechallenge.ai` in a
   thread, replies once with the canonical count + definition, never a word
   about the thread.

Everything the bot says is canonical text, identical to what the site's
Broadcast panel composes.

## Three states (SPEC-zero-hour.md)

One instrument, three positions — the definition never changes (it carries no
date, so lateness measures lateness and never expires the challenge):

- **Countdown** — before the deadline: `{n} days remain.`
- **Overtime** — automatic after the deadline, if the era has not commenced:
  `{n} days past the deadline.` The daily post and the summons both switch; no
  human hands required.
- **Commenced** — the human switch. Put the measured ISO date in
  `commenced.txt` (cite the World Bank PIP / UN SDG source in the commit). The
  bot posts one commencement record, daily posts stop, and the summons
  thereafter speaks the record line.

The **annual reality check** (`reality-check.yml`, mid-September) posts the
canonical data-day line — with link facets to the UN SDG progress report and
the World Bank poverty data, HTTP-checked before posting — and opens a private
issue asking whether the page is still telling the truth.

## How it runs

No server. Four GitHub Actions workflows (all also `workflow_dispatch`):

| Workflow | Schedule (UTC) | Command |
|---|---|---|
| `daily-post.yml` | `0 13 * * *` | `python bot.py post` |
| `mentions.yml` | `*/15 * * * *` | `python bot.py mentions` |
| `discover.yml` | `43 20 * * *` | `python bot.py discover` |
| `digest.yml` | `0 8 * * 1` | `python bot.py digest` |
| `reality-check.yml` | `0 12 15 9 *` | `python bot.py reality-check` |

**Mentions** is a single short scheduled job: each run does one poll and exits
in ~15s, so normal cycles end in success and never email. It catches both
`mention`- and `reply`-reason summons, paginates the notification window
(bounded), and is idempotent (once per thread). Latency is the schedule interval
(GitHub throttles frequent crons; up-to-an-hour worst case is accepted). Dispatch
the workflow manually for an instant run. (An always-on internal loop was tried
and reverted — on GitHub's hosted runners it produced cancellation-email churn
and intermittent startup failures; a plain scheduled job is startup-clean and
email-quiet.)

Python 3.11, standard library + `requests`. AT Protocol over plain HTTPS
against `https://bsky.social`. Failure emails from Actions are the monitoring
layer — every command fails loudly (non-zero) on anything unexpected, and
**never truncates** the definition or posts a zero/negative count.

## Secrets (repo → Settings → Secrets and variables → Actions)

- `BSKY_HANDLE` = `abundancechallenge.ai`
- `BSKY_APP_PASSWORD` = a Bluesky **app password** (Settings → Privacy and
  security → App passwords), bot-scoped and revocable in one click.

`GITHUB_TOKEN` is provided by Actions automatically (used by the digest to open
an issue and by discover to commit `follows.log`).

## Files

- `bot.py` — the whole bot (`post` / `mentions` / `discover` / `digest` /
  `reality-check`; each takes `--dry-run`).
- `queries.txt` — discovery search terms, one per line, editable without code.
- `never-again.txt` — handles/DIDs discovery must never follow.
- `follows.log` — append-only follow record (`date handle did`).
- `commenced.txt` — the commencement switch (empty until the era commences).

## Guardrails

Additive infrastructure only. This repo is separate from the site repo
(`abundance-challenge`) and from AIC-WG-Tool; it never touches either, and
never opens the `aicframework.net` DNS zone.
