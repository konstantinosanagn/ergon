"""Command-line interface (typer + rich). Expanded in Phase 2; functional skeleton here."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .exceptions import ErgonTrackerError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="ergon-tracker — unified job-fetching SDK CLI",
)
console = Console()
err_console = Console(stderr=True)


@app.command()
def version() -> None:
    """Print the ergon-tracker version."""
    console.print(f"ergon-tracker {__version__}")


@app.command()
def sponsors(
    query: str = typer.Argument("", help="filter by employer name (blank = biggest sponsors)"),
    limit: int = typer.Option(25, "--limit", "-n"),
) -> None:
    """Browse known H-1B visa sponsors (US DoL LCA data), ranked by filing volume."""
    from .extract.visa import load_sponsor_index, search_sponsors

    if len(load_sponsor_index()) == 0:
        err_console.print(
            "[yellow]No H-1B sponsor index built yet.[/] Run scripts/build_h1b_sponsors.py "
            "against the DoL LCA disclosure file(s)."
        )
        raise typer.Exit(code=1)
    rows = search_sponsors(query or None, limit)
    table = Table(title=f"H-1B sponsors{f' matching {query!r}' if query else ''}")
    table.add_column("employer", style="cyan")
    table.add_column("filings", justify="right", style="yellow")
    table.add_column("last filed", style="green")
    for r in rows:
        table.add_row(str(r["name"]).title(), str(r["filings"]), str(r["last_filed"] or "—"))
    console.print(table)


@app.command()
def sources() -> None:
    """List registered providers and their availability."""
    from .providers.base import iter_providers, load_builtins, load_plugins

    load_builtins()
    load_plugins()
    providers = iter_providers()
    table = Table(title="ergon-tracker providers")
    table.add_column("name", style="cyan")
    table.add_column("type")
    for p in providers:
        table.add_row(p.name, type(p).__module__)
    if not providers:
        err_console.print("[yellow]No providers registered yet (Phase 1 in progress).[/]")
        return
    console.print(table)


@app.command()
def resolve(target: str) -> None:
    """Detect which ATS a company/careers URL uses, and its board token."""
    from .sync import ErgonTracker

    try:
        resolution = ErgonTracker().resolve(target)
    except (ErgonTrackerError, NotImplementedError, ImportError) as exc:
        err_console.print(f"[red]resolve failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(getattr(resolution, "__dict__", {"result": str(resolution)})))


@app.command()
def search(
    keywords: str = typer.Argument(..., help="search keywords"),
    company: list[str] | None = typer.Option(
        None,
        "--company",
        "-c",
        help="target a company by domain/careers URL (repeatable, e.g. -c stripe.com). "
        "Omit to search the whole bundled registry (much slower).",
    ),
    location: str | None = typer.Option(None, "--location", "-l"),
    remote: bool = typer.Option(False, "--remote"),
    level: str | None = typer.Option(None, "--level", help="intern/senior/staff/manager/..."),
    include_unknown_level: bool = typer.Option(
        False, "--include-unknown-level", help="keep roles with no inferable level when filtering"
    ),
    sector: str | None = typer.Option(None, "--sector", help='e.g. "Fintech", "AI/ML"'),
    include_unknown_sector: bool = typer.Option(
        False, "--include-unknown-sector", help="keep roles with no detected sector when filtering"
    ),
    country: str | None = typer.Option(None, "--country", help="USA/US -> United States, UK, ..."),
    city: str | None = typer.Option(
        None, "--city", help='metro-aware (e.g. "New York" also matches NYC boroughs)'
    ),
    salary_min: float | None = typer.Option(None, "--salary-min"),
    salary_max: float | None = typer.Option(None, "--salary-max"),
    salary_currency: str | None = typer.Option(
        None,
        "--salary-currency",
        help="e.g. USD — when a salary bound is set, drop other currencies",
    ),
    min_years: int | None = typer.Option(
        None, "--min-years", help="minimum years of experience required"
    ),
    max_years: int | None = typer.Option(
        None, "--max-years", help='maximum years of experience (e.g. --max-years 2 for "new grad")'
    ),
    include_unknown_years: bool = typer.Option(
        True, "--include-unknown-years/--strict-years", help="keep roles with no stated years"
    ),
    employment_type: str | None = typer.Option(
        None, "--employment-type", help="full_time/part_time/contract/internship/temporary"
    ),
    posted_within_days: int | None = typer.Option(
        None, "--posted-within-days", help="only roles posted within the last N days"
    ),
    visa_sponsor: bool = typer.Option(
        False, "--visa-sponsor", help="only employers known to sponsor H-1B (DoL LCA data)"
    ),
    sponsorship: bool = typer.Option(
        False,
        "--sponsorship",
        help="hide postings that explicitly refuse visa sponsorship (keeps offered + unstated)",
    ),
    infer_level: bool = typer.Option(
        False,
        "--infer-level",
        help="derive level from years of experience when title has no marker",
    ),
    semantic: bool = typer.Option(
        False,
        "--semantic",
        help="rank by meaning via embeddings (needs: pip install 'ergon-tracker[semantic]')",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json", help="emit JSON instead of a table"),
) -> None:
    """Search jobs across all sources."""
    from datetime import datetime, timedelta, timezone

    from .models import EmploymentType, JobLevel
    from .sync import search as run_search

    posted_after = (
        datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        if posted_within_days and posted_within_days > 0
        else None
    )
    try:
        result = run_search(
            keywords,
            companies=company or None,
            location=location,
            remote=remote or None,
            level=JobLevel(level) if level else None,
            include_unknown_level=include_unknown_level,
            sector=sector,
            include_unknown_sector=include_unknown_sector,
            country=country,
            city=city,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=salary_currency,
            min_years=min_years,
            max_years=max_years,
            include_unknown_years=include_unknown_years,
            employment_type=EmploymentType(employment_type) if employment_type else None,
            posted_after=posted_after,
            visa_sponsor=True if visa_sponsor else None,
            sponsorship_offered=True if sponsorship else None,
            infer_level_from_experience=infer_level,
            semantic=semantic,
            limit=limit,
        )
    except ValueError as exc:
        err_console.print(f"[red]invalid level/employment-type:[/] {exc}")
        raise typer.Exit(code=1) from exc
    except (ErgonTrackerError, NotImplementedError, ImportError) as exc:
        err_console.print(f"[red]search failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if as_json:
        console.print_json(json.dumps(result.to_dicts()))
    else:
        table = Table(title=f"{len(result)} jobs")
        table.add_column("score", style="yellow", justify="right")
        table.add_column("company", style="cyan")
        table.add_column("title")
        table.add_column("location")
        table.add_column("level", style="magenta")
        table.add_column("salary", style="green")
        table.add_column("sector", style="green")
        table.add_column("source", style="dim")
        for job in result.jobs:
            loc = job.locations[0].as_text() if job.locations else ""
            score = f"{job.score:.1f}" if job.score is not None else "—"
            salary = job.salary.as_text() if job.salary else ""
            table.add_row(
                score,
                job.company,
                job.title,
                loc,
                job.level.value,
                salary,
                job.sector or "",
                job.source,
            )
        console.print(table)
        # Surface index freshness so a CLI user knows how current the snapshot is (matches the
        # as_of reported via the SDK/MCP). Only the index source carries it; live sources don't.
        fresh = next((h.as_of for h in result.health if h.source == "index" and h.as_of), None)
        if fresh:
            console.print(f"[dim]served from index snapshot {fresh}[/]")
        for h in result.failed_sources:
            err_console.print(f"[yellow]source {h.source} failed:[/] {h.error}")


if __name__ == "__main__":
    app()
