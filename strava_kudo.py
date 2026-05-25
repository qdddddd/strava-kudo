#!/usr/bin/env python3
"""Auto-give kudos to friends' new Strava activities.

Reads session cookies from your logged-in Edge browser, fetches the
"following" feed, and POSTs kudos for any activity where canKudo is true.
Already-kudoed activity IDs are stored in state.json so they're never retried.

Designed to run from cron, e.g.:
    */30 * * * * cron-log strava-kudo python /home/qdu/projects/strava-kudo/strava_kudo.py
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import tempfile
from pathlib import Path

import browser_cookie3
import requests

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"

BASE = "https://www.strava.com"
DASHBOARD = f"{BASE}/dashboard"
FEED_URL = f"{BASE}/dashboard/feed"
KUDO_URL = f"{BASE}/feed/activity/{{id}}/kudo"

# This UA may need refreshing if Strava tightens client fingerprinting.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
)

log = logging.getLogger("kudo")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"kudoed_ids": []}


def save_state(state: dict) -> None:
    if len(state["kudoed_ids"]) > 5000:
        state["kudoed_ids"] = state["kudoed_ids"][-3000:]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=ROOT, delete=False) as f:
        json.dump(state, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        tmp_path = Path(f.name)
    os.replace(tmp_path, STATE_FILE)


def retry_after_seconds(r: requests.Response, default: float = 60.0) -> float:
    retry_after = r.headers.get("Retry-After")
    if not retry_after:
        return default
    try:
        return float(retry_after)
    except ValueError:
        return default


def request_with_429_retry(
    s: requests.Session,
    method: str,
    url: str,
    *,
    retry_default: float = 60.0,
    **kwargs,
) -> requests.Response:
    r = s.request(method, url, **kwargs)
    if r.status_code != 429:
        return r
    delay = retry_after_seconds(r, retry_default)
    log.warning("rate limited on %s %s; sleeping %.1fs and retrying once", method.upper(), url, delay)
    time.sleep(delay)
    return s.request(method, url, **kwargs)


def make_session(browser: str) -> requests.Session:
    loader = {
        "edge": browser_cookie3.edge,
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "chromium": browser_cookie3.chromium,
    }[browser]
    try:
        cj = loader(domain_name="strava.com")
    except Exception:
        time.sleep(3)
        cj = loader(domain_name="strava.com")
    if not any(c.name == "_strava4_session" for c in cj):
        raise SystemExit(
            f"no _strava4_session cookie found in {browser}. "
            "Log in to strava.com in that browser, then retry."
        )
    s = requests.Session()
    for c in cj:
        s.cookies.set_cookie(c)
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US"})
    return s


def bootstrap(s: requests.Session) -> tuple[str, str]:
    """GET /dashboard to extract the CSRF token and current athlete id."""
    r = request_with_429_retry(s, "get", DASHBOARD, timeout=20)
    r.raise_for_status()
    csrf_m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    ath_m = re.search(r'/athletes/(\d+)', r.text)
    if not csrf_m or not ath_m:
        raise SystemExit(
            "could not parse csrf token / athlete id from dashboard. "
            "Cookies may be stale — re-login in the browser."
        )
    return csrf_m.group(1), ath_m.group(1)


def fetch_feed(s: requests.Session, athlete_id: str, before: int | None, cursor: int | None) -> dict:
    params: dict[str, str] = {"feed_type": "following", "athlete_id": athlete_id}
    if before:
        params["before"] = str(before)
    if cursor is not None:
        params["cursor"] = str(cursor)
    r = request_with_429_retry(
        s,
        "get",
        FEED_URL,
        params=params,
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": DASHBOARD,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def iter_kudoable(feed: dict, athlete_id: str):
    """Yield (activity_id, athlete_name, activity_name) for kudoable entries."""
    for e in feed.get("entries", []):
        ent = e.get("entity")
        if ent == "Activity":
            a = e.get("activity") or {}
            if a.get("ownedByCurrentAthlete"):
                continue
            kc = a.get("kudosAndComments") or {}
            if kc.get("canKudo"):
                yield (
                    str(a["id"]),
                    (a.get("athlete") or {}).get("athleteName", "?"),
                    a.get("activityName", "?"),
                )
        elif ent == "GroupActivity":
            kc_map = e.get("kudosAndComments") or {}
            for sub in (e.get("rowData") or {}).get("activities", []):
                sub_ath = sub.get("athlete") or {}
                sub_athlete_id = sub_ath.get("athleteId") or sub.get("athlete_id")
                if str(sub_athlete_id) == athlete_id:
                    continue
                aid = sub.get("activity_id") or sub.get("id")
                if not aid:
                    continue
                aid = str(aid)
                sub_kc = kc_map.get(aid) or {}
                if sub_kc.get("canKudo"):
                    ath = sub.get("athlete") or {}
                    who = ath.get("athleteName") or sub.get("athlete_name") or "?"
                    what = sub.get("name") or sub.get("type") or "?"
                    yield aid, who, what


def give_kudos(s: requests.Session, csrf: str, aid: str) -> requests.Response:
    return request_with_429_retry(
        s,
        "post",
        KUDO_URL.format(id=aid),
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
            "Referer": DASHBOARD,
        },
        data="{}",
        timeout=15,
    )


def oldest_before_and_cursor(entries: list) -> tuple[int | None, int | None]:
    best_before = None
    best_cursor = None
    for e in entries:
        c = e.get("cursorData") or {}
        updated_at = c.get("updated_at")
        cursor = c.get("rank")
        if not updated_at:
            continue
        if best_before is None or updated_at < best_before:
            best_before = updated_at
            best_cursor = cursor
    return best_before, best_cursor


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be kudoed without sending")
    p.add_argument("--pages", type=int, default=1,
                   help="how many feed pages to walk (default 1, ≈20 entries each)")
    p.add_argument("--sleep", type=float, default=1.5,
                   help="seconds to sleep between kudo POSTs")
    p.add_argument("--browser", default="edge",
                   choices=["edge", "chrome", "firefox", "chromium"],
                   help="which browser to read cookies from")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])

    state = load_state()
    already = set(state["kudoed_ids"])

    s = make_session(args.browser)
    csrf, athlete_id = bootstrap(s)
    log.info("logged in as athlete %s; %d ids in state",
             athlete_id, len(already))

    before: int | None = None
    cursor: int | None = None
    new = sent = failed = 0
    for page in range(args.pages):
        feed = fetch_feed(s, athlete_id, before, cursor)
        entries = feed.get("entries", [])
        has_more = (feed.get("pagination") or {}).get("hasMore", False)
        log.info("page %d: %d entries (hasMore=%s, before=%s)",
                 page + 1, len(entries), has_more, before)

        for aid, who, what in iter_kudoable(feed, athlete_id):
            if aid in already:
                continue
            new += 1
            if args.dry_run:
                log.info("[dry] would kudo %s — %s: %s", aid, who, what)
                continue
            r = give_kudos(s, csrf, aid)
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            body = r.text[:200]
            ok = r.status_code == 200 and payload.get("success") == "true"
            (log.info if ok else log.warning)(
                "kudo %s %s — %s: %s (%s %s)",
                "ok" if ok else "FAIL", aid, who, what, r.status_code, body[:80],
            )
            if ok:
                already.add(aid)
                state["kudoed_ids"].append(aid)
                sent += 1
            else:
                failed += 1
            time.sleep(args.sleep)

        if not has_more:
            break
        nxt_before, nxt_cursor = oldest_before_and_cursor(entries)
        if not nxt_before or (nxt_before == before and nxt_cursor == cursor):
            break
        before = nxt_before
        cursor = nxt_cursor

    save_state(state)
    log.info("done: kudoable_new=%d sent=%d failed=%d", new, sent, failed)
    if failed and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
