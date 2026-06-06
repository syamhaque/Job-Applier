#!/usr/bin/env python3
"""
Job Application Script
Find job listings matching your profile, review them, and open applications in your browser.
All listings are logged to a CSV for reference.
"""

import argparse
import csv
import os
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

console = Console()

LOG_FIELDNAMES = [
    "id", "title", "company", "location", "remote", "job_type",
    "salary_min", "salary_max", "url", "source", "posted_date", "status", "applied_at", "notes"
]
DEFAULT_LOG = "applications.csv"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    remote: bool = False
    job_type: str = "full-time"
    posted_date: Optional[datetime] = None
    description: str = ""
    source: str = ""
    status: str = "pending"
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None

    def salary_label(self) -> str:
        def fmt(n: int) -> str:
            return f"${n // 1000}k" if n >= 1000 else f"${n}"
        if self.salary_min and self.salary_max:
            return f"{fmt(self.salary_min)}-{fmt(self.salary_max)}"
        if self.salary_min:
            return f"{fmt(self.salary_min)}+"
        if self.salary_max:
            return f"up to {fmt(self.salary_max)}"
        return "N/A"

    def days_ago(self) -> Optional[int]:
        if not self.posted_date:
            return None
        now = datetime.now(timezone.utc)
        posted = self.posted_date
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (now - posted).days

    def posted_label(self) -> str:
        d = self.days_ago()
        if d is None:
            return "Unknown"
        if d == 0:
            return "Today"
        if d == 1:
            return "1 day ago"
        return f"{d}d ago"

    def to_row(self, idx: int) -> dict:
        return {
            "id": idx,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "remote": "yes" if self.remote else "no",
            "job_type": self.job_type,
            "salary_min": self.salary_min or "",
            "salary_max": self.salary_max or "",
            "url": self.url,
            "source": self.source,
            "posted_date": self.posted_date.isoformat() if self.posted_date else "",
            "status": self.status,
            "applied_at": datetime.now().isoformat() if self.status == "applied" else "",
            "notes": "",
        }


# ---------------------------------------------------------------------------
# Job search sources
# ---------------------------------------------------------------------------

def _parse_salary_text(text: str) -> tuple[Optional[int], Optional[int]]:
    """Extract (min, max) integers from a free-text salary string.
    Returns (None, None) when salary is not determinable.
    """
    import re
    if not text:
        return None, None
    # Normalise: strip currency symbols, commas, spaces
    cleaned = text.replace(",", "").replace("$", "").replace("£", "").replace("€", "")
    # Find all numbers, optionally followed by k/K
    numbers = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([kK])?", cleaned):
        val = float(m.group(1))
        if m.group(2):
            val *= 1000
        val = int(val)
        # Ignore implausible values (< $10k or > $2M annual)
        if 10_000 <= val <= 2_000_000:
            numbers.append(val)
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], None
    return min(numbers), max(numbers)


def _parse_salary_arg(s: str) -> int:
    """Parse a CLI salary value: '120000' or '120k' → 120000."""
    s = s.strip().lower()
    if s.endswith("k"):
        return int(float(s[:-1]) * 1000)
    return int(s)


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def search_adzuna(
    keywords: str,
    location: str = "",
    remote: bool = False,
    job_type: Optional[str] = None,
    max_days_old: Optional[int] = None,
    count: int = 20,
    app_id: str = "",
    app_key: str = "",
    min_salary: Optional[int] = None,
) -> list[Job]:
    if not app_id or not app_key:
        return []

    what = f"{keywords} remote" if remote else keywords
    params: dict = {
        "app_id": app_id,
        "app_key": app_key,
        "what": what,
        "results_per_page": min(count, 50),
        "content-type": "application/json",
    }
    if location:
        params["where"] = location
    if max_days_old:
        params["max_days_old"] = max_days_old
    if job_type == "full-time":
        params["full_time"] = 1
    elif job_type == "part-time":
        params["part_time"] = 1
    elif job_type == "contract":
        params["contract"] = 1
    if min_salary:
        params["salary_min"] = min_salary

    try:
        resp = requests.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        console.print(f"[yellow]Adzuna: {e}[/yellow]")
        return []

    jobs = []
    for item in data.get("results", []):
        text = (item.get("title", "") + " " + item.get("description", "")).lower()
        is_remote = remote or "remote" in text
        sal_min = item.get("salary_min")
        sal_max = item.get("salary_max")
        jobs.append(
            Job(
                title=item.get("title", ""),
                company=item.get("company", {}).get("display_name", ""),
                location=item.get("location", {}).get("display_name", ""),
                url=item.get("redirect_url", ""),
                remote=is_remote,
                job_type=job_type or "full-time",
                posted_date=_parse_iso(item.get("created", "")),
                description=item.get("description", ""),
                source="Adzuna",
                salary_min=int(sal_min) if sal_min else None,
                salary_max=int(sal_max) if sal_max else None,
            )
        )
    return jobs


