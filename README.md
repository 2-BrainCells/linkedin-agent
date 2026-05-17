# LinkedIn Agent

A locally-run LinkedIn outreach pipeline. Search → LLM-filter → enrich (scrape Contact Info) → compose with LLM-generated openers → send LinkedIn DM and cold email. Everything runs on your machine: Playwright drives a real Chrome window using your logged-in session, Ollama provides the LLM, your own Gmail account sends the emails.

> **LinkedIn ToS warning.** Automated access violates LinkedIn's User Agreement. Accounts can be restricted or banned. This tool ships with hard daily caps, randomized human-like delays, working-hours gates, dry-run-by-default sending, and a CAPTCHA/warning watcher — but **residual risk is on you**. Start small. If you ever see a restriction warning, stop.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally
- Google account with 2FA enabled (for the Gmail App Password)
- ~2 GB free disk for the Chromium binary and persistent profile

## Setup

```powershell
# from D:\LinkedIn Agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -e ".[dev,agent-fallback]"
playwright install chromium

# pull the LLM models referenced in config.yaml
ollama pull qwen2.5:3b
ollama pull llama3.1:8b

# create your .env from the template and set your Gmail App Password
agent init
# then edit .env: GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
```

Generate the Gmail App Password at <https://myaccount.google.com/apppasswords> (requires 2FA enabled).

## One-time LinkedIn login

```powershell
agent login
```

This opens a headed Chrome window pointed at LinkedIn. Log in manually (handle 2FA), then close the window. The session is saved to `data/chrome_profile/` and reused for every subsequent run.

## Pipeline (run each stage, in order)

```powershell
# 1. Discover prospects
agent search --query "founder AI startup" --max 25

# 2. Score them with the LLM (criteria comes from config.yaml or --criteria-file)
agent filter

# 3. Visit kept profiles and scrape Contact Info
agent enrich --limit 10

# 4. Generate openers and render templates as drafts
agent compose --channel linkedin
agent compose --channel email

# 5. Send. Dry-run by default — preview goes to data/logs/audit.log.
agent send --channel linkedin            # dry-run
agent send --channel linkedin --live     # actually send

agent send --channel email               # dry-run (initial + any due followups)
agent send --channel email --live        # actually send

# After sending an initial email, two followups are scheduled automatically.
# Run this on a cron/Task Scheduler timer to fire due followups:
agent followup --live                    # send only followups whose due_at has passed

# Inspect state anytime
agent status                             # includes followup counts + replies skipped
agent inspect <prospect_id|profile_url>
```

## Configuration

All behavior knobs live in `config.yaml`:

- `caps`: daily limits for profile visits / DMs / emails
- `delays`: gaussian delays between actions + a working-hours window
- `ollama`: model names for filter / personalize / parse
- `templates`: paths to message + email body + subject + signature
- `filter.criteria`: free-form description of who you want to keep
- `followups`: email followup cadence (default 24h then 72h), templates, IMAP reply-detection settings

Secrets go in `.env` (gitignored). Only `GMAIL_APP_PASSWORD` is required (used for both SMTP send and IMAP reply detection).

## Verification checklist

Run all ten before scaling up.

1. `ollama list` shows the configured models. `agent init` reports them as ok.
2. `agent login` succeeds — you can manually browse LinkedIn in the persistent profile.
3. `agent search --query "AI engineer" --max 5` — DB has 5 prospects with headlines.
4. `agent filter` — inspect `filter_reason` on 2-3 rows.
5. `agent enrich --limit 3` — Chrome opens, profiles are visited with visible delay, `contact_info` rows populated.
6. `agent compose --channel linkedin` — `agent inspect` shows opener and rendered body.
7. `agent send --channel linkedin` (default dry-run) — zero messages on LinkedIn, previews logged.
8. `agent send --channel linkedin --live` — send exactly one to a friend who's consented.
9. `agent send --channel email` dry-run + `--live` to yourself first, then a consented contact.
10. Temporarily set `caps.linkedin_messages_per_day: 1`, try a second send → expect `skipped_cap`.

## Layout

```
src/agent/
  cli.py                # Typer entry points
  config.py             # Pydantic settings + YAML loader
  templating.py         # Jinja rendering
  db/                   # SQLAlchemy models + session
  linkedin/             # browser, search, profile, messaging, detection
  llm/                  # Ollama client, prompts, filter, personalize, parse_contact
  mailer/               # Gmail SMTP send + email rendering
  safety/               # caps, delays, audit
  pipeline/             # (Phase 2) orchestrator
templates/              # editable Jinja templates
data/                   # gitignored: SQLite DB, Chrome profile, logs
```

## Phase 2

A Streamlit dashboard reading the same SQLite DB. Not built yet.

## License

MIT.
