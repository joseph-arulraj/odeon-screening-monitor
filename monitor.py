#!/usr/bin/env python3
"""Watch Odeon's showtime API and send an ntfy alert when a new session appears.

Run this from a home internet connection. Odeon's Cloudflare returns HTTP 403 to
GitHub's servers (and other data centres) for both the website and the API, so
it cannot run on a GitHub-hosted Actions runner.

The API needs a bearer token. That token is a public guest token which every
odeon.co.uk page hands to every visitor in ``window.initialData.api.authToken``.
It lasts roughly 12 hours, so this script reads a fresh one from the site when
it needs one and caches it locally. Nothing has to be copied from a browser.

Usage:
    python monitor.py                 check once and exit
    python monitor.py --watch         check every 5 minutes until stopped
    python monitor.py --test-notify   send a test notification and exit
"""
import argparse
import base64
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------- what to watch
SHOW_DATE = "2026-12-16"
STOP_AFTER = date(2026, 12, 12)  # last day the monitor does anything
SITES = {"155": "London West End", "153": "London Leicester Square"}

# ------------------------------------------------------------------- plumbing
HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
TOKEN_PATH = HERE / ".odeon_token.json"
ENV_PATH = HERE / ".env"
try:
    LONDON = ZoneInfo("Europe/London")
except ZoneInfoNotFoundError:
    # Windows has no time zone database unless the tzdata package is installed.
    # London is on GMT (UTC+0) for the whole monitoring window, so UTC is exact.
    LONDON = timezone.utc

DEFAULT_SITE_URL = "https://www.odeon.co.uk"
DEFAULT_API_URL = "https://vwc.odeon.co.uk/WSVistaWebClient"
DEFAULT_NTFY_URL = "https://ntfy.sh"
# Pages tried, in order, when a new guest token is needed.
TOKEN_PAGES = ("/my-account/", "/")
# Odeon's CDN turns away clients that announce themselves as scripts.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)
TIMEOUT = 30
TOKEN_REFRESH_MARGIN = 15 * 60  # refresh once the cached token has less than this left
FAILURE_ALERT_AFTER = 30 * 60  # tell the phone if checks have failed for this long
TOKEN_RE = re.compile(r'"authToken"\s*:\s*"([\w-]+\.[\w-]+\.[\w-]+)"')

# A cookie jar matters: Odeon's Queue-it check sets a cookie on a redirect, and
# without one the redirect loops.
OPENER = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))


class MonitorError(RuntimeError):
    """A check could not be completed. The message says why in plain words."""


class Blocked(MonitorError):
    """Cloudflare refused this network outright."""


def setting(name, default=""):
    return os.environ.get(name, "").strip() or default


def load_env_file(path=ENV_PATH):
    """Read KEY=VALUE lines from .env without overriding real environment variables."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def log(message):
    print(f"[{datetime.now(LONDON):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# ------------------------------------------------------------------ transport
def http_get(url, headers):
    """GET a URL. Returns (body bytes, final URL). Raises HTTPError or MonitorError."""
    host = urlsplit(url).netloc
    request = Request(url, headers={
        "User-Agent": setting("ODEON_USER_AGENT", DEFAULT_USER_AGENT),
        "Accept-Language": "en-GB,en;q=0.8",
        "Accept-Encoding": "gzip",
        **headers,
    })
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return body, response.geturl()
    except HTTPError as exc:
        server = (exc.headers.get("Server", "") if exc.headers else "").lower()
        content_type = (exc.headers.get("Content-Type", "") if exc.headers else "").lower()
        if exc.code == 403 and "cloudflare" in server and "json" not in content_type:
            raise Blocked(
                f"Cloudflare refused this network (HTTP 403 from {host}). That is an IP "
                "block, not a token problem. It happens on GitHub/cloud servers and on "
                "many VPNs. Run the monitor from a home connection with any VPN off."
            ) from exc
        raise
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise MonitorError(f"could not reach {host} ({reason})") from exc


# ---------------------------------------------------------------------- token
def jwt_expiry(token):
    """Expiry time (UTC) read from the token's own payload."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return datetime.fromtimestamp(
            json.loads(base64.urlsafe_b64decode(payload))["exp"], timezone.utc
        )
    except (IndexError, KeyError, TypeError, ValueError):
        # Unreadable expiry: assume a short life so it gets replaced soon.
        return datetime.now(timezone.utc) + timedelta(hours=1)


def token_from_page(html):
    found = TOKEN_RE.search(html)
    return found.group(1) if found else None


