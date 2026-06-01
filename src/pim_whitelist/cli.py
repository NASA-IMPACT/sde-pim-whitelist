"""Command-line entrypoint for the whitelist updater."""

from __future__ import annotations

import datetime
import subprocess
import sys
from dataclasses import dataclass

import click

from . import config, report
from .diff import Delta, compute_delta
from .merge import apply_delta
from .sources import get_source
from .whitelist import Whitelist


@dataclass
class DatasetResult:
    dataset: config.DatasetConfig
    delta: Delta
    merged: Whitelist


def _today() -> str:
    return datetime.date.today().isoformat()


def process_dataset(
    dataset: config.DatasetConfig, *, from_cache: bool
) -> DatasetResult:
    """Load whitelist + source, compute delta, and build the merged whitelist."""
    whitelist = Whitelist.load(dataset.whitelist_path)
    source = get_source(dataset)
    source_concepts = source.load(from_cache=from_cache)
    delta = compute_delta(dataset.name, whitelist, source_concepts)
    merged = apply_delta(whitelist, delta)
    return DatasetResult(dataset=dataset, delta=delta, merged=merged)


def _selected_datasets(name: str) -> list[config.DatasetConfig]:
    if name == "all":
        return list(config.DATASETS.values())
    return [config.DATASETS[name]]


@click.group()
@click.version_option()
def main() -> None:
    """Reconcile PIM whitelist files with upstream NASA sources."""


@main.command()
@click.option(
    "--dataset",
    type=click.Choice(["all", *config.DATASETS.keys()]),
    default="all",
    show_default=True,
    help="Which dataset(s) to process.",
)
@click.option("--dry-run", is_flag=True, help="Report only; do not write whitelist files.")
@click.option("--from-cache", is_flag=True, help="Reuse cached raw responses in data/raw/.")
@click.option(
    "--open-pr",
    is_flag=True,
    help="Create a branch, commit the updated files + report, and open a PR via gh.",
)
def update(dataset: str, dry_run: bool, from_cache: bool, open_pr: bool) -> None:
    """Fetch sources, compute deltas, and (unless --dry-run) write merged files."""
    datasets = _selected_datasets(dataset)
    date = _today()

    results: list[DatasetResult] = []
    for ds in datasets:
        click.echo(f"Processing {ds.name}…")
        try:
            results.append(process_dataset(ds, from_cache=from_cache))
        except Exception as exc:  # surface, continue with others
            click.echo(f"  ERROR: {exc}", err=True)
            raise

    deltas = [r.delta for r in results]
    report_text = report.render_many(deltas, date=date)
    click.echo("")
    click.echo(report_text)

    # Always write the report file.
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = dataset if dataset != "all" else "all"
    report_path = config.REPORTS_DIR / f"{suffix}-delta-{date}.md"
    report_path.write_text(report_text, encoding="utf-8")
    click.echo(f"Report written to {report_path.relative_to(config.REPO_ROOT)}")

    if dry_run:
        click.echo("Dry run: no whitelist files modified.")
        return

    changed: list[DatasetResult] = []
    for result in results:
        if result.delta.is_empty:
            continue
        result.merged.save(result.dataset.whitelist_path)
        changed.append(result)
        click.echo(f"Updated {result.dataset.whitelist_filename}")

    if not changed:
        click.echo("No changes to apply.")
        return

    if open_pr:
        _open_pr(changed, report_path, date)


def _run_git(*args: str) -> None:
    subprocess.run(["git", *args], cwd=config.REPO_ROOT, check=True)


def _open_pr(changed: list[DatasetResult], report_path, date: str) -> None:
    branch = f"whitelist-update-{date}"
    _run_git("checkout", "-b", branch)
    for result in changed:
        _run_git("add", str(result.dataset.whitelist_path))
    _run_git("add", str(report_path))
    names = ", ".join(r.dataset.name for r in changed)
    _run_git("commit", "-m", f"Update {names} whitelist ({date})")
    _run_git("push", "-u", "origin", branch)
    body = report_path.read_text(encoding="utf-8")
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            f"PIM whitelist update — {date}",
            "--body",
            body,
        ],
        cwd=config.REPO_ROOT,
        check=True,
    )
    click.echo(f"Opened PR from branch {branch}.")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
