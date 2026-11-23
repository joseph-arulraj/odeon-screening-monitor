# Odeon Screening Monitor

Checks Odeon's showtime API for **16 December 2026** at:

- London West End (site ID 155)
- London Leicester Square (site ID 153)

When a showtime appears that was not there before, it sends a push notification to your phone through [ntfy](https://ntfy.sh/). Tapping the notification opens Odeon's seat picker for that showing.

## Why this runs on your computer and not on GitHub Actions

Odeon's Cloudflare returns `HTTP 403` to GitHub's servers for both `www.odeon.co.uk` and the API at `vwc.odeon.co.uk`. This was measured from a GitHub-hosted runner on 23 November 2026: four requests, with and without a browser User-Agent, all answered 403 by `server: cloudflare` with an HTML block page. It is a block on the network the request comes from. A bearer token does not change it, because the request is refused before Odeon's API ever looks at the token.

So the scheduled workflow has been removed and the script is meant to run from a home internet connection.

## The bearer token

The API does need a bearer token, and it is temporary (about 12 hours). It is a public guest token, not a login. Every odeon.co.uk page gives one to every visitor in `window.initialData.api.authToken`.

`monitor.py` handles it by itself:

1. It reads a guest token from `https://www.odeon.co.uk/my-account/` (falling back to the home page).
2. It caches the token in `.odeon_token.json` and reuses it until 15 minutes before it expires.
3. If the API answers 401, it fetches a new token and retries once.

You never copy a token from your browser. `ODEON_BEARER_TOKEN` still works as a manual override, but there is no reason to set it.

## Setup

Needs Python 3.9 or newer. No extra packages.

1. Install the ntfy app on your phone and subscribe to a long, random topic name.
2. Download this repository to your computer (`git clone`, or **Code → Download ZIP**).
3. Copy `.env.example` to `.env` and put your topic in it:

   ```
   NTFY_TOPIC=your-long-random-topic
   ```

   `.env` is ignored by git, so the topic is never pushed to this public repo.
4. Check that notifications reach your phone:

   ```
   python monitor.py --test-notify
   ```

5. Start the monitor and leave the window open:

   ```
   python monitor.py --watch
   ```

On Windows the command may be `py` instead of `python`. On macOS and Linux it is usually `python3`.

The first successful check records the showtimes that already exist and sends nothing. After that, each new showtime ID produces one notification. A check runs every 5 minutes (`--interval 120` changes that, minimum 60 seconds). The script stops by itself after 12 December 2026 (`STOP_AFTER` in `monitor.py`).

The computer has to be on, awake and online for checks to happen. Turn off any VPN, since Cloudflare blocks many VPN addresses the same way it blocks GitHub.

### Running it in the background instead

`python monitor.py` with no options does one check and exits, which suits a scheduler.

Windows Task Scheduler, every 5 minutes with no window:

```
schtasks /Create /TN "OdeonMonitor" /SC MINUTE /MO 5 /TR "\"C:\full\path\to\pythonw.exe\" \"C:\full\path\to\monitor.py\""
```

macOS or Linux cron (`crontab -e`):

```
*/5 * * * * /usr/bin/python3 /full/path/to/monitor.py >> /full/path/to/monitor.log 2>&1
```

## If it stops getting through

If checks fail for 30 minutes in a row you get one "Odeon monitor is not getting through" notification with the reason, and one more when it recovers. Common reasons:

- **Cloudflare refused this network (HTTP 403).** The connection you are on is blocked. Try without a VPN or from a different network.
- **Queue-it waiting room.** Odeon puts its website behind a queue during big on-sales. The API often keeps working, and a cached token keeps the monitor going until it expires.
- **No authToken in the page.** Odeon changed its website and the token lookup needs updating.

## Files

| File | Purpose |
| --- | --- |
| `monitor.py` | The monitor. |
| `.env` | Your ntfy topic. Local only. |
| `state.json` | Showtime IDs already seen. Local only. Delete it to start again from a fresh baseline. |
| `.odeon_token.json` | Cached guest token. Local only. |
| `tests/test_monitor.py` | Offline tests against a fake Odeon and fake ntfy: `python -m unittest discover -s tests` |

## Response parsing

The parser reads `showtimes[]` and joins each record to `relatedData.films[]`, `relatedData.sites[]` and `relatedData.attributes[]` by `filmId`, `siteId` and `attributeIds`. Session start comes from `schedule.startsAt` and the film start from `schedule.filmStartsAt`, both shown in London time.
