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

SHOW_DATE = "2026-12-16"
SITES = {"155": "London West End", "153": "London Leicester Square"}
STATE_PATH = Path("state.json")
BASE = "https://vwc.odeon.co.uk/WSVistaWebClient/ocapi/v1/showtimes/by-business-date"

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

def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)

def first_value(obj, names):
    for item in walk(obj):
        for key, value in item.items():
            if key.lower() in names and isinstance(value, (str, int, float)):
                text = str(value).strip()
                if text:
                    return text
    return ""

def extract_sessions(payload, site_name):
    sessions = []
    time_keys = {"showtime", "showtimeutc", "startdatetime", "starttime",
                 "sessiondatetime", "sessiontime", "businessdatetime"}
    title_keys = {"title", "filmtitle", "movietitle", "name"}
    seen = set()
    for obj in walk(payload):
        keys = {k.lower() for k in obj}
        # A session-like object needs a time and a film title somewhere in its subtree.
        show_time = first_value(obj, time_keys)
        title = first_value(obj, title_keys)
        if not show_time or not title:
            continue
        # Avoid treating parent containers as sessions; require a time field on this object.
        own_time = next((str(v).strip() for k, v in obj.items()
                         if k.lower() in time_keys and isinstance(v, (str, int, float)) and str(v).strip()), "")
        if not own_time:
            continue
        film = first_value(obj, {"filmtitle", "movietitle", "title", "name"}) or title
        auditorium = first_value(obj, {"auditorium", "auditoriumname", "screen", "screenname"})
        format_name = first_value(obj, {"format", "screeningformat", "experience"})
        booking_url = first_value(obj, {"bookingurl", "url", "weburl"})
        identity = {
            "site": site_name, "film": film, "time": own_time,
            "auditorium": auditorium, "format": format_name,
        }
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        sessions.append((fingerprint, identity, booking_url))
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
    end_date = date(2026, 12, 12)
    today = datetime.now(timezone.utc).date()
    if today > end_date:
        print("Monitoring period has ended.")
        return
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("Missing NTFY_TOPIC GitHub Actions secret.")

    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"seen": []}
    previously_seen = set(state.get("seen", []))
    current = []
    new_sessions = []

    for site_id, site_name in SITES.items():
        payload = fetch(site_id)
        sessions = extract_sessions(payload, site_name)
        print(f"{site_name}: found {len(sessions)} session-like records")
        for fingerprint, details, booking_url in sessions:
            current.append(fingerprint)
            if fingerprint not in previously_seen:
                new_sessions.append((details, booking_url))

    # On the first successful poll, establish a baseline rather than sending a flood of alerts.
    if "initialised" not in state:
        state = {"initialised": True, "seen": sorted(set(current))}
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
        print(f"Baseline saved ({len(current)} sessions); no notifications sent.")
        return

    for details, booking_url in new_sessions:
        lines = [
            f"Cinema: {details['site']}",
            f"Film: {details['film']}",
            f"Time: {details['time']}",
        ]
        if details.get("auditorium"):
            lines.append(f"Screen: {details['auditorium']}")
        if details.get("format"):
            lines.append(f"Format: {details['format']}")
        if booking_url:
            lines.append(f"Booking: {booking_url}")
        notify(topic, f"New Odeon showtime: {details['film']}", "\n".join(lines))
        print(f"Notification sent: {details['site']} / {details['film']} / {details['time']}")

    state["seen"] = sorted(previously_seen.union(current))
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
    print(f"Poll complete. {len(new_sessions)} new session(s).")

if __name__ == "__main__":
    try:
        main()
    except (HTTPError, URLError) as exc:
        print(f"Network/API error: {exc}", file=sys.stderr)
        raise