_OPEN_LOCATIONS = {"worldwide", "anywhere", "global", "remote"}

def _location_matches(candidate_location: str, filter_location: str) -> bool:
    """Return True if the job's required location is compatible with the filter."""
    cl = candidate_location.lower().strip()
    # Empty = open to all; known open terms also pass
    if not cl or any(term in cl for term in _OPEN_LOCATIONS):
        return True
    fl = filter_location.lower().strip()
    # Accept if either string contains the other (e.g. "US" in "USA Only", or "united states" in "US")
    return fl in cl or cl in fl


def search_remotive(
    keywords: str,
    location: str = "",
    job_type: Optional[str] = None,
    count: int = 20,
) -> list[Job]:
    # Fetch more than needed so location filtering doesn't leave us short
    fetch_limit = min(count * 4, 100) if location else min(count, 100)
    params: dict = {"search": keywords, "limit": fetch_limit}
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        console.print(f"[yellow]Remotive: {e}[/yellow]")
        return []

    jobs = []
    for item in data.get("jobs", []):
        if len(jobs) >= count:
            break
        jtype = item.get("job_type", "full_time").replace("_", "-").lower()
        if job_type and job_type not in jtype:
            continue
        candidate_loc = item.get("candidate_required_location", "")
        if location and not _location_matches(candidate_loc, location):
            continue
        sal_min, sal_max = _parse_salary_text(item.get("salary", ""))
        jobs.append(
            Job(
                title=item.get("title", ""),
                company=item.get("company_name", ""),
                location=item.get("candidate_required_location", "Worldwide"),
                url=item.get("url", ""),
                remote=True,
                job_type=jtype,
                posted_date=_parse_iso(item.get("publication_date", "")),
                description=item.get("description", ""),
                source="Remotive",
                salary_min=sal_min,
                salary_max=sal_max,
            )
        )
    return jobs


