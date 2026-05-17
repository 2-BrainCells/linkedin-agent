from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.table import Table
from sqlalchemy import func, select

from agent.config import PROJECT_ROOT, load_settings
from datetime import datetime, timezone

from agent.db.migrate import migrate
from agent.db.models import (
    ContactInfo,
    OutreachChannel,
    OutreachEvent,
    OutreachStatus,
    Prospect,
    ProspectStatus,
)
from agent.db.session import session_scope
from agent.linkedin.browser import interactive_login
from agent.linkedin.messaging import compose_linkedin_drafts, send_linkedin_drafts
from agent.linkedin.profile import enrich_pending
from agent.linkedin.search import SearchQuery, run_search
from agent.llm.client import is_model_available
from agent.llm.filter import run_filter
from agent.mailer.sender import (
    MissingAppPassword,
    compose_email_drafts,
    send_email_drafts,
)
from agent.safety import audit
from agent.safety.caps import today_usage

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Local LinkedIn outreach agent.")


def _bootstrap() -> None:
    audit.configure_logging()


@app.command()
def init() -> None:
    """Scaffold .env from .env.example if missing, run migrations, check models."""
    _bootstrap()
    env = PROJECT_ROOT / ".env"
    example = PROJECT_ROOT / ".env.example"
    if not env.exists() and example.exists():
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        rprint(f"[green]created[/green] {env}")
    settings = load_settings()
    added = migrate()
    if added:
        rprint(f"[green]migrated[/green] new columns: {added}")
    rprint(f"[green]database[/green]: {settings.resolve_path(settings.paths.database)}")
    rprint(f"[green]chrome profile[/green]: "
           f"{settings.resolve_path(settings.linkedin.browser_profile_dir)}")
    if not settings.secrets.gmail_app_password:
        rprint("[yellow]Set GMAIL_APP_PASSWORD in .env before sending emails.[/yellow]")
    for model in [settings.ollama.filter_model, settings.ollama.personalize_model,
                  settings.ollama.parse_model]:
        ok = is_model_available(model)
        marker = "[green]ok[/green]" if ok else "[red]MISSING[/red]"
        rprint(f"ollama model {model}: {marker}")
    if settings.followups.enabled:
        steps = settings.followups.email_sequence
        rprint(f"[green]followups[/green]: {len(steps)} step(s) "
               f"({', '.join(f'+{s.delay_hours}h' for s in steps) or 'none configured'})")
        rd = settings.followups.reply_detection
        rprint(f"[green]reply detection[/green]: "
               f"{'enabled' if rd.enabled else 'disabled'} "
               f"({rd.imap_host}:{rd.imap_port}, on_error={rd.on_error})")


@app.command()
def login() -> None:
    """Open a headed Chrome window and wait for you to log into LinkedIn."""
    _bootstrap()
    asyncio.run(interactive_login())


@app.command()
def search(
    query: str = typer.Option(..., "--query", "-q", help="Keywords for LinkedIn people search."),
    location: str = typer.Option("", "--location", help="Optional location filter (regular search only)."),
    max_results: Optional[int] = typer.Option(None, "--max",
                                              help="Override config.search.max_results_per_query."),
) -> None:
    """Run a LinkedIn people search and store discovered prospects."""
    _bootstrap()
    settings = load_settings()
    if max_results:
        settings.search.max_results_per_query = max_results
    q = SearchQuery(keywords=query, location=location)
    result = asyncio.run(run_search(q, settings))
    rprint(result)


