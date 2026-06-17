"""Command-line interface (typer + rich). Expanded in Phase 2; functional skeleton here."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .exceptions import JobSpineError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="ergon-tracker — unified job-fetching SDK CLI",
)
console = Console()
err_console = Console(stderr=True)


@app.command()
def version() -> None:
    """Print the ergon_tracker version."""
    console.print(f"ergon-tracker {__version__}")


@app.command()
def sources() -> None:
    """List registered providers and their availability."""
    from .providers.base import iter_providers, load_builtins, load_plugins

    load_builtins()
    load_plugins()
    providers = iter_providers()
    table = Table(title="ergon_tracker providers")
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
    from .sync import JobSpine

    try:
        resolution = JobSpine().resolve(target)
    except (JobSpineError, NotImplementedError, ImportError) as exc:
        err_console.print(f"[red]resolve failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(getattr(resolution, "__dict__", {"result": str(resolution)})))


@app.command()
def search(
    keywords: str = typer.Argument(..., help="search keywords"),
    location: str | None = typer.Option(None, "--location", "-l"),
    remote: bool = typer.Option(False, "--remote"),
    level: str | None = typer.Option(None, "--level", help="intern/senior/staff/manager/..."),
    sector: str | None = typer.Option(None, "--sector", help='e.g. "Fintech", "AI/ML"'),
    country: str | None = typer.Option(None, "--country"),
    salary_min: float | None = typer.Option(None, "--salary-min"),
    salary_max: float | None = typer.Option(None, "--salary-max"),
    infer_level: bool = typer.Option(
        False,
        "--infer-level",
        help="derive level from years of experience when title has no marker",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json", help="emit JSON instead of a table"),
) -> None:
    """Search jobs across all sources."""
    from .models import JobLevel
    from .sync import search as run_search

    try:
        result = run_search(
            keywords,
            location=location,
            remote=remote or None,
            level=JobLevel(level) if level else None,
            sector=sector,
            country=country,
            salary_min=salary_min,
            salary_max=salary_max,
            infer_level_from_experience=infer_level,
            limit=limit,
        )
    except ValueError as exc:
        err_console.print(f"[red]invalid level:[/] {exc}")
        raise typer.Exit(code=1) from exc
    except (JobSpineError, NotImplementedError, ImportError) as exc:
        err_console.print(f"[red]search failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if as_json:
        console.print_json(json.dumps(result.to_dicts()))
    else:
        table = Table(title=f"{len(result)} jobs")
        table.add_column("company", style="cyan")
        table.add_column("title")
        table.add_column("location")
        table.add_column("level", style="magenta")
        table.add_column("sector", style="green")
        table.add_column("source", style="dim")
        for job in result.jobs:
            loc = job.locations[0].as_text() if job.locations else ""
            table.add_row(
                job.company, job.title, loc, job.level.value, job.sector or "", job.source
            )
        console.print(table)
        for h in result.failed_sources:
            err_console.print(f"[yellow]source {h.source} failed:[/] {h.error}")


if __name__ == "__main__":
    app()