def read_cached_token():
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        return data["value"], datetime.fromisoformat(data["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def fetch_guest_token():
    """Read the public guest token out of an odeon.co.uk page."""
    site = setting("ODEON_SITE_URL", DEFAULT_SITE_URL).rstrip("/")
    problems = []
    for page in TOKEN_PAGES:
        url = site + page
        try:
            body, final_url = http_get(url, {"Accept": "text/html"})
        except Blocked:
            raise
        except HTTPError as exc:
            problems.append(f"{page} answered HTTP {exc.code}")
            continue
        except MonitorError as exc:
            problems.append(str(exc))
            continue
        if "queue-it" in urlsplit(final_url).netloc:
            problems.append(f"{page} redirected to Odeon's Queue-it waiting room")
            continue
        token = token_from_page(body.decode("utf-8", "replace"))
        if token:
            return token
        problems.append(f"{page} contained no authToken (Odeon may have changed its site)")
    raise MonitorError("could not get a guest token: " + "; ".join(problems))


def get_token(force_refresh=False):
    """Return a usable bearer token, fetching a new one only when needed."""
    manual = setting("ODEON_BEARER_TOKEN")
    if manual:
        return manual

    now = datetime.now(timezone.utc)
    cached = None if force_refresh else read_cached_token()
    if cached and (cached[1] - now).total_seconds() > TOKEN_REFRESH_MARGIN:
        return cached[0]

    try:
        token = fetch_guest_token()
    except MonitorError as exc:
        # The website can be down or queued while the API still works, and a
        # token with a few minutes left is enough for one check.
        if cached and (cached[1] - now).total_seconds() > 60:
            log(f"Could not refresh the token ({exc}). Using the cached one.")
            return cached[0]
        raise

    expires = jwt_expiry(token)
    TOKEN_PATH.write_text(
        json.dumps({"value": token, "expires": expires.isoformat()}), encoding="utf-8"
    )
    log(f"Fetched a new guest token, valid until {expires.astimezone(LONDON):%d %b %H:%M}.")
    return token


# ------------------------------------------------------------------ showtimes
def fetch(site_id):
    api = setting("ODEON_API_URL", DEFAULT_API_URL).rstrip("/")
    url = f"{api}/ocapi/v1/showtimes/by-business-date/{SHOW_DATE}?siteIds={site_id}"
    manual = bool(setting("ODEON_BEARER_TOKEN"))
    for attempt in (1, 2):
        token = get_token(force_refresh=(attempt == 2))
        try:
            body, _ = http_get(url, {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "connect-region-code": "UK",
            })
        except HTTPError as exc:
            if exc.code == 401 and attempt == 1 and not manual:
                log("API rejected the token (401). Fetching a new one.")
                continue
            if exc.code == 401:
                hint = (
                    " The ODEON_BEARER_TOKEN you set has expired. Remove it and the "
                    "script will fetch its own." if manual else ""
                )
                raise MonitorError(f"Odeon's API rejected the token (HTTP 401).{hint}") from exc
            raise MonitorError(f"Odeon's API answered HTTP {exc.code}") from exc
        if not body.strip():
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise MonitorError("Odeon's API sent something that is not JSON") from exc
    raise MonitorError("Odeon's API kept rejecting the token")


def text_field(value):
    if isinstance(value, dict):
        return str(value.get("text", "")).strip()
    return str(value or "").strip()


def local_time(value):
    if not value:
        return "Unknown"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LONDON).strftime("%H:%M")


def extract_sessions(payload, requested_site_id):
    """Parse the response shape: showtimes[] joined to relatedData by id."""
    related = payload.get("relatedData") or {}
    films = {
        str(film.get("id")): text_field(film.get("title"))
        for film in related.get("films") or []
    }
    sites = {
        str(site.get("id")): text_field(site.get("name"))
        for site in related.get("sites") or []
    }
    attributes = {
        str(item.get("id")): text_field(item.get("name"))
        for item in related.get("attributes") or []
    }

    sessions = []
    for show in payload.get("showtimes") or []:
        if str(show.get("siteId")) != str(requested_site_id):
            continue
        schedule = show.get("schedule") or {}
        film_id = str(show.get("filmId", ""))
        site_id = str(show.get("siteId", requested_site_id))
        api_id = str(show.get("id", "")).strip()
        # The API normally supplies an ID. This fallback stays deterministic.
        session_id = api_id or hashlib.sha256(
            json.dumps(show, sort_keys=True).encode("utf-8")
        ).hexdigest()

        sessions.append((session_id, {
            "session_id": session_id,
            "api_id": api_id,
            "site": sites.get(site_id) or SITES.get(site_id, f"Site {site_id}"),
            "film": films.get(film_id) or f"Film {film_id}",
            "session_start": local_time(schedule.get("startsAt")),
            "film_start": local_time(schedule.get("filmStartsAt")),
            "sold_out": bool(show.get("isSoldOut", False)),
            "attributes": [
                attributes.get(str(attribute_id)) or str(attribute_id)
                for attribute_id in show.get("attributeIds") or []
            ],
        }))
    return sessions


# -------------------------------------------------------------- notifications
def notify(topic, title, message, priority=4, tags=("movie_camera",), click=None):
    """Publish through ntfy's JSON endpoint, which copes with any characters in titles."""
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": list(tags),
    }
    if click:
        payload["click"] = click
    request = Request(
        setting("NTFY_URL", DEFAULT_NTFY_URL),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with OPENER.open(request, timeout=20) as response:
            response.read()
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise MonitorError(f"could not send the ntfy notification ({exc})") from exc


def booking_link(details):
    if not details["api_id"]:
        return None
    site = setting("ODEON_SITE_URL", DEFAULT_SITE_URL).rstrip("/")
    return f"{site}/ticketing/seat-picker/?showtimeId={quote(details['api_id'])}"


# ---------------------------------------------------------------------- state
def load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("seen", [])
    return state


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))
    temp = STATE_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


