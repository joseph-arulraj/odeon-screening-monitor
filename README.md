# Odeon Screening Monitor

Polls Odeon's showtime API for **16 December 2026** at:

- London West End (site ID 155)
- London Leicester Square (site ID 153)

The GitHub Actions workflow requests a run every five minutes from 23 November until 12 December 2026 (the script exits after 12 December). GitHub may delay scheduled jobs, so five minutes is the requested cadence, not a guaranteed execution time.

## Setup

1. Install the ntfy app on your phone and subscribe to a long, random topic name at https://ntfy.sh/.
2. In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Add a secret named `NTFY_TOPIC` with the exact topic name you subscribed to.
4. Add `ODEON_BEARER_TOKEN` only if the endpoint requires it. Use a fresh token and keep it in Actions Secrets only. Never commit tokens, cookies, or browser session headers.
5. Open **Actions → Odeon showtime monitor → Run workflow** to test the workflow manually.

The first successful run creates a baseline and does not notify for sessions already listed. Later runs notify when a previously unseen showtime ID appears.

## Response parsing

The parser uses the API's `showtimes[]` records and joins them to `relatedData.films[]` and `relatedData.sites[]` using `filmId` and `siteId`. It reads the session start from `schedule.startsAt` and the actual film start from `schedule.filmStartsAt`, displaying both in Europe/London time.

## Authentication

The bearer token and cookies in a copied browser request are session credentials and may expire. They are not embedded in this repository. If Odeon requires authentication and the token expires, the workflow will fail until a fresh token is supplied. Check the Actions log after the first run to confirm that both API requests succeeded.

## State and notifications

Detected session IDs are persisted in `state.json` by a workflow commit. Notifications are sent to the configured ntfy topic. Never commit credentials.
