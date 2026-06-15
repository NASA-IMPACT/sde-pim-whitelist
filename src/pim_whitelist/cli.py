"""Command-line entrypoint for the whitelist updater."""

from __future__ import annotations

import datetime
import logging
import subprocess
import sys
from dataclasses import dataclass

import click
from dotenv import load_dotenv

from . import config, report
from .consolidate import ConsolidationReport, consolidate
from .diff import Delta, compute_delta, merge_source_concepts
from .sources import get_sources
from .whitelist import Whitelist


@dataclass
class DatasetResult:
    dataset: config.DatasetConfig
    delta: Delta
    merged: Whitelist
    consolidation: ConsolidationReport


def _today() -> str:
    return datetime.date.today().isoformat()


def process_dataset(
    dataset: config.DatasetConfig, *, from_cache: bool
) -> DatasetResult:
    """Load whitelist + sources, compute delta, and rebuild the merged whitelist.

    The delta is kept for the "what's new" report; the written file is produced by
    :func:`consolidate`, which reprocesses the whole list (fold duplicates, merge
    safe acronyms, sort) rather than merely appending.
    """
    whitelist = Whitelist.load(dataset.whitelist_path)
    raw_concepts = []
    for source in get_sources(dataset):
        raw_concepts.extend(source.load(from_cache=from_cache))
    source_concepts = merge_source_concepts(raw_concepts)
    delta = compute_delta(dataset.name, whitelist, source_concepts)
    merged, consolidation = consolidate(whitelist, source_concepts, dataset.name)
    return DatasetResult(
        dataset=dataset, delta=delta, merged=merged, consolidation=consolidation
    )


def _changed(result: DatasetResult) -> bool:
    """True if the rebuilt whitelist differs from what is on disk."""
    original = Whitelist.load(result.dataset.whitelist_path).serialize()
    return result.merged.serialize() != original


def _write_consolidation_report(result: DatasetResult, date: str) -> None:
    """Write the per-dataset consolidation audit/review report."""
    text = report.render_consolidation(result.consolidation, date=date)
    path = config.REPORTS_DIR / f"{result.dataset.name}-consolidation-{date}.md"
    path.write_text(text, encoding="utf-8")


def _selected_datasets(name: str) -> list[config.DatasetConfig]:
    if name == "all":
        return list(config.DATASETS.values())
    return [config.DATASETS[name]]


@click.group()
@click.version_option()
def main() -> None:
    """Reconcile PIM whitelist files with upstream NASA sources."""
    # Load secrets (e.g. OPENAI_API_KEY for `classify`) from the repo-root .env.
    # Real environment variables already set take precedence (override=False).
    load_dotenv(config.REPO_ROOT / ".env", override=False)


@main.command()
@click.option(
    "--dataset",
    type=click.Choice(["all", *config.DATASETS.keys()]),
    default="all",
    show_default=True,
    help="Which dataset(s) to process.",
)
@click.option(
    "--dry-run", is_flag=True, help="Report only; do not write whitelist files."
)
@click.option(
    "--from-cache", is_flag=True, help="Reuse cached raw responses in data/raw/."
)
@click.option(
    "--open-pr",
    is_flag=True,
    help="Create a branch, commit the updated files + report, and open a PR via gh.",
)
def update(dataset: str, dry_run: bool, from_cache: bool, open_pr: bool) -> None:
    """Fetch sources, compute deltas, and (unless --dry-run) write merged files."""
    # Surface source-layer progress (e.g. the SDE crawl) on stderr. Kept to bare
    # text to match the CLI's existing echo style.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    datasets = _selected_datasets(dataset)
    date = _today()

    results: list[DatasetResult] = []
    for ds in datasets:
        click.echo(f"Processing {ds.name}…")
        try:
            results.append(process_dataset(ds, from_cache=from_cache))
        except Exception as exc:
            # Fail fast: abort the whole run on the first dataset error (after
            # naming the culprit) rather than writing a partial report.
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

    for result in results:
        _write_consolidation_report(result, date)

    if dry_run:
        click.echo("Dry run: no whitelist files modified.")
        return

    changed: list[DatasetResult] = []
    for result in results:
        if not _changed(result):
            continue
        result.merged.save(result.dataset.whitelist_path)
        changed.append(result)
        click.echo(f"Updated {result.dataset.whitelist_filename}")

    if not changed:
        click.echo("No changes to apply.")
        return

    if open_pr:
        _open_pr(changed, report_path, date)