# ----------------------------------------------------------------------- poll
def poll(state, topic):
    """One full check of every site. Returns the number of new sessions alerted."""
    current = {}
    for site_id, site_name in SITES.items():
        payload = fetch(site_id)
        business_date = payload.get("businessDate")
        if business_date and not str(business_date).startswith(SHOW_DATE):
            raise MonitorError(
                f"unexpected businessDate for site {site_id}: {business_date}"
            )
        sessions = extract_sessions(payload, site_id)
        log(f"{site_name}: {len(sessions)} session(s) listed for {SHOW_DATE}")
        current.update(sessions)

    # The first successful check records what already exists and alerts on nothing.
    if not state.get("initialised"):
        state["initialised"] = True
        state["seen"] = sorted(current)
        save_state(state)
        log(f"Baseline saved: {len(current)} existing session(s). No alerts sent.")
        return 0

    new_ids = sorted(set(current) - set(state["seen"]))
    for session_id in new_ids:
        details = current[session_id]
        lines = [
            f"Cinema: {details['site']}",
            f"Date: {SHOW_DATE}",
            f"Session begins: {details['session_start']} (London time)",
            f"Film begins: {details['film_start']} (London time)",
            f"Sold out: {'Yes' if details['sold_out'] else 'No'}",
        ]
        if details["attributes"]:
            lines.append("Features: " + ", ".join(details["attributes"]))
        lines.append(f"Session ID: {session_id}")
        notify(
            topic,
            f"New Odeon showtime: {details['film']}",
            "\n".join(lines),
            click=booking_link(details),
        )
        # Saved after each alert, so a failure part-way never repeats earlier alerts.
        state["seen"].append(session_id)
        save_state(state)
        log(f"Alert sent: {details['site']} / {details['film']} / {details['film_start']}")

    log(f"Check complete. {len(new_ids)} new session(s).")
    return len(new_ids)


def run_once(topic):
    """Run one check and keep track of failures. Returns True if the check worked."""
    state = load_state()
    now = datetime.now(timezone.utc)
    try:
        poll(state, topic)
    except MonitorError as exc:
        log(f"Check failed: {exc}")
        state.setdefault("failing_since", now.isoformat())
        failing_for = (now - datetime.fromisoformat(state["failing_since"])).total_seconds()
        if failing_for >= FAILURE_ALERT_AFTER and not state.get("failure_alerted"):
            try:
                notify(
                    topic,
                    "Odeon monitor is not getting through",
                    f"Checks have failed for {int(failing_for // 60)} minutes, so no "
                    f"showtime alerts will arrive until this clears.\nLast error: {exc}",
                    priority=3,
                    tags=("warning",),
                )
                state["failure_alerted"] = True
            except MonitorError as notify_exc:
                log(str(notify_exc))
        save_state(state)
        return False

    if state.pop("failure_alerted", False):
        try:
            notify(topic, "Odeon monitor is working again",
                   "Checks are getting through to Odeon again.",
                   priority=3, tags=("white_check_mark",))
        except MonitorError as notify_exc:
            log(str(notify_exc))
    if state.pop("failing_since", None) is not None:
        save_state(state)
    return True


def monitoring_ended():
    return datetime.now(LONDON).date() > STOP_AFTER


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--watch", action="store_true",
                        help="keep running and check repeatedly")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between checks in --watch mode (default 300)")
    parser.add_argument("--test-notify", action="store_true",
                        help="send a test notification and exit")
    args = parser.parse_args(argv)

    # A film title can contain characters a Windows console cannot print. Replace
    # them instead of crashing. (Under pythonw the streams are None.)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    load_env_file()
    topic = setting("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC is not set. Put NTFY_TOPIC=your-topic in a .env file next "
              "to monitor.py (see README).", file=sys.stderr)
        return 2

    if args.test_notify:
        try:
            notify(topic, "Odeon monitor test",
                   "Notifications are working.", priority=3, tags=("white_check_mark",))
        except MonitorError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("Test notification sent. Check your phone.")
        return 0

    if monitoring_ended():
        log("Monitoring period has ended.")
        return 0

    if not args.watch:
        return 0 if run_once(topic) else 1

    interval = max(60, args.interval)
    log(f"Watching {', '.join(SITES.values())} for {SHOW_DATE}, every {interval}s. "
        "Press Ctrl+C to stop.")
    try:
        while not monitoring_ended():
            run_once(topic)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("Stopped.")
        return 0
    log("Monitoring period has ended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
