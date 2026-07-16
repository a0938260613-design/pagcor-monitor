# PAGCOR Regulatory Monitor

Monitors the PAGCOR regulatory site for HTML, PDF, Excel, and document changes, then generates a human-readable daily Markdown report.

## Setup

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill Telegram values in `.env` only if you want push notifications.

## Run

```powershell
python monitor.py
```

Outputs:

- `data/state.json` - latest known snapshot
- `data/downloads/` - downloaded files by hash
- `reports/YYYY-MM-DD_HH-mm-ss.md` - readable Markdown change report
- `reports/latest.html` - browser-friendly latest report

## Open the report

If `.md` files do not open cleanly on Windows, open `reports/latest.html` in a browser.

## Telegram

```powershell
python send_telegram.py
```

For daily automation, run `python monitor.py` from Windows Task Scheduler, then run `python send_telegram.py`. Telegram sends `reports/telegram_summary.txt` by default; the full report remains in `reports/latest.md`.

## Runtime notes

The first full run can take several minutes because it downloads PDFs and builds the baseline. If you run it from an automation tool, set the task timeout to at least 10-15 minutes.

Recommended `.env` values for full monitoring:

```env
PAGCOR_MAX_PAGES=1000
PAGCOR_REQUEST_DELAY_SECONDS=0.2
PAGCOR_REQUEST_TIMEOUT_SECONDS=45
```

For a quick test, temporarily lower `PAGCOR_MAX_PAGES` to `40`.

## Monitoring philosophy

This project is designed for full PAGCOR regulatory monitoring. It should collect every reachable regulatory page and downloadable resource, then classify changes by severity for readable reporting. Low-risk changes are still recorded; they are only de-prioritized in the report.

See `MONITORING_STRATEGY.md` for the monitoring and severity model.


## Daily automation

Use `run_daily.bat` in Windows Task Scheduler after `.env` is configured.

Recommended Task Scheduler settings:

- Program/script: `C:\Users\ronnieli\Desktop\cowork\pagcor 監控\run_daily.bat`
- Start in: `C:\Users\ronnieli\Desktop\cowork\pagcor 監控`
- Allow the task to run for at least 15 minutes.
- Avoid overlapping runs. If a previous run is still active, do not start a new instance.

Validation note: a 1000-resource cap completed the current reachable regulatory site at 473 resources in about 6 minutes. The next identical run produced 0 changes, confirming the baseline comparison is stable.



## GitHub Pages publishing

The monitor writes public website files to `docs/`:

- `docs/index.html` - latest public report
- `docs/latest.html` - same as latest public report
- `docs/reports/YYYY-MM-DD_HH-mm-ss.html` - timestamped archive

Sensitive local files are excluded by `.gitignore`, including `.env`, `data/`, and raw local report files.

Setup steps:

1. Create a GitHub repository.
2. Push this project to the repository.
3. In GitHub, open Settings > Pages.
4. Set Source to `Deploy from a branch`.
5. Set Branch to `main` and folder to `/docs`.
6. Save. GitHub will provide a Pages URL.
7. Add that URL to `.env`:

```env
GITHUB_PAGES_URL=https://YOUR_ACCOUNT.github.io/YOUR_REPO/
```

Daily publishing:

```powershell
publish_pages.bat
```

This runs the monitor, sends the Telegram summary (if `.env` has bot token/chat id), updates `docs/`, commits the public report, and pushes it to GitHub.

## Unattended local scheduling and resume

This project is intended to run from the local Windows machine. GitHub Pages is only used to share the generated HTML report.

Reliability behavior:

- `data/state.json` is the last completed baseline.
- `data/checkpoint.json` is written during a run.
- If the task stops before completion, the next run resumes from `data/checkpoint.json`.
- The official state and report are updated only after the crawl finishes.
- If `PAGCOR_MAX_PAGES` is reached, the monitor will not mark missing old URLs as removed.

For Windows Task Scheduler:

- Enable `Run whether user is logged on or not`.
- Enable `Run with highest privileges` if git or network credentials require it.
- Set `Start in` to this project folder.
- Set the timeout to at least 30 minutes.
- Configure the task not to start a new instance if the previous run is still active.

Use `publish_pages.bat` for the daily task when GitHub Pages is configured. It runs the monitor, sends the Telegram summary, updates `docs/`, commits, and pushes the HTML report.
