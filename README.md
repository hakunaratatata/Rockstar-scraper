# GTA VI Physical Product Monitor

A passive Windows desktop monitor for publicly accessible GTA VI physical-product developments. The original Rockstar Newswire monitoring remains part of the source set.

## Setup and launch

Run `setup.bat` once, then start the application manually with `run.bat`. Python 3.11+ is required. The application does **not** launch automatically with Windows and does not create or modify Windows startup entries. Closing the window minimizes it to the system tray; choose **Quit** from the tray menu to stop monitoring.

Local data is stored in `%LOCALAPPDATA%\RockstarNewsMonitor`:

- `monitor.db` — versioned SQLite database
- `monitor.log` — local error log

## Built-in sources

| Source | Public URL | Provenance | Minimum interval |
|---|---|---:|---:|
| Rockstar Store | `https://store.rockstargames.com/en/` | OFFICIAL | 30 minutes |
| Rockstar Newswire | `https://www.rockstargames.com/newswire` | OFFICIAL | 30 minutes |
| Rockstar GTA VI | `https://www.rockstargames.com/VI` | OFFICIAL | 30 minutes |
| PlayStation GTA VI | `https://www.playstation.com/en-us/games/grand-theft-auto-vi/` | OFFICIAL | 30 minutes |

These are ordinary public HTML pages. The app uses public JSON-LD when present and conservative HTML parsing otherwise. It does not claim or rely on a hidden Rockstar Store API. Conditional requests use ETag/Last-Modified when the server provides them. Social-media platforms are not monitored.

Sources can be enabled or disabled on the **Sources** tab. You can also add a public HTTPS retailer or reporting page; its exact hostname becomes its URL policy and it receives a conservative 60- or 120-minute minimum interval. The app checks sources sequentially. `Retry-After` and protective 403/429 cooldowns take precedence over manual and scheduled checks.

## Evidence and confidence

Information from Rockstar or PlayStation is labeled **OFFICIAL**. The data model also supports **RETAILER**, **SECONDARY REPORT**, and **UNVERIFIED** provenance for future user-configured sources. A report is never silently upgraded to official evidence.

Only strong GTA VI terms such as “Grand Theft Auto VI,” “GTA VI,” or “GTAVI” qualify. Generic terms such as “Vice” do not. Items without clear physical-product language appear as **POSSIBLE MATCH** and do not generate high-confidence alerts.

The database stores normalized observations, content hashes, parser names, small evidence excerpts, prior/current event values, and changed fields. It does not store cookies, credentials, browser data, sensitive headers, or full third-party pages.

## Meaningful changes

The monitor records new products, preorder openings, availability/sold-out transitions, price changes, metadata updates, and confirmed removals. Missing items are not considered removed after one response; three successful parsed observations are required. Empty or failed parses do not count as removals.

The first successful observation for each source establishes a baseline. Later discoveries can alert. Notification types are configurable, including an optional no-change cycle notification.

## UI

- **Status:** last/next check and cycle totals
- **Products:** sortable product list, provenance, metadata, and event history
- **Activity:** chronological source checks and changes
- **Sources:** enable/disable controls and cooldown/error state
- **Settings:** interval and notification preferences
- **Export:** normalized products and events as JSON or products as CSV

## Responsible operation

The application uses only normal unauthenticated HTTPS requests. It does not log in, copy cookies, bypass CAPTCHAs or Cloudflare, rotate proxies, enumerate endpoints, spoof browser fingerprints, access game services, reserve products, add items to carts, or purchase anything. Keep polling conservative and review each site's terms and robots guidance.

## Troubleshooting and limitations

- If setup reports that Python is missing, install Python 3.11+ and rerun `setup.bat`.
- A 403 or 429 appears in **Sources** and causes a long cooldown; do not try to bypass it.
- Public site layouts change. A source can return zero relevant observations without implying that a product was removed.
- JavaScript-only content may not appear in ordinary HTML. The monitor deliberately does not bypass anti-bot systems.
- Generic retailer/reporting pages depend on visible HTML or JSON-LD and may need a dedicated provider for complete catalog coverage.
- Windows tray behavior, sleep/resume timing, and long-running operation should still be manually exercised on the target machine.

Run tests without contacting live sites:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
