# LinkedIn Agent — Complete Setup & Feature Guide

This document explains what the agent does, how to set it up from scratch, and how every feature works and connects to the next. Written to be understood without prior technical knowledge of the individual components.

---

## What this agent does

The LinkedIn Agent is a pipeline that automates LinkedIn outreach in stages. You give it a search term (like "AI startup founder"), it:

1. **Searches** LinkedIn and collects matching profiles
2. **Filters** them using a local AI model (so only the right people remain)
3. **Visits** each profile and scrapes their public Contact Info (email, phone, etc.)
4. **Writes** a personalized opening line for each person using the AI
5. **Sends** them a LinkedIn direct message — with their name and that opener inserted
6. **Emails** them if a public email address was found, using your Gmail

Everything runs on your computer. No SaaS, no cloud subscription, no LinkedIn API key. The AI runs locally via Ollama. The browser it uses is a real Chrome window, logged into your actual LinkedIn account.

---

## Before you start: what you need

| Requirement | Why |
|---|---|
| Python 3.11 or newer | The agent is written in Python |
| [Ollama](https://ollama.com/) installed and running | Runs the AI models locally |
| A Google account with 2-Step Verification enabled | Needed to generate a Gmail App Password for sending emails |
| Your existing LinkedIn account | The agent logs in as you, in real Chrome |
| ~3 GB of free disk space | For Chromium, the AI models, and the database |

---

## Step-by-step setup

### 1. Create a Python environment

Open PowerShell in the `D:\LinkedIn Agent` folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

You'll see `(.venv)` in your prompt — this means Python is now isolated for this project.

### 2. Install the agent and its dependencies

```powershell
pip install -e ".[dev,agent-fallback]"
playwright install chromium
```

`playwright install chromium` downloads the browser the agent uses. This can take a few minutes and uses ~250 MB.

### 3. Pull the AI models

```powershell
ollama pull qwen2.5:3b
ollama pull llama3.1:8b
```

These are the two AI models used:
- **`qwen2.5:3b`** — small and fast. Used for reading headlines and deciding yes/no (filtering), and for cleaning up scraped contact text.
- **`llama3.1:8b`** — larger and more capable. Used for writing the personalized opening lines.

Ollama must be running in the background before you use the agent. It starts automatically on Windows after installation, but you can also open the Ollama app manually.

### 4. Set up the configuration and secrets

```powershell
agent init
```

This command:
- Creates a `.env` file (from `.env.example`) where your Gmail password goes
- Creates the local database and folder structure in `data/`
- Checks that your AI models are available and reports if any are missing

After running it, open the `.env` file and fill in your Gmail App Password:

```
GMAIL_APP_PASSWORD=your16charpassword
```

To generate a Gmail App Password:
1. Go to your Google Account → Security → 2-Step Verification → App passwords
2. Name it "LinkedIn Agent" and generate
3. Copy the 16-character password (no spaces) into `.env`

### 5. Log in to LinkedIn — once, manually

```powershell
agent login
```

This opens a real Chrome window pointed at the LinkedIn login page. Log in normally — handle 2FA if prompted. The session (cookies, login state) is saved to `data/chrome_profile/` on your disk. **You only need to do this once.** Every subsequent command reuses that saved session. Only if LinkedIn signs you out (rarely) do you need to run `agent login` again.

> **Do not delete `data/chrome_profile/`.** That folder is what keeps you logged in.

---

## How each feature works

### Search

```powershell
agent search --query "VP Engineering fintech" --max 25
```

**What it does:** Opens Chrome as you, goes to LinkedIn People Search (or Sales Navigator if you have a subscription), searches for your keywords, and scrolls through the results. For each person found it records:
- Full name and first name
- LinkedIn profile URL
- Headline (the text below their name)
- Current company and title
- Location

**Where results go:** Into the local database (`data/agent.db`) as "discovered" prospects. Nothing is sent, nothing is visited in depth — just the search result card data.

**Sales Navigator:** If your LinkedIn account has a Sales Navigator subscription, the agent detects it automatically and uses it (better filters, more results). If not, it falls back to the regular people search.

**Important:** LinkedIn limits how many search results you can see per day on free accounts (roughly 1,000 per month across all searches). The `--max` flag caps how many results are saved per run.

---

### Filter

```powershell
agent filter
```

**What it does:** Takes every "discovered" prospect from the database and asks the AI: *"Does this person match my criteria?"*

For each person, the AI sees:
- Their headline
- Their current title and company
- Your targeting criteria (from `config.yaml`)

It scores them 0–10 and returns a `yes/no` decision with a one-sentence reason. Prospects scoring 6 or above get marked `filtered_in`; the rest get marked `filtered_out`.

**The criteria** live in `config.yaml` under `filter.criteria`. Edit them to match your campaign:

```yaml
filter:
  criteria: |
    Keep founders, CTOs, and senior engineers at AI startups (Seed–Series B).
    Drop recruiters, students, sales, and anyone whose headline is vague.
  min_score: 6
```

You can also override the criteria per-run:
```powershell
agent filter --criteria "Keep only founders of SaaS companies, max 50 employees"
```
or point to a text file:
```powershell
agent filter --criteria-file my-criteria.txt
```

**How it connects to the next stage:** Only `filtered_in` prospects move forward. `filtered_out` prospects stay in the database but are skipped by every subsequent command.

---

### Enrich

```powershell
agent enrich --limit 10
```

**What it does:** For each `filtered_in` prospect, the agent:
1. Opens their LinkedIn profile in Chrome (as you, with delays so it looks like normal browsing)
2. Reads the "About" section (the bio text)
3. Clicks "Contact info" and reads the modal that appears
4. Extracts email addresses, phone number, Twitter handle, and website

**The contact info extraction works in two passes:**
- First, a regex scan picks up obvious things like `john@company.com`
- Then the AI reads the raw text to catch obfuscated emails people write to avoid scrapers, like `john [at] company dot com` → converts to `john@company.com`

**Safety during enrichment:**
- The agent checks your daily profile-visit cap before each visit (default: 25/day). If you'd exceed the cap, it stops.
- It waits a randomized human-like delay between each profile visit (roughly 5–11 seconds, varied so there's no machine-like pattern).
- If it detects that LinkedIn is showing a warning ("you've been viewing many profiles"), a CAPTCHA, or a login wall — it stops immediately and does not continue.

**Where results go:** Into the `contact_info` table. The prospect's status is advanced to `enriched`.

**The `--limit` flag** lets you cap how many profiles are visited in one run. Useful if you want to spread visits over multiple days (e.g., `--limit 10` twice = 20 profiles without hitting the 25/day cap in one go).

---

### Compose

```powershell
agent compose --channel linkedin
agent compose --channel email
agent compose --channel both
```

**What it does:** For each `enriched` prospect, the agent:
1. Sends their name, headline, and About text to the larger AI model
2. Receives a single personalized opening sentence (1–2 sentences, under 25 words)
3. Inserts that opener into your message template, along with their first name

**Example result for a prospect with headline "Co-founder @ FinanceAI | Ex-Stripe":**

> Hi Priya,
>
> Your work building financial infrastructure at FinanceAI after Stripe caught my eye — the intersection of compliance and ML is exactly the problem space I'm thinking about.
>
> I'm building tools that help small AI teams ship faster — would love to compare notes for 15 minutes if you're open to it. No pitch.
>
> Best,
> Abhinav

The opener line is AI-generated. The rest of the message comes from your template file (`templates/linkedin_message.md`).

**Your templates** (edit these to match your actual message):
- `templates/linkedin_message.md` — the LinkedIn DM body
- `templates/email_outreach.md` — the email body
- `templates/email_subject.txt` — the email subject line
- `templates/signature.txt` — your email signature

Templates use `{{ first_name }}`, `{{ opener }}`, `{{ from_name }}`, and `{{ signature }}` as placeholders.

**Important:** Compose is separate from Send intentionally. After composing, you can run `agent inspect <profile_url>` to read the drafted message before anything is sent.

**For email compose:** A separate draft event is created for each email address found in Contact Info. If someone has two emails, two draft events are created.

---

### Send (LinkedIn)

```powershell
agent send --channel linkedin          # dry-run (default — NOTHING is sent)
agent send --channel linkedin --live   # actually sends the messages
```

**What dry-run does:** Reads all drafted LinkedIn messages, prints a preview of each to the audit log (`data/logs/audit.log`), marks them as `skipped_dry_run` in the database, and exits. No browser is opened. This is the default.

**What `--live` does:**
1. Opens Chrome (as you)
2. Navigates to each prospect's profile
3. Clicks the "Message" button
4. Types the drafted message — with realistic per-character timing variation, so it looks like a human typing
5. Clicks Send
6. Marks the event as `sent` in the database

**Safety during sending:**
- Checks the daily LinkedIn message cap before each send (default: 15/day)
- Waits a randomized delay between sends (roughly 30–60 seconds)
- Runs the detection check on each page load — stops the whole run if LinkedIn shows any warning
- If capped, remaining messages are marked `skipped_cap` and left as drafts for the next day

---

### Send (Email)

```powershell
agent send --channel email          # dry-run
agent send --channel email --live   # actually sends via Gmail
```

**What it does:** For each prospect with a drafted email event, sends the email from your Gmail account via SMTP (the same protocol Gmail uses for standard email sending). Uses STARTTLS encryption — your password is never sent in plaintext.

This command sends both **initial** emails (newly drafted) **and** any **followup** emails whose scheduled time has arrived. See the next section for the followup system.

**Deduplication:** If you accidentally run `send --channel email --live` twice, it does not send duplicate emails. The database tracks which `(prospect, email address, sequence)` triples have been sent.

**Gmail App Password explained:** Gmail requires a special 16-character "App Password" (instead of your main Google password) for this type of SMTP access. This is a security feature — if the App Password is ever compromised, you can revoke just it without affecting your Google account. It's generated at `myaccount.google.com/apppasswords`.

---

### Followups (the 3-email sequence)

Every initial email automatically gets two followups scheduled — by default, **24 hours** after the initial, then **72 hours** after the first followup. So each prospect gets up to 3 emails:

| Email | When | Template | Subject |
|---|---|---|---|
| Initial | Immediately on `send --channel email --live` | `templates/email_outreach.md` | from `email_subject.txt` |
| Followup 1 | +24h after initial | `templates/email_followup_1.md` | `Re: <original subject>` |
| Followup 2 | +72h after followup 1 (96h after initial) | `templates/email_followup_2.md` | `Re: <original subject>` |

**Same Gmail thread.** Followups carry `In-Reply-To` and `References` headers pointing to the initial email's Message-ID. Gmail (and most other email clients) groups them into a single conversation thread for the recipient.

**How the schedule actually works:**

1. You run `agent send --channel email --live`.
2. The initial email is sent. Its database event gets `sequence_number=1`, `status=sent`, and a `sent_at` timestamp.
3. Immediately after, the system creates two more events for the same prospect:
   - `sequence_number=2`, `status=scheduled`, `due_at = sent_at + 24h`, body pre-rendered from `email_followup_1.md`
   - `sequence_number=3`, `status=scheduled`, `due_at = (sent_at + 24h) + 72h = sent_at + 96h`, body from `email_followup_2.md`
4. Later, when you run `agent send --channel email --live` (or `agent followup --live`), the system looks for `status=scheduled` events whose `due_at` is in the past, and sends them.

**Important:** The agent is not a daemon — it doesn't wake itself up at +24h to send. **You need to run the command** (or schedule it via Task Scheduler / cron). Recommended cadence: run `agent followup --live` every 1–4 hours during the day. See "Automating followups" below.

**The `agent followup` command:**

```powershell
agent followup            # dry-run preview of which followups are due
agent followup --live     # actually send any due followups (no new initials)
```

This is identical to `agent send --channel email --live` except it skips brand-new initial drafts — handy for cron-style automation when you only want to fire schedules.

**Reply detection (auto-stop on reply):**

Before sending each followup, the agent connects to your Gmail inbox via IMAP and asks: *"Has this person sent me any email since I sent the initial?"* If yes, the followup is **canceled** (marked `skipped_replied`), and any later followups in the same sequence are also canceled.

This means: someone replies "thanks, not interested" → they don't get two more pestering followups.

**Reply detection requirements:**
- IMAP must be enabled in Gmail (it's on by default for personal accounts).
- The same `GMAIL_APP_PASSWORD` you use for sending is reused for IMAP.
- The agent only checks your INBOX. If a reply is auto-archived by a filter to another label, it might be missed.

**If IMAP is unreachable** (network issue, etc.): controlled by `followups.reply_detection.on_error` in `config.yaml`:
- `skip` (default, safe): don't send the followup this run; try again next run.
- `send`: send anyway, accepting that you might message someone who replied.

**Configuring delays:** Change them in `config.yaml`:

```yaml
followups:
  email_sequence:
    - delay_hours: 24    # change to 48 or whatever
      template: ./templates/email_followup_1.md
    - delay_hours: 72
      template: ./templates/email_followup_2.md
```

You can add a third (or fourth, or fifth) followup by appending more entries — and adding a corresponding template file.

**Disabling followups entirely:**

```yaml
followups:
  enabled: false
```

With this off, `agent send --channel email --live` sends only the initial email and never schedules followups.

**Editing followups for one prospect:** Open `data/agent.db` with [DB Browser for SQLite](https://sqlitebrowser.org/). Find the relevant `outreach_events` rows (filter by `prospect_id` and `channel='email'`). You can:
- Change a `due_at` to push a followup later
- Set `status='skipped_replied'` to cancel a followup
- Edit `rendered_body` to customize one specific followup

**Automating followups (Windows Task Scheduler):**

1. Open Task Scheduler → Create Basic Task
2. Name: "LinkedIn Agent Followups"
3. Trigger: Daily, recur every 1 day, repeat every 2 hours for 24 hours
4. Action: Start a program
5. Program: `D:\LinkedIn Agent\.venv\Scripts\agent.exe`
6. Arguments: `followup --live`
7. Start in: `D:\LinkedIn Agent`

The agent will check for due followups every 2 hours and send anything ready. Combine with the working-hours gate in `config.yaml` to keep sends inside business hours.

---

## The safety system — why it exists and how it works

LinkedIn's Terms of Service prohibit automated access. Accounts that behave like bots (visiting 200 profiles in 10 minutes, sending identical messages instantly) get restricted or permanently banned.

The agent has four overlapping safeguards:

### 1. Daily caps (hard limits)

Set in `config.yaml`:
```yaml
caps:
  profile_visits_per_day: 25
  linkedin_messages_per_day: 15
  emails_per_day: 40
```

These are counted against the local calendar day (in your timezone). Counted by looking at the database — how many profile visits occurred today? How many LinkedIn DMs were sent? The moment you'd exceed a cap, that action is skipped and flagged `skipped_cap`.

### 2. Human-like delays

Set in `config.yaml`:
```yaml
delays:
  between_profile_visits:
    mean_seconds: 8
    stdev_seconds: 3
    min_seconds: 3
  between_messages:
    mean_seconds: 45
    stdev_seconds: 15
    min_seconds: 20
```

The actual delay each time is sampled from a bell curve (gaussian distribution) around the mean. A human browsing LinkedIn is not precisely 8 seconds between every click — sometimes 5, sometimes 12. The agent mimics this variability. The `min_seconds` floor prevents the bell curve from producing impossibly short waits.

Typing is also randomized: each character in a message is typed with a slightly different delay (40–140 ms per character), making keystrokes look human.

### 3. Working-hours window

```yaml
delays:
  working_hours:
    start: "09:00"
    end: "18:00"
    tz: "Asia/Kolkata"
    enforce: true
```

If `enforce: true`, the agent refuses to open browsers or send messages outside this time window. LinkedIn activity at 3am looks unusual. Set `enforce: false` to disable this gate.

### 4. Detection watcher

Every time Chrome loads a LinkedIn page, the agent scans the page content for known warning signals:
- "Your account has been restricted"
- "Please complete this security check" (CAPTCHA)
- "You've reached the weekly invitation limit"
- Login redirects (means your session expired)
- Captcha iframes

If any of these appear, the agent **immediately stops**, does not continue the current run, and logs what it saw. You then check LinkedIn manually, resolve whatever LinkedIn is asking for, and re-run when clear.

### 5. Dry-run is always the default

Every `send` command does nothing for real unless you pass `--live`. You can run the entire pipeline — search, filter, enrich, compose, send (dry-run) — and read exactly what would happen without touching any LinkedIn message or sending any email. Check the audit log:

```powershell
cat data\logs\audit.log
```

---

## The database — what gets saved

Everything is stored in `data/agent.db`, a single SQLite file you can open with any SQLite viewer (like [DB Browser for SQLite](https://sqlitebrowser.org/)):

| Table | What's in it |
|---|---|
| `prospects` | One row per person. Name, headline, status, filter score, about text |
| `contact_info` | Emails, phone, Twitter, website — one row per prospect |
| `outreach_events` | Each drafted/sent/scheduled message. One row per (prospect, channel, email, sequence). Includes `due_at`, `parent_event_id`, `message_id` for the followup system. |
| `profile_visits` | Every profile page the agent visited, with timestamp |
| `audit_events` | Every significant action the agent took, with whether it was a dry-run |

The `agent status` command reads these tables and prints a summary:
```powershell
agent status
```

The `agent inspect` command shows the full record for one person:
```powershell
agent inspect https://linkedin.com/in/their-handle
agent inspect 42      # by database ID
```

---

## A typical campaign run (day-by-day)

**Day 1 — setup and discovery:**
```powershell
agent search --query "founder AI startup London" --max 50
agent filter
agent status    # see how many passed filter
```

**Day 2 and 3 — enrichment (spread over days to stay under the 25/day cap):**
```powershell
agent enrich --limit 10   # run once each day
```

**Day 4 — compose and review:**
```powershell
agent compose --channel both
agent inspect <url>    # read a few drafts to check quality
# edit templates/linkedin_message.md if you don't like the tone
agent compose --channel both    # re-run to regenerate (only for un-composed prospects)
```

**Day 5 and onward — send (spread over days):**
```powershell
agent send --channel linkedin          # dry-run first
cat data\logs\audit.log               # read the previews
agent send --channel linkedin --live  # when happy
agent send --channel email --live
agent status                          # check sent counts
```

---

## Configuration quick reference

`config.yaml` is the main control file. Key settings:

| Setting | What it controls |
|---|---|
| `ollama.filter_model` | Which AI model reads headlines and filters (default: `qwen2.5:3b`) |
| `ollama.personalize_model` | Which AI model writes openers (default: `llama3.1:8b`) |
| `caps.*` | Daily action limits |
| `delays.working_hours` | Time window + timezone when sending is allowed |
| `search.max_results_per_query` | How many search results to save per `agent search` run |
| `filter.criteria` | Free-text description of who to keep (the AI follows this) |
| `filter.min_score` | Minimum AI score (0–10) to include a prospect (default: 6) |
| `email.from_address` | Your Gmail address |
| `email.from_name` | Your name as it appears in sent emails |

Secrets (passwords) go in `.env`, never in `config.yaml`. `.env` is gitignored and never committed.

---

## Common problems and what to do

**"Ollama model missing" on `agent init`:**
Run `ollama pull qwen2.5:3b` and `ollama pull llama3.1:8b`. Make sure Ollama is running (open the Ollama app or run `ollama serve` in another terminal).

**Chrome opens but the agent says it's not logged in:**
Run `agent login` again. Log in manually when the window opens.

**LinkedIn shows a CAPTCHA and the agent stops:**
Solve the CAPTCHA manually in the Chrome window, then run `agent enrich` or `agent send` again. The agent will pick up where it left off (already-enriched prospects are skipped).

**`agent send --channel email --live` fails with "GMAIL_APP_PASSWORD not set":**
Open `.env` and fill in `GMAIL_APP_PASSWORD=` with the 16-character password from Google.

**Filter scores seem wrong (AI keeps wrong people):**
Edit `filter.criteria` in `config.yaml`. Be specific — the more concrete the description, the better the AI does. Example: *"Keep CTOs and VP Engineering at companies with 10–200 employees in the US. Drop anyone at Big Tech (Google, Meta, Microsoft, Amazon, Apple). Drop recruiters and anyone in sales."*

**You want to re-enrich someone (maybe their contact info updated):**
The agent skips prospects already at `enriched` status or beyond. To re-run enrichment for a specific person, open `data/agent.db` in a SQLite viewer and change their `status` back to `filtered_in`.

---

## How the pieces talk to each other (summary diagram)

```
  You
   │
   ├─ agent search ──────→ [discovers prospects]
   │                              │
   ├─ agent filter ──────→ [scores & keeps/drops]
   │                              │
   ├─ agent enrich ──────→ [visits profiles, scrapes emails]
   │         │                    │
   │    [Chrome browser]   [contact_info table]
   │    [Detection watcher]        │
   │    [Delay system]             │
   │    [Cap checker]              │
   │                               │
   ├─ agent compose ─────→ [Ollama writes opener]
   │         │             [Jinja renders full message]
   │    [llama3.1:8b]      [outreach_events table: DRAFTED]
   │                               │
   ├─ agent send linkedin → [Chrome types & sends DM]  ← --live required
   │                        [outreach_events: SENT]
   │
   └─ agent send email ──→ [Gmail SMTP sends initial]  ← --live required
                           [outreach_events: SENT seq=1]
                                  │
                                  └→ [schedules followup 1 (+24h) and 2 (+96h)]
                                     [outreach_events: SCHEDULED seq=2,3]
                                              │
                          [later runs of `agent followup --live`]
                                              │
                          [IMAP reply check — skip if recipient replied]
                                              │
                          [Gmail SMTP sends followup] [with In-Reply-To header]
                                              │
                          [outreach_events: SENT, or SKIPPED_REPLIED]

  At every step:
    audit.log records the action (always)
    SQLite records the state (always)
    Caps / delays / detection gate every browser action
```

The database (`data/agent.db`) is the backbone. Every command reads from it and writes back to it. That's why stages are independent — you can stop after `enrich`, come back tomorrow, and `compose` will pick up exactly where you left off.
