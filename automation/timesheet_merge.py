"""Merge a cycle month's weekly timesheet PDFs into one document per resource.

The download step writes one PDF per timesheet - 452 files for a 97-person month. This step
folds those back into 97 documents, one per resource, weeks in chronological order.

It is deliberately a separate pass rather than something the downloader does as it goes. The
downloads run concurrently, so a person's weeks land out of order and interleaved with everyone
else's; merging on the fly would need per-worker completion tracking and a decision about what
to do with a half-finished person. Run afterwards, over the finished directory, the whole thing
is a deterministic, offline, disk-only operation: group, sort, concatenate.

Two properties matter and both fall out of that choice:

1. The weekly PDFs remain the source of truth. A merged file is a derived artifact and is
   always rebuilt from its sources, never appended to. This is what makes a re-run safe -
   timesheets that clear approval later are picked up by re-downloading and re-merging, and
   the weeks already in last run's merged file are not duplicated.
2. No session, no browser, no network. The merge can be re-run at any time against a month
   directory that is already on disk.
"""

import csv
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from loguru import logger
from pypdf import PdfWriter

from automation.timesheet_pdf import DOWNLOADABLE_STATUSES, TimesheetRow, worker_slug
from config.settings import Settings

# Weekly PDFs are named <slug>__<YYYY-MM-DD>__<DLTTS ref>.pdf. The slug collapses every run of
# non-alphanumerics to a single underscore, so it can never contain '__' and the split is
# unambiguous.
_WEEK_FILE = re.compile(r"^(?P<slug>.+?)__(?P<week>\d{4}-\d{2}-\d{2})__(?P<ref>[A-Za-z0-9]+)\.pdf$")

# Merged output lives in its own subdirectory, so it is never mistaken for a weekly file and
# the downloader's resumability check keeps working untouched.
MERGED_DIRNAME = "merged"


@dataclass(frozen=True)
class WeekFile:
    """One weekly timesheet PDF found on disk."""

    path: Path
    slug: str
    week_ending: date
    timesheet_ref: str

    @property
    def sort_key(self) -> tuple[date, str]:
        return self.week_ending, self.timesheet_ref


@dataclass(frozen=True)
class WorkerIdentity:
    """One Fieldglass worker record, and the timesheets filed under it."""

    worker_id: str
    worker_name: str
    refs: frozenset[str]
    week_endings: frozenset[date]


@dataclass
class MergedFile:
    """One resource's merged month."""

    slug: str
    worker_name: str
    path: Path
    weeks: list[WeekFile]
    pages: int
    size_bytes: int
    worker_ids: tuple[str, ...] = ()
    expected_weeks: int | None = None
    note: str = ""

    @property
    def is_complete(self) -> bool:
        """Did every timesheet this resource filed this month make it into the document?"""
        return self.expected_weeks is None or len(self.weeks) >= self.expected_weeks