@main.command("consolidate")
@click.option(
    "--dataset",
    type=click.Choice(["all", *config.DATASETS.keys()]),
    default="all",
    show_default=True,
    help="Which dataset(s) to process.",
)
@click.option(
    "--dry-run", is_flag=True, help="Report only; do not write whitelist files."
)
def consolidate_cmd(dataset: str, dry_run: bool) -> None:
    """Re-fold, merge safe acronyms, and alphabetize the whitelist file(s).

    Pure local pass — no network. Reprocesses each whitelist against itself and
    writes a consolidation report under reports/.
    """
    datasets = _selected_datasets(dataset)
    date = _today()
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for ds in datasets:
        click.echo(f"Consolidating {ds.name}…")
        whitelist = Whitelist.load(ds.whitelist_path)
        merged, consolidation = consolidate(whitelist, [], ds.name)
        result = DatasetResult(
            dataset=ds,
            delta=Delta(dataset=ds.name),
            merged=merged,
            consolidation=consolidation,
        )
        report_text = report.render_consolidation(consolidation, date=date)
        click.echo(report_text)
        _write_consolidation_report(result, date)

        if dry_run:
            continue
        if _changed(result):
            merged.save(ds.whitelist_path)
            click.echo(f"Updated {ds.whitelist_filename}")
        else:
            click.echo(f"{ds.whitelist_filename} already consolidated.")


@main.command("classify")
@click.option(
    "--dataset",
    type=click.Choice(["all", *config.DATASETS.keys()]),
    default="all",
    show_default=True,
    help="Which dataset(s) to classify.",
)
@click.option(
    "--model",
    default=None,
    help="OpenAI model. Defaults to $OPENAI_MODEL, then config.DEFAULT_OPENAI_MODEL.",
)
@click.option(
    "--reclassify",
    is_flag=True,
    help="Ignore the cache and re-resolve every concept (clean slate / first run). "
    "Default reuses already-resolved records and only classifies empty + new ones.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Send at most N concepts to OpenAI (cheap test run).",
)
@click.option(
    "--batch-size",
    type=int,
    default=config.OPENAI_BATCH_SIZE,
    show_default=True,
    help="Concepts per OpenAI request.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report only; do not call OpenAI or write files (no API key needed).",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log each batch request/response (and retries) on stderr to see progress.",
)
def classify_cmd(
    dataset: str,
    model: str | None,
    reclassify: bool,
    limit: int | None,
    batch_size: int,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Tag each concept with its NASA SMD science division(s).

    Resolves each concept provenance-first (free SDE-crawl lookup), sending only
    the remainder to OpenAI, and writes one JSON file per dataset to
    whitelist/classified/. The JSON is a persistent cache: already-resolved
    records are reused and only empty + new concepts are (re)classified. OpenAI
    work requires OPENAI_API_KEY (from the environment or a repo-root .env).
    """
    from . import classify

    # Surface classifier progress on stderr. Timestamps + level make a stalled
    # request obvious (watch the seconds tick between lines). -v drops to DEBUG
    # (per-request timing); otherwise INFO shows per-batch lines, WARNING retries.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    # Quiet the HTTP client libraries so -v shows our batch logs, not header dumps.
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    resolved_model = classify.resolve_model(model)
    datasets = _selected_datasets(dataset)
    if not dry_run:
        config.CLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)

    for ds in datasets:
        click.echo(f"Classifying {ds.name} with {resolved_model}…")

        def _progress(done: int, total: int, _ds=ds) -> None:
            click.echo(f"  {_ds.name}: classified {done}/{total}", err=True)

        try:
            stats = classify.classify_dataset(
                ds,
                model=resolved_model,
                reclassify=reclassify,
                limit=limit,
                dry_run=dry_run,
                batch_size=batch_size,
                on_progress=_progress,
            )
        except classify.ClassifierError as exc:
            raise click.ClickException(str(exc))

        out = config.CLASSIFIED_DIR / f"{ds.name}.json"
        if dry_run:
            click.echo(
                f"  {stats['records']} records — reused {stats['reused']}, "
                f"provenance {stats['provenance']}, would classify "
                f"{stats['to_classify']} via OpenAI ({stats['empty']} empty)"
            )
            click.echo(f"  Dry run: {out.relative_to(config.REPO_ROOT)} not written.")
        else:
            click.echo(
                f"  {stats['records']} records — reused {stats['reused']}, "
                f"provenance {stats['provenance']}, classified {stats['classified']} "
                f"({stats['empty']} empty)"
            )
            click.echo(f"  wrote {out.relative_to(config.REPO_ROOT)}")


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
