# strava-kudo

Auto-give kudos to friends' new Strava activities.

## How it works

The Strava v3 OAuth API does **not** expose the "following" activity feed
(removed years ago), so this script piggybacks on the same internal endpoints
the web dashboard uses:

- `GET /dashboard/feed?feed_type=following&athlete_id=…` → JSON feed
- `POST /feed/activity/{id}/kudo` → `{"success":"true"}`

Authentication is the browser's logged-in session cookie (`_strava4_session`),
read directly from your browser's cookie DB via `browser_cookie3` — no manual
export needed.

> ⚠️ This uses Strava's private web endpoints (not the public OAuth API), so it
> may violate Strava's terms of service and can break without warning if Strava
> changes the dashboard contract, CSRF handling, or session cookie format.

## Setup

```bash
~/.venv/bin/pip install browser_cookie3 requests
```

Make sure you're logged in to strava.com in Edge (or another supported browser).
Then verify with a dry-run:

```bash
~/.venv/bin/python strava_kudo.py --dry-run --pages 2
```

You should see `logged in as athlete <your_id>` and a list of any new kudoable
entries (or nothing if you're already caught up).

## Usage

```bash
# normal run — kudo everyone in the top feed page
~/.venv/bin/python strava_kudo.py

# walk more pages of history (useful for first run)
~/.venv/bin/python strava_kudo.py --pages 5

# use a different browser
~/.venv/bin/python strava_kudo.py --browser chrome

# show what would happen without sending
~/.venv/bin/python strava_kudo.py --dry-run
```

State (already-kudoed activity IDs) is persisted to `state.json`.

## Cron

Every 30 minutes, using the local `cron-log` wrapper which prefixes lines with
timestamp + tag and appends to `~/.local/var/log/cron.log`:

```cron
*/30 * * * * cron-log strava-kudo python /home/qdu/projects/strava-kudo/strava_kudo.py
```

Tail the log with `grep '\[strava-kudo\]' ~/.local/var/log/cron.log` to see runs.

Edge must be installed (the script reads from `~/.config/microsoft-edge/Default/Cookies`).
You don't need Edge running, but it must have a valid Strava session — if cookies
expire (typically months), the script will exit with a clear error and you just
re-login in the browser.

## Notes

- `state.json` is a safety net only; the script checks Strava's own `canKudo`
  flag before posting, so it won't double-kudo even if state is lost.
- Default 1.5s sleep between POSTs to stay polite. Tune with `--sleep`.
- Only `Activity` and `GroupActivity` entries are handled. Promotional / club
  / suggested-follow entries are ignored.
