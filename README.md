# Odeon Screening Monitor

Polls Odeon's showtime API for **16 December 2026** at:

- London West End (site ID 155)
- London Leicester Square (site ID 153)

The GitHub Actions workflow runs every five minutes from 23 November until 12 December 2026 (the script exits after 12 December). GitHub may delay scheduled jobs, so five minutes is the requested cadence, not a guaranteed execution time.

## Setup

1. Install the ntfy app on your phone and subscribe to a long, random topic name at https://ntfy.sh/.
2. In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Add a secret named `NTFY_TOPIC` with the exact topic name you subscribed to.
4. Optionally add `ODEON_BEARER_TOKEN` if the API requires authentication. Use a fresh token and keep it in Actions Secrets only. Do not commit tokens, cookies, or browser session headers.
5. Open **Actions → Odeon showtime monitor → Run workflow** to test the workflow manually.

The first successful run creates a baseline and does not notify for sessions already listed. Later runs notify when a new session is detected.

## Important API note

The bearer token and cookies in a copied browser request are short-lived session credentials. They are not embedded in this repository. If Odeon requires authentication and the token expires, the workflow will fail until a fresh token is supplied. The response schema can also change; check the Actions log after the first run to confirm that session-like records are being detected.

## State and notifications

Detected session fingerprints are persisted in `state.json` by a workflow commit. Notifications are sent to the configured ntfy topic. Keep the repository private if you later add any non-public configuration; never commit credentials.