@dataclass
class MergeSummary:
    """Outcome of one month's merge."""

    merged: list[MergedFile] = field(default_factory=list)
    unreadable: list[tuple[Path, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    split_workers: list[str] = field(default_factory=list)
    removed_stale: list[Path] = field(default_factory=list)
    output_dir: Path | None = None
    manifest_path: Path | None = None
    elapsed_seconds: float = 0.0

    @property
    def partial(self) -> list[MergedFile]:
        return [m for m in self.merged if not m.is_complete]

    @property
    def source_count(self) -> int:
        return sum(len(m.weeks) for m in self.merged)


def month_bounds(month_dir: Path) -> tuple[date, date] | None:
    """Infer the cycle month covered by a <root>/<YYYY>/<MM> directory.

    The upper bound runs a week past month end: Fieldglass includes the week that starts inside
    the month but ends just after it, so July's cycle owns the week ending 01/08.
    """
    try:
        month = int(month_dir.name)
        year = int(month_dir.parent.name)
        start = date(year, month, 1)
    except ValueError:
        return None
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    return start, next_month + timedelta(days=6)


def discover_week_files(month_dir: Path) -> tuple[list[WeekFile], list[Path]]:
    """Find every weekly timesheet PDF in a month directory.

    Returns:
        tuple: The parsed week files, and any .pdf whose name did not fit the pattern.
    """
    found: list[WeekFile] = []
    unrecognised: list[Path] = []

    for path in sorted(month_dir.glob("*.pdf")):
        match = _WEEK_FILE.match(path.name)
        if not match:
            unrecognised.append(path)
            continue
        try:
            week = date.fromisoformat(match.group("week"))
        except ValueError:
            unrecognised.append(path)
            continue
        found.append(WeekFile(path=path, slug=match.group("slug"), week_ending=week,
                              timesheet_ref=match.group("ref")))

    return found, unrecognised


def load_worker_identities(
    settings: Settings, period_start: date, period_end: date
) -> dict[str, list[WorkerIdentity]]:
    """Read each worker's Fieldglass record(s) for the period out of PostgreSQL.

    The list grid the downloader reads carries no worker id, so identity can only come from the
    extracted data. Returns an empty mapping if the database is unreachable or the month has
    not been extracted - the merge then falls back to grouping on name alone.

    Returns:
        dict: Worker-name slug -> the worker records filing timesheets under that name.
    """
    try:
        import psycopg

        from db import postgres

        with psycopg.connect(**postgres.connection_kwargs(settings)) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT worker_name, worker_id, timesheet_id, period_end
                   FROM timesheets
                   WHERE period_end BETWEEN %s AND %s AND worker_id IS NOT NULL
                     AND status = ANY(%s)""",
                (period_start, period_end, sorted(DOWNLOADABLE_STATUSES)),
            )
            records = cur.fetchall()
    except Exception as exc:
        logger.warning(f"Worker identity audit unavailable ({type(exc).__name__}: "
                       f"{str(exc).splitlines()[0][:80]}); grouping on worker name alone.")
        return {}

    if not records:
        logger.warning("No extracted data for this period; grouping on worker name alone.")
        return {}

    grouped: dict[tuple[str, str], tuple[str, set[str], set[date]]] = {}
    for worker_name, worker_id, timesheet_id, period in records:
        key = (worker_slug(worker_name), worker_id)
        entry = grouped.setdefault(key, (worker_name, set(), set()))
        entry[1].add(timesheet_id)
        entry[2].add(period)

    identities: dict[str, list[WorkerIdentity]] = {}
    for (slug, worker_id), (worker_name, refs, weeks) in grouped.items():
        identities.setdefault(slug, []).append(WorkerIdentity(
            worker_id=worker_id, worker_name=worker_name,
            refs=frozenset(refs), week_endings=frozenset(weeks)))

    logger.info(f"Identity audit: {len(identities)} name(s) across "
                f"{sum(len(v) for v in identities.values())} worker record(s).")
    return identities


def _partition_by_identity(
    slug: str, weeks: list[WeekFile], identities: list[WorkerIdentity]
) -> tuple[list[tuple[str, list[WeekFile], tuple[str, ...], str]], bool, list[WeekFile]]:
    """Decide whether one name's weeks belong in one document or several.

    A single name covering several worker records is normally a re-badge: the resource was
    reissued a worker id mid-cycle (a renewal, or a rate revision), and their weeks run
    consecutively across the two records. That is one person and belongs in one document.

    Two different people sharing a name look different: both file a timesheet for the same
    week. Overlapping weeks is therefore the discriminator, and it is the case worth splitting
    on - these are billing documents, and quietly binding two people's hours into one file is
    a far worse outcome than emitting two files with an id in the name.

    Returns:
        tuple: The groups to write (suffix, weeks, worker ids, note), whether a genuine name
            collision forced a split, and any week that could not be attributed to a person.
    """
    all_ids = tuple(sorted(i.worker_id for i in identities))

    if len(identities) < 2:
        return [("", weeks, all_ids, "")], False, []

    overlapping: set[date] = set()
    for index, first in enumerate(identities):
        for second in identities[index + 1:]:
            overlapping |= first.week_endings & second.week_endings

    if not overlapping:
        note = f"re-badged mid-cycle across {len(identities)} worker records"
        logger.info(f"{slug}: {note} ({', '.join(all_ids)}); weeks do not overlap, "
                    f"merging as one resource.")
        return [("", weeks, all_ids, note)], False, []

    logger.warning(
        f"{slug}: NAME COLLISION - {len(identities)} worker records "
        f"({', '.join(all_ids)}) both filed timesheets for "
        f"{', '.join(str(w) for w in sorted(overlapping))}. These are different people; "
        f"splitting into one document per worker id.")

    by_ref = {ref: identity.worker_id for identity in identities for ref in identity.refs}
    groups: list[tuple[str, list[WeekFile], tuple[str, ...], str]] = []
    for identity in sorted(identities, key=lambda i: i.worker_id):
        owned = [w for w in weeks if by_ref.get(w.timesheet_ref) == identity.worker_id]
        if owned:
            groups.append((f"__{identity.worker_id}", owned, (identity.worker_id,),
                           "split: name shared by more than one worker record"))

    unclaimed = [w for w in weeks if w.timesheet_ref not in by_ref]
    if unclaimed:
        # A weekly PDF on disk with no extracted row cannot be attributed to either person,
        # so it is left out rather than guessed into the wrong document.
        logger.error(f"{slug}: {len(unclaimed)} weekly PDF(s) could not be attributed to a "
                     f"worker id and were left unmerged: "
                     f"{', '.join(w.timesheet_ref for w in unclaimed)}")

    return groups, True, unclaimed


def _write_merged_pdf(
    target: Path, weeks: list[WeekFile], title: str, add_bookmarks: bool
) -> tuple[int, list[tuple[Path, str]]]:
    """Concatenate one resource's weeks into a single PDF.

    Written to a temporary file and moved into place, so an interrupted merge cannot leave a
    truncated document sitting where a complete one is expected.

    Returns:
        tuple: Page count written, and any source that could not be read.
    """
    unreadable: list[tuple[Path, str]] = []
    temp = target.with_suffix(".pdf.part")
    pages = 0

    writer = PdfWriter()
    try:
        for week in weeks:
            try:
                # import_outline=False: the source documents carry their own outlines, which
                # would otherwise be pulled in alongside the per-week bookmark added here.
                before = len(writer.pages)
                writer.append(str(week.path), import_outline=False)
                if add_bookmarks and len(writer.pages) > before:
                    label = week.week_ending.strftime("Week ending %d/%m/%Y")
                    writer.add_outline_item(f"{label}  ({week.timesheet_ref})", before)
            except Exception as exc:
                unreadable.append((week.path, f"{type(exc).__name__}: {str(exc)[:80]}"))
                logger.warning(f"Skipping unreadable source {week.path.name}: {exc}")

        pages = len(writer.pages)
        if not pages:
            return 0, unreadable

        writer.add_metadata({"/Title": title, "/Producer": "SAP Fieldglass timesheet pipeline"})
        with temp.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()

    temp.replace(target)
    return pages, unreadable


def _expected_week_counts(rows: Iterable[TimesheetRow] | None) -> dict[str, int]:
    """How many downloadable timesheets each resource filed, from the month's list rows."""
    counts: dict[str, int] = {}
    for row in rows or ():
        if row.is_downloadable:
            counts[worker_slug(row.worker_name)] = counts.get(worker_slug(row.worker_name), 0) + 1
    return counts


def _write_manifest(summary: MergeSummary, path: Path) -> None:
    """Record what went into each document, so a partial month is visible without opening it."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Worker", "Worker IDs", "Merged File", "Weeks Merged", "Weeks Expected",
                         "Complete", "Pages", "Bytes", "Week Endings", "Timesheet IDs", "Note"])
        for item in sorted(summary.merged, key=lambda m: m.path.name):
            writer.writerow([
                item.worker_name,
                " ".join(item.worker_ids),
                item.path.name,
                len(item.weeks),
                "" if item.expected_weeks is None else item.expected_weeks,
                "yes" if item.is_complete else "NO",
                item.pages,
                item.size_bytes,
                " ".join(w.week_ending.isoformat() for w in item.weeks),
                " ".join(w.timesheet_ref for w in item.weeks),
                item.note,
            ])


def merge_month_by_worker(
    month_dir: Path,
    settings: Settings | None = None,
    rows: list[TimesheetRow] | None = None,
    output_dir: Path | None = None,
    add_bookmarks: bool = True,
    identities: dict[str, list[WorkerIdentity]] | None = None,
) -> MergeSummary:
    """Merge a month of weekly timesheet PDFs into one document per resource.

    Every merged document is rebuilt from its weekly sources on each call. Re-running after
    more timesheets have cleared approval is therefore safe and is the intended way to complete
    a partial month - nothing is ever appended to an existing merged file.

    Args:
        month_dir: A <root>/<YYYY>/<MM> directory of weekly timesheet PDFs.
        settings: Application settings, used to look up worker identity in PostgreSQL. Omit to
            skip the audit and group on worker name alone.
        rows: The month's list rows, if the caller already has them. Used to report how many
            timesheets each resource was expected to have.
        output_dir: Where merged documents are written. Defaults to <month_dir>/merged.
        add_bookmarks: Add a PDF outline entry per week.
        identities: Pre-loaded identity audit, to avoid a second database round trip.

    Returns:
        MergeSummary: What was written, what was incomplete, and what could not be read.
    """
    started = time.time()
    target_dir = output_dir or (month_dir / MERGED_DIRNAME)
    summary = MergeSummary(output_dir=target_dir)

    if not month_dir.is_dir():
        raise FileNotFoundError(f"No such month directory: {month_dir}")

    weeks, unrecognised = discover_week_files(month_dir)
    for path in unrecognised:
        logger.warning(f"Ignoring {path.name}: not a recognised weekly timesheet file name.")

    logger.info("=" * 60)
    logger.info(f"Timesheet merge | {month_dir}")
    if not weeks:
        logger.warning("No weekly timesheet PDFs found - nothing to merge.")
        return summary

    by_slug: dict[str, list[WeekFile]] = {}
    for week in weeks:
        by_slug.setdefault(week.slug, []).append(week)
    for group in by_slug.values():
        group.sort(key=lambda w: w.sort_key)

    logger.info(f"{len(weeks)} weekly PDF(s) across {len(by_slug)} resource(s)")

    bounds = month_bounds(month_dir)
    if identities is None and settings is not None and bounds is not None:
        identities = load_worker_identities(settings, *bounds)
    identities = identities or {}

    expected = _expected_week_counts(rows)
    if not expected and identities:
        expected = {slug: len({ref for i in ids for ref in i.refs})
                    for slug, ids in identities.items()}

    target_dir.mkdir(parents=True, exist_ok=True)
    month_tag = f"{month_dir.parent.name}-{month_dir.name}"
    written: set[Path] = set()

    for slug in sorted(by_slug):
        group = by_slug[slug]
        identity_list = identities.get(slug, [])
        worker_name = identity_list[0].worker_name if identity_list else slug.replace("_", " ")

        try:
            partitions, was_split, unclaimed = _partition_by_identity(slug, group, identity_list)
        except Exception as exc:
            summary.failed.append((slug, f"{type(exc).__name__}: {str(exc)[:90]}"))
            logger.error(f"{slug}: could not resolve identity: {exc}")
            continue

        if was_split:
            summary.split_workers.append(slug)
        if unclaimed:
            summary.failed.append((slug, f"{len(unclaimed)} week(s) not attributable to a "
                                         f"worker id, left unmerged: "
                                         f"{' '.join(w.timesheet_ref for w in unclaimed)}"))
        if not partitions:
            logger.error(f"{slug}: no week could be attributed to a worker id; nothing written.")

        for suffix, part_weeks, worker_ids, note in partitions:
            target = target_dir / f"{slug}__{month_tag}{suffix}.pdf"
            try:
                pages, unreadable = _write_merged_pdf(
                    target, part_weeks,
                    title=f"{worker_name} - timesheets {month_tag}", add_bookmarks=add_bookmarks)
                summary.unreadable.extend(unreadable)

                if not pages:
                    summary.failed.append((slug, "no readable source PDFs"))
                    logger.error(f"{slug}: no readable source PDFs; nothing written.")
                    continue

                merged_weeks = [w for w in part_weeks
                                if w.path not in {p for p, _ in unreadable}]
                written.add(target)
                summary.merged.append(MergedFile(
                    slug=slug, worker_name=worker_name, path=target, weeks=merged_weeks,
                    pages=pages, size_bytes=target.stat().st_size, worker_ids=worker_ids,
                    expected_weeks=None if was_split else expected.get(slug), note=note))
                logger.info(f"{target.name}  <- {len(merged_weeks)} week(s), {pages} page(s)")
            except Exception as exc:
                summary.failed.append((slug, f"{type(exc).__name__}: {str(exc)[:90]}"))
                logger.error(f"{slug}: merge failed: {exc}")

    # Merged output is entirely derived, so anything left from an earlier run that this one did
    # not produce is stale and misleading - most importantly the un-suffixed file left behind
    # when a resource is later split by the identity audit.
    for path in sorted(target_dir.glob("*.pdf")):
        if path not in written:
            path.unlink()
            summary.removed_stale.append(path)
            logger.warning(f"Removed stale merged file: {path.name}")
    for leftover in sorted(target_dir.glob("*.pdf.part")):
        leftover.unlink()

    summary.manifest_path = target_dir / f"merged_manifest_{month_tag.replace('-', '_')}.csv"
    _write_manifest(summary, summary.manifest_path)
    summary.elapsed_seconds = time.time() - started

    logger.success(f"Merged {summary.source_count} weekly PDF(s) into {len(summary.merged)} "
                   f"document(s) in {summary.elapsed_seconds:.1f}s")
    if summary.partial:
        logger.warning(f"Incomplete: {len(summary.partial)} resource(s) have fewer weeks than "
                       f"expected - re-run the download once they clear approval")
        for item in summary.partial[:10]:
            logger.warning(f"   {item.worker_name}: {len(item.weeks)}/{item.expected_weeks} week(s)")
    if summary.split_workers:
        logger.warning(f"Name collisions split by worker id: {', '.join(summary.split_workers)}")
    if summary.unreadable:
        logger.error(f"Unreadable sources: {len(summary.unreadable)}")
    if summary.failed:
        logger.error(f"Failed: {len(summary.failed)}")
        for slug, reason in summary.failed[:10]:
            logger.error(f"   {slug}: {reason}")
    logger.info(f"Output  : {target_dir}")
    logger.info(f"Manifest: {summary.manifest_path}")
    logger.info("=" * 60)

    return summary