@app.command("filter")
def filter_cmd(
    criteria_file: Optional[Path] = typer.Option(None, "--criteria-file",
                                                  exists=True, dir_okay=False),
    criteria: Optional[str] = typer.Option(None, "--criteria"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Score DISCOVERED prospects against criteria; sets FILTERED_IN/OUT."""
    _bootstrap()
    text = None
    if criteria_file:
        text = criteria_file.read_text(encoding="utf-8")
    elif criteria:
        text = criteria
    report = run_filter(text, limit=limit)
    rprint({"evaluated": report.evaluated, "kept": report.kept,
            "dropped": report.dropped, "errors": report.errors})


@app.command()
def enrich(
    limit: Optional[int] = typer.Option(None, "--limit",
                                        help="Cap how many FILTERED_IN prospects to visit."),
) -> None:
    """Visit FILTERED_IN profiles and scrape Contact Info."""
    _bootstrap()
    report = asyncio.run(enrich_pending(limit=limit))
    rprint({"visited": report.visited, "enriched": report.enriched,
            "skipped": report.skipped, "blocked": report.blocked,
            "reason": report.reason})


@app.command()
def compose(
    channel: str = typer.Option("linkedin", "--channel",
                                help="linkedin | email | both"),
) -> None:
    """Generate openers and render templates into outreach_events as drafts."""
    _bootstrap()
    out: dict = {}
    if channel in ("linkedin", "both"):
        out["linkedin_drafted"] = compose_linkedin_drafts()
    if channel in ("email", "both"):
        out["email_drafted"] = compose_email_drafts()
    rprint(out)


@app.command()
def send(
    channel: str = typer.Option(..., "--channel", help="linkedin | email"),
    live: bool = typer.Option(False, "--live",
                              help="Actually send. Default is dry-run."),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Send drafted outreach events (initial emails + any due followups).

    DRY-RUN by default. For email channel, this also processes any followup
    events whose scheduled due time has passed.
    """
    _bootstrap()
    dry_run = not live
    if channel == "linkedin":
        out = asyncio.run(send_linkedin_drafts(dry_run=dry_run, limit=limit))
    elif channel == "email":
        try:
            out = send_email_drafts(dry_run=dry_run, limit=limit)
        except MissingAppPassword as e:
            raise typer.BadParameter(str(e))
    else:
        raise typer.BadParameter("--channel must be 'linkedin' or 'email'")
    rprint(out)


@app.command()
def followup(
    live: bool = typer.Option(False, "--live",
                              help="Actually send. Default is dry-run."),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Send ONLY due followup emails (skip new initial drafts).

    Useful for cron-style invocation that runs every few hours to fire any
    followups whose scheduled time has passed.
    """
    _bootstrap()
    try:
        out = send_email_drafts(dry_run=not live, limit=limit, only_followups=True)
    except MissingAppPassword as e:
        raise typer.BadParameter(str(e))
    rprint(out)


@app.command()
def status() -> None:
    """Show pipeline counts and today's cap usage."""
    _bootstrap()
    settings = load_settings()
    usage = today_usage(settings)
    rem = usage.remaining(settings)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        counts = {}
        for st in ProspectStatus:
            n = s.scalar(
                select(func.count(Prospect.id)).where(Prospect.status == st)
            ) or 0
            counts[st.value] = n
        emails_with_contact = s.scalar(
            select(func.count(ContactInfo.id)).where(ContactInfo.emails != [])
        ) or 0
        scheduled_total = s.scalar(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.status == OutreachStatus.SCHEDULED
            )
        ) or 0
        scheduled_due_now = s.scalar(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.status == OutreachStatus.SCHEDULED,
                OutreachEvent.due_at.isnot(None),
                OutreachEvent.due_at <= now,
            )
        ) or 0
        replied_count = s.scalar(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.status == OutreachStatus.SKIPPED_REPLIED
            )
        ) or 0
        sent_by_seq = {}
        for seq in (1, 2, 3):
            sent_by_seq[seq] = s.scalar(
                select(func.count(OutreachEvent.id)).where(
                    OutreachEvent.channel == OutreachChannel.EMAIL,
                    OutreachEvent.status == OutreachStatus.SENT,
                    OutreachEvent.sequence_number == seq,
                )
            ) or 0

    t = Table(title="Prospects by status")
    t.add_column("Status"); t.add_column("Count", justify="right")
    for k, v in counts.items():
        t.add_row(k, str(v))
    rprint(t)

    t2 = Table(title="Today's usage / remaining")
    t2.add_column("Bucket"); t2.add_column("Used", justify="right"); t2.add_column("Remaining", justify="right")
    t2.add_row("profile visits", str(usage.profile_visits), str(rem["profile_visits"]))
    t2.add_row("linkedin messages", str(usage.linkedin_sent), str(rem["linkedin_messages"]))
    t2.add_row("emails", str(usage.emails_sent), str(rem["emails"]))
    rprint(t2)

    t3 = Table(title="Email followups")
    t3.add_column("Bucket"); t3.add_column("Count", justify="right")
    t3.add_row("scheduled (total)", str(scheduled_total))
    t3.add_row("scheduled (due now)", str(scheduled_due_now))
    t3.add_row("skipped — replied", str(replied_count))
    t3.add_row("sent: initial (seq 1)", str(sent_by_seq[1]))
    t3.add_row("sent: followup 1 (seq 2)", str(sent_by_seq[2]))
    t3.add_row("sent: followup 2 (seq 3)", str(sent_by_seq[3]))
    rprint(t3)

    rprint(f"prospects with at least one email: [bold]{emails_with_contact}[/bold]")


@app.command()
def inspect(target: str = typer.Argument(..., help="Prospect ID or profile URL.")) -> None:
    """Show full record + last 5 outreach events for one prospect."""
    _bootstrap()
    with session_scope() as s:
        p: Prospect | None = None
        if target.isdigit():
            p = s.get(Prospect, int(target))
        else:
            p = s.scalar(select(Prospect).where(Prospect.profile_url == target.rstrip("/")))
        if not p:
            rprint("[red]not found[/red]")
            raise typer.Exit(1)
        rprint({
            "id": p.id, "name": p.full_name, "headline": p.headline,
            "company": p.current_company, "location": p.location,
            "status": p.status.value, "filter_score": p.filter_score,
            "filter_reason": p.filter_reason, "url": p.profile_url,
            "emails": (p.contact.emails if p.contact else []),
            "phone": (p.contact.phone if p.contact else ""),
        })
        events = list(s.scalars(
            select(OutreachEvent).where(OutreachEvent.prospect_id == p.id)
            .order_by(OutreachEvent.channel, OutreachEvent.sequence_number,
                      OutreachEvent.id.desc()).limit(10)
        ))
        for e in events:
            rprint({
                "channel": e.channel.value, "status": e.status.value,
                "seq": e.sequence_number, "recipient": e.recipient_email,
                "due_at": str(e.due_at) if e.due_at else None,
                "sent_at": str(e.sent_at) if e.sent_at else None,
                "subject": e.rendered_subject,
                "preview": e.rendered_body[:200],
            })


if __name__ == "__main__":  # pragma: no cover
    app()