def search_themuse(
    keywords: str,
    location: str = "",
    job_type: Optional[str] = None,
    count: int = 20,
) -> list[Job]:
    params: dict = {"page": 0, "api_key": os.environ.get("THEMUSE_API_KEY", "")}

    # The Muse uses "category" not free-text; we filter client-side
    try:
        resp = requests.get(
            "https://www.themuse.com/api/public/jobs",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        console.print(f"[yellow]The Muse: {e}[/yellow]")
        return []

    kw_lower = keywords.lower().split()
    jobs = []
    for item in data.get("results", []):
        name = item.get("name", "")
        if not any(k in name.lower() for k in kw_lower):
            continue

        locs = [loc.get("name", "") for loc in item.get("locations", [])]
        loc_str = ", ".join(locs) if locs else "Unknown"

        is_remote = any("remote" in l.lower() for l in locs)
        if location and not is_remote:
            if not any(location.lower() in l.lower() for l in locs):
                continue

        levels = [lv.get("name", "") for lv in item.get("levels", [])]
        jtype = "full-time"
        if any("part" in lv.lower() for lv in levels):
            jtype = "part-time"
        if job_type and job_type != jtype:
            continue

        url = item.get("refs", {}).get("landing_page", "")
        pub = item.get("publication_date", "")

        jobs.append(
            Job(
                title=name,
                company=item.get("company", {}).get("name", ""),
                location=loc_str,
                url=url,
                remote=is_remote,
                job_type=jtype,
                posted_date=_parse_iso(pub),
                source="The Muse",
            )
        )
        if len(jobs) >= count:
            break

    return jobs


def aggregate_search(
    keywords: str,
    location: str = "",
    remote: bool = False,
    job_type: Optional[str] = None,
    max_days_old: Optional[int] = None,
    count: int = 20,
    adzuna_id: str = "",
    adzuna_key: str = "",
    min_salary: Optional[int] = None,
) -> list[Job]:
    results: list[Job] = []

    with console.status("[bold green]Searching for jobs...[/bold green]"):
        if adzuna_id and adzuna_key:
            results += search_adzuna(
                keywords, location, remote, job_type, max_days_old, count,
                adzuna_id, adzuna_key, min_salary,
            )

        # Always include Remotive for remote or when no Adzuna key
        if remote or not (adzuna_id and adzuna_key):
            results += search_remotive(keywords, location, job_type, count)

        # The Muse as supplemental source (no key required)
        if len(results) < count:
            results += search_themuse(keywords, location, job_type, count)

    # Deduplicate by URL
    seen: set[str] = set()
    unique: list[Job] = []
    for job in results:
        if job.url and job.url not in seen:
            seen.add(job.url)
            unique.append(job)

    # Filter by max age
    if max_days_old is not None:
        unique = [j for j in unique if j.days_ago() is None or j.days_ago() <= max_days_old]

    # Filter by minimum salary — only exclude if salary is known and tops out below the floor
    if min_salary is not None:
        def _salary_ok(j: Job) -> bool:
            cap = j.salary_max or j.salary_min
            return cap is None or cap >= min_salary
        unique = [j for j in unique if _salary_ok(j)]

    def _sort_key(j: Job) -> datetime:
        if not j.posted_date:
            return datetime.min.replace(tzinfo=timezone.utc)
        if j.posted_date.tzinfo is None:
            return j.posted_date.replace(tzinfo=timezone.utc)
        return j.posted_date

    unique.sort(key=_sort_key, reverse=True)

    return unique[:count]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def display_listings(jobs: list[Job]) -> None:
    table = Table(
        title=f"[bold cyan]{len(jobs)} Job Listing(s) Found[/bold cyan]",
        box=box.ROUNDED,
        show_lines=True,
        highlight=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Title", min_width=28)
    table.add_column("Company", style="green", min_width=18)
    table.add_column("Location", style="blue", min_width=14)
    table.add_column("Type", width=11)
    table.add_column("Remote", width=7, justify="center")
    table.add_column("Salary", width=14, justify="right")
    table.add_column("Posted", width=11)
    table.add_column("Source", width=9, style="dim")

    for i, job in enumerate(jobs, 1):
        remote_cell = Text("Yes", style="bold green") if job.remote else Text("No", style="dim")
        sal = job.salary_label()
        sal_cell = Text(sal, style="dim") if sal == "N/A" else Text(sal, style="yellow")
        table.add_row(
            str(i),
            job.title,
            job.company,
            job.location,
            job.job_type,
            remote_cell,
            sal_cell,
            job.posted_label(),
            job.source,
        )

    console.print()
    console.print(table)
    console.print()


def display_job_detail(job: Job, idx: int, total: int) -> None:
    console.rule(f"[bold]Listing {idx}/{total}[/bold]")
    console.print(f"[bold white]{job.title}[/bold white]")
    console.print(f"  Company:  [green]{job.company}[/green]")
    console.print(f"  Location: [blue]{job.location}[/blue]")
    console.print(f"  Type:     {job.job_type}")
    console.print(f"  Remote:   {'[green]Yes[/green]' if job.remote else 'No'}")
    sal = job.salary_label()
    sal_fmt = f"[dim]{sal}[/dim]" if sal == "N/A" else f"[yellow]{sal}[/yellow]"
    console.print(f"  Salary:   {sal_fmt}")
    console.print(f"  Posted:   {job.posted_label()}")
    console.print(f"  Source:   [dim]{job.source}[/dim]")
    console.print(f"  URL:      [dim cyan]{job.url}[/dim cyan]")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def ensure_log(path: str) -> None:
    if not Path(path).exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDNAMES).writeheader()


def log_job(job: Job, idx: int, path: str) -> None:
    ensure_log(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=LOG_FIELDNAMES).writerow(job.to_row(idx))


# ---------------------------------------------------------------------------
# Apply / open
# ---------------------------------------------------------------------------

def open_in_browser(job: Job) -> None:
    webbrowser.open(job.url)
    time.sleep(1.5)


def do_apply(job: Job, idx: int, log: str) -> None:
    job.status = "applied"
    open_in_browser(job)
    log_job(job, idx, log)
    console.print(f"  [green]✓ Opened in browser[/green] — logged as [bold]applied[/bold]")


def do_skip(job: Job, idx: int, log: str) -> None:
    job.status = "skipped"
    log_job(job, idx, log)
    console.print(f"  [dim]Skipped[/dim]")


def do_list_only(job: Job, idx: int, log: str) -> None:
    job.status = "listed"
    log_job(job, idx, log)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_auto(jobs: list[Job], log: str, delay: float = 2.0) -> None:
    console.print(
        Panel(
            f"[bold yellow]Auto mode[/bold yellow] — opening all [bold]{len(jobs)}[/bold] listings in your browser.\n"
            "[dim]Switch to each tab and click Apply / Easy Apply.[/dim]",
            border_style="yellow",
        )
    )
    if not Confirm.ask(f"Open all {len(jobs)} listings?", default=True):
        console.print("[dim]Cancelled.[/dim]")
        return

    for i, job in enumerate(jobs, 1):
        console.print(f"\n[{i}/{len(jobs)}] [bold]{job.title}[/bold] @ [green]{job.company}[/green]")
        do_apply(job, i, log)
        if i < len(jobs):
            time.sleep(delay)

    console.print(f"\n[green]Done.[/green] All {len(jobs)} listings opened.")


def mode_review(jobs: list[Job], log: str) -> tuple[int, int]:
    console.print(
        Panel(
            "[bold cyan]Review mode[/bold cyan] — press [bold]y[/bold] to open & apply, "
            "[bold]n[/bold] to skip, [bold]q[/bold] to quit.",
            border_style="cyan",
        )
    )

    applied = skipped = 0
    for i, job in enumerate(jobs, 1):
        display_job_detail(job, i, len(jobs))
        choice = Prompt.ask(
            "\n  Apply?",
            choices=["y", "n", "q"],
            default="y",
            show_choices=True,
        )
        if choice == "q":
            console.print("[yellow]Stopping review.[/yellow]")
            for remaining in jobs[i:]:
                do_skip(remaining, i + jobs[i:].index(remaining), log)
            break
        elif choice == "y":
            do_apply(job, i, log)
            applied += 1
        else:
            do_skip(job, i, log)
            skipped += 1
        console.print()

    return applied, skipped


def mode_list_only(jobs: list[Job], log: str) -> None:
    for i, job in enumerate(jobs, 1):
        do_list_only(job, i, log)
    console.print(f"[dim]{len(jobs)} listings logged to {log}[/dim]")


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apply",
        description="Find job listings and open applications in your browser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python apply.py -k "software engineer" -l "San Francisco, CA" -n 15
  python apply.py -k "backend engineer python" --remote --max-age-days 7 --auto
  python apply.py -k "staff engineer" -t contract --count 10 --list-only
  python apply.py -k "SDE" --remote --review

API Keys (optional, add to .env):
  ADZUNA_APP_ID / ADZUNA_APP_KEY   — broader job coverage (free at developer.adzuna.com)
  THEMUSE_API_KEY                  — higher rate limits on The Muse
        """,
    )

    # Search filters
    p.add_argument("--keywords", "-k", required=True,
                   help="Job title / skills to search for")
    p.add_argument("--location", "-l", default="",
                   help="City, state, or country (e.g. 'San Francisco, CA')")
    p.add_argument("--remote", "-r", action="store_true",
                   help="Include remote listings (adds Remotive results)")
    p.add_argument("--job-type", "-t",
                   choices=["full-time", "part-time", "contract"],
                   default="full-time",
                   help="Employment type (default: full-time)")
    p.add_argument("--max-age-days", "-d", type=int, default=30,
                   help="Only listings posted within this many days (default: 30)")
    p.add_argument("--count", "-n", type=int, default=20,
                   help="Max listings to fetch (default: 20)")
    p.add_argument("--min-salary", "-s", default=None,
                   help="Minimum salary (e.g. 120000 or 120k). Listings with known salary below this are excluded; unknown-salary listings are kept.")

    # Mode (mutually exclusive)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--auto", "-a", action="store_true",
                      help="Open all listings in browser after showing the table")
    mode.add_argument("--review", action="store_true",
                      help="Review and apply one by one")
    mode.add_argument("--list-only", action="store_true",
                      help="Show and log listings without opening any")

    # Output
    p.add_argument("--log", default=DEFAULT_LOG,
                   help=f"CSV log file path (default: {DEFAULT_LOG})")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Seconds between browser opens in auto mode (default: 2)")

    return p


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()

    adzuna_id = os.environ.get("ADZUNA_APP_ID", "")
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "")

    min_salary: Optional[int] = None
    if args.min_salary:
        try:
            min_salary = _parse_salary_arg(args.min_salary)
        except ValueError:
            console.print(f"[red]Invalid --min-salary value: {args.min_salary!r}. Use a number like 120000 or 120k.[/red]")
            sys.exit(1)

    sal_display = f"${min_salary:,}" if min_salary else "(any)"
    console.print(
        Panel.fit(
            f"[bold cyan]Job Finder[/bold cyan]\n"
            f"[dim]Keywords:[/dim]    [bold]{args.keywords}[/bold]\n"
            f"[dim]Location:[/dim]    {args.location or '(any)'}\n"
            f"[dim]Remote:[/dim]      {'Yes' if args.remote else 'No'}\n"
            f"[dim]Type:[/dim]        {args.job_type}\n"
            f"[dim]Min salary:[/dim]  {sal_display}\n"
            f"[dim]Max age:[/dim]     {args.max_age_days} days\n"
            f"[dim]Count:[/dim]       {args.count}",
            border_style="cyan",
        )
    )

    if not adzuna_id:
        console.print(
            "[yellow]Tip:[/yellow] Add [bold]ADZUNA_APP_ID[/bold] and [bold]ADZUNA_APP_KEY[/bold] "
            "to [dim].env[/dim] for broader job coverage (free at developer.adzuna.com).\n"
        )

    jobs = aggregate_search(
        keywords=args.keywords,
        location=args.location,
        remote=args.remote,
        job_type=args.job_type,
        max_days_old=args.max_age_days,
        count=args.count,
        adzuna_id=adzuna_id,
        adzuna_key=adzuna_key,
        min_salary=min_salary,
    )

    if not jobs:
        console.print("[red]No listings found matching your criteria.[/red]")
        sys.exit(0)

    # Always show the full table first
    display_listings(jobs)

    # Determine mode
    if args.list_only:
        mode_list_only(jobs, args.log)
        console.print(f"\n[dim]Log: {args.log}[/dim]")
        return

    if not args.auto and not args.review:
        console.print("Choose how to proceed:")
        choice = Prompt.ask(
            "  Mode",
            choices=["auto", "review", "list-only", "quit"],
            default="review",
            show_choices=True,
        )
        if choice == "quit":
            mode_list_only(jobs, args.log)
            console.print(f"[dim]All logged to {args.log}[/dim]")
            return
        elif choice == "auto":
            args.auto = True
        elif choice == "list-only":
            mode_list_only(jobs, args.log)
            console.print(f"\n[dim]Log: {args.log}[/dim]")
            return
        else:
            args.review = True

    if args.auto:
        mode_auto(jobs, args.log, delay=args.delay)
    else:
        applied, skipped = mode_review(jobs, args.log)
        console.print(
            f"[bold]Session complete.[/bold] "
            f"Applied: [green]{applied}[/green]  "
            f"Skipped: [dim]{skipped}[/dim]"
        )

    console.print(f"\n[dim]Log saved → {args.log}[/dim]")


if __name__ == "__main__":
    main()
