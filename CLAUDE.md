# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
# environment
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,agent-fallback]"
playwright install chromium
ollama pull qwen2.5:3b; ollama pull llama3.1:8b

# tests
pytest                                              # all
pytest tests/test_render.py                         # one file
pytest tests/test_render.py::test_first_name_substitution  # one test
pytest -k working_hours                             # by name

# lint
ruff check src tests
ruff format src tests

# the CLI itself (installed by `pip install -e`)
agent init | login | search | filter | enrich | compose | send | status | inspect
```

The full design rationale, risk posture, and verification checklist live in `C:\Users\chuch\.claude\plans\okay-help-to-create-cached-dragonfly.md` and in `README.md`.

## Architecture: stage-based pipeline driven by status

The agent is not a long-running orchestrator. Each CLI subcommand is a discrete stage that **reads prospects at one status, performs work, and advances them to the next status**. SQLite is the source of truth between stages. Every stage is idempotent — re-running it skips rows already past its target state.

```
search → filter → enrich → compose → send (linkedin)
                                  └→ send (email)
```

Status transitions (`agent.db.models.ProspectStatus`):

```
DISCOVERED
  └→ FILTERED_IN  ─→ ENRICHED ─→ COMPOSED ─→ LINKEDIN_SENT
  └→ FILTERED_OUT                          └→ EMAIL_SENT
                                           └→ DONE / FAILED
