#!/usr/bin/env python3
"""Poll Odeon's showtime API and notify on newly listed sessions."""
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SHOW_DATE = "2026-12-16"
SITES = {"155": "London West End", "153": "London Leicester Square"}
STATE_PATH = Path("state.json")
BASE = "https://vwc.odeon.co.uk/WSVistaWebClient/ocapi/v1/showtimes/by-business-date"
LONDON = ZoneInfo("Europe/London")


def fetch(site_id):
    url = f"{BASE}/{SHOW_DATE}?siteIds={site_id}"
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-GB,en;q=0.8",
        "connect-region-code": "UK",
        "User-Agent": "OdeonScreeningMonitor/1.0",
    }
    token = os.getenv("ODEON_BEARER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


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
    """Parse the documented response shape: showtimes + relatedData."""
    related = payload.get("relatedData") or {}
    films = {
        str(film.get("id")): text_field(film.get("title"))
        for film in related.get("films", [])
    }
    sites = {
        str(site.get("id")): text_field(site.get("name"))
        for site in related.get("sites", [])
    }
    attributes = {
        str(item.get("id")): text_field(item.get("name"))
        for item in related.get("attributes", [])
    }

    sessions = []
    for show in payload.get("showtimes", []):
        if str(show.get("siteId")) != str(requested_site_id):
            continue
        schedule = show.get("schedule") or {}
        film_id = str(show.get("filmId", ""))
        site_id = str(show.get("siteId", requested_site_id))
        session_id = str(show.get("id", "")).strip()
        if not session_id:
            # The API normally supplies an ID; this fallback remains deterministic.
            session_id = hashlib.sha256(
                json.dumps(show, sort_keys=True).encode("utf-8")
            ).hexdigest()

        details = {
            "session_id": session_id,
            "site": sites.get(site_id, SITES.get(site_id, f"Site {site_id}")),
            "film": films.get(film_id, f"Film {film_id}"),
            "session_start": local_time(schedule.get("startsAt")),
            "film_start": local_time(schedule.get("filmStartsAt")),
            "sold_out": bool(show.get("isSoldOut", False)),
            "attributes": [
                attributes.get(str(attribute_id), str(attribute_id))
                for attribute_id in show.get("attributeIds", [])
            ],
        }
        # Use the API's stable session ID as the identity of an announced screening.
        sessions.append((session_id, details))
    return sessions


def notify(topic, title, message):
    req = Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "movie_camera"},
        method="POST",
    )
    with urlopen(req, timeout=20) as response:
        response.read()


def main():
    if datetime.now(timezone.utc).date() > date(2026, 12, 12):
        print("Monitoring period has ended.")
        return

    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("Missing NTFY_TOPIC GitHub Actions secret.")

    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    previously_seen = set(state.get("seen", []))
    current = {}
    for site_id in SITES:
        payload = fetch(site_id)
        if str(payload.get("businessDate")) != SHOW_DATE:
            raise RuntimeError(
                f"Unexpected businessDate for site {site_id}: {payload.get('businessDate')}"
            )
        sessions = extract_sessions(payload, site_id)
        print(f"{SITES[site_id]}: parsed {len(sessions)} showtime sessions")
        for session_id, details in sessions:
            current[session_id] = details

    # First successful poll establishes a baseline and avoids alerts for existing sessions.
    if not state.get("initialised"):
        STATE_PATH.write_text(json.dumps({
            "initialised": True,
            "seen": sorted(current),
        }, indent=2) + "\n")
        print(f"Baseline saved: {len(current)} existing sessions. No alerts sent.")
        return

    new_ids = sorted(set(current) - previously_seen)
    for session_id in new_ids:
        details = current[session_id]
        lines = [
            f"Cinema: {details['site']}",
            f"Film: {details['film']}",
            f"Session begins: {details['session_start']} (London time)",
            f"Film begins: {details['film_start']} (London time)",
            f"Sold out: {'Yes' if details['sold_out'] else 'No'}",
        ]
        if details["attributes"]:
            lines.append("Features: " + ", ".join(details["attributes"]))
        lines.append(f"Session ID: {session_id}")
        notify(topic, f"New Odeon showtime: {details['film']}", "\n".join(lines))
        print(f"Alert sent: {details['site']} / {details['film']} / {details['film_start']}")

    STATE_PATH.write_text(json.dumps({
        "initialised": True,
        "seen": sorted(previously_seen.union(current)),
    }, indent=2) + "\n")
    print(f"Poll complete. {len(new_ids)} new session(s).")


if __name__ == "__main__":
    try:
        main()
    except (HTTPError, URLError) as exc:
        print(f"Network/API error: {exc}", file=sys.stderr)
        raise