```

When adding a stage, follow the same pattern: query by source status, write changes inside `session_scope()`, advance status on success, emit `audit.record(...)`. Do not chain stages in code — keep them runnable independently from the CLI.

## Safety: three gates every live action must pass

Every action that touches LinkedIn or sends email is gated by three checks. If you add a new send path, wire all three:

1. **`safety.caps.assert_under_cap(action, settings)`** — raises `CapExceeded` if today's count of the action (`profile_visit` | `linkedin_message` | `email`) is at the configured daily cap. Counted by querying `ProfileVisit` and `OutreachEvent` for the local-tz calendar day.
2. **`safety.delay.assert_working_hours(settings)`** — raises `OutsideWorkingHours` outside the configured window. `human_sleep(window)` and `type_like_human(page, sel, text)` introduce the randomized delays themselves.
3. **`linkedin.detection.inspect_page(page).raise_if_blocked()`** — scans every loaded LinkedIn page for CAPTCHA/restriction/login-wall signals. Raises `LinkedInBlocked` — let it propagate up to the CLI; do not catch and retry.

**Dry-run is the default.** Every `send` command must require an explicit `--live` flag. In dry-run, render and persist drafts, emit `audit.record(..., dry_run=True)`, mark events `SKIPPED_DRY_RUN`, and never launch a browser or open SMTP.

## Module map (what to read when)

- **Config & DB** (`config.py`, `db/`): `load_settings()`, `get_engine()`, and `_sessionmaker()` are all `@lru_cache(maxsize=1)`. Tests that need a fresh DB or alternate config must clear those caches (`load_settings.cache_clear()`). `session_scope()` is a transactional context manager — commit on exit, rollback on exception.
- **LinkedIn surface** (`linkedin/`): the **fragile layer**. `search.py`, `profile.py`, `messaging.py` use CSS selectors that LinkedIn rotates via A/B tests. Each function tries multiple selector candidates in order — extend that list rather than replacing it. `browser.py` owns the single Playwright `launch_persistent_context` against `data/chrome_profile/`; that profile dir **is** the auth state, never delete it without warning the user.
- **LLM** (`llm/`): `client.chat()` is the only Ollama call site. Use `json_mode=True` + `parse_json()` for any prompt expecting structured output. Three model slots in config (`filter_model`, `personalize_model`, `parse_model`) so a fast small model handles bulk filtering while a larger one writes openers. `parse_contact.extract_contact()` does regex first, then LLM augmentation — keep it that way (regex catches the common cases; LLM only handles obfuscated emails like "name [at] domain").
- **Templates** (`templating.py`, `templates/`): Jinja with `StrictUndefined` — a missing variable raises, which is intentional. All templates take `first_name`, `opener`, and one of `from_name` / `signature`.
- **Mailer** (`mailer/`): the package is named `mailer/`, **not** `email/`, because `email` shadows Python's stdlib. Don't rename it back.
- **Audit** (`safety/audit.py`): `record()` writes to both the `audit_events` table and a loguru JSON sink at `data/logs/audit.log`. Every send and every state-changing browser action calls it. Audit failures must never raise — they log and continue.

## Conventions worth knowing before editing

- **Idempotent drafts:** `compose_linkedin_drafts` skips prospects with an existing LinkedIn `OutreachEvent`. `compose_email_drafts` creates **one event per (recipient email, sequence_number=1)** and dedups by `UniqueConstraint(prospect_id, channel, recipient_email, sequence_number)`. recipient_email is empty string for LinkedIn rows. Honor this when adding new outreach types.
- **Two senders share a code path shape:** `send_linkedin_drafts` (async, browser) and `send_email_drafts` (sync, SMTP) deliberately mirror each other — load drafted events, branch on dry-run, gate with caps, update status + sent_at, audit. New channels should follow the same shape.
- **Don't widen what runs in a session_scope:** LLM calls inside `compose_*_drafts` hold the SQLite transaction open across multi-second Ollama requests. Acceptable for v1 because SQLite is single-writer and we run interactively. If you parallelize, generate openers first and persist in a tighter transaction.
- **No retries on `LinkedInBlocked`:** if `detection.inspect_page` flags a page, the operation halts and the user must manually verify the account is unrestricted before re-running. Do not add automatic re-tries or workarounds.

## Email followups (3-email sequence)

`send_email_drafts` handles initial sends **and** scheduled followups together. The sequence is driven entirely by data on `OutreachEvent`:

- `sequence_number`: 1 = initial, 2/3/... = followups
- `due_at`: when a `SCHEDULED` followup becomes eligible to send. `None` = send immediately (initial drafts).
- `parent_event_id`: each followup links back to the initial event in the same chain. Used to (a) find the chain's `sent_at` lower-bound for IMAP reply search, (b) cascade `SKIPPED_REPLIED` across siblings, (c) copy the parent's `message_id` into `In-Reply-To` / `References` headers so Gmail threads the conversation.
- `message_id`: set by us at send time via `make_msgid()`. Persisted so followups can reference it.

**Followup status flow:**
```
DRAFTED (seq=1) ─send─→ SENT ──→ _schedule_followups() creates SCHEDULED rows (seq=2,3)
                                                              │
                              ─due_at passes, send command picks it up─→
                                                              │
                          reply-check → SENT  or  SKIPPED_REPLIED (cascades to siblings)
```

**Reply detection** (`mailer/reply_detector.py`) uses IMAP, not Gmail API. Uses the same App Password as SMTP. Returns `ReplyCheckError` on any failure — callers honor `followups.reply_detection.on_error` (`skip` = safer, `send` = optimistic). Never silently treat IMAP failure as "no reply".

**Re: subject normalization** is in `_normalize_subject_for_reply` — strips any number of leading `Re:` prefixes (case-insensitive) and adds exactly one. Use it whenever generating a followup subject.

**Subagents adding more followup steps** can simply add entries to `config.yaml#followups.email_sequence` — `_schedule_followups()` iterates the list and assigns `sequence_number` starting from 2. No code change required for an additional step beyond a new template file.

## Schema changes

There is no Alembic. `db/migrate.py` holds a `_COLUMN_ADDITIONS` list of `(table, column, ddl)` triples and applies any missing columns via `ALTER TABLE` on each `agent init` run. **When you add a column to an existing model, append the corresponding tuple to that list** — `create_all` alone won't add it to pre-existing databases.
