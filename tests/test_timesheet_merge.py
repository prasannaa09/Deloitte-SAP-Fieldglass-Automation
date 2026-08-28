"""Unit test module verifying the per-resource timesheet PDF merge."""

from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import Destination

from automation.timesheet_merge import (
    WorkerIdentity,
    discover_week_files,
    merge_month_by_worker,
    month_bounds,
)
from automation.timesheet_pdf import TimesheetRow, worker_slug


def _write_pdf(path: Path, pages: int) -> None:
    """Write a minimal valid PDF, standing in for a downloaded weekly timesheet."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.fixture
def month_dir(tmp_path: Path) -> Path:
    """A <root>/<YYYY>/<MM> directory holding one resource's weeks."""
    directory = tmp_path / "2026" / "07"
    directory.mkdir(parents=True)
    for week, ref, pages in (("2026-07-04", "DLTTS001", 2),
                             ("2026-07-11", "DLTTS002", 1),
                             ("2026-08-01", "DLTTS003", 2)):
        _write_pdf(directory / f"Doe_Jane__{week}__{ref}.pdf", pages)
    return directory


def test_worker_slug_matches_download_filenames() -> None:
    """The merge groups on the same slug the downloader names files with."""
    row = TimesheetRow(status="Invoiced", time_sheet_ref="DLTTS01", worker_name="Doe, Jane A",
                       end_date="04/07/2026", st_hours="40", internal_id="x", host="h")
    assert row.pdf_filename.startswith(worker_slug(row.worker_name) + "__")


def test_month_bounds_includes_the_overhanging_week() -> None:
    """July's cycle owns the week that starts in July and ends in August."""
    bounds = month_bounds(Path("downloads/timesheets/2026/07"))
    assert bounds is not None
    assert bounds[0] == date(2026, 7, 1)
    assert bounds[1] >= date(2026, 8, 1)


def test_discover_ignores_unrecognised_names(month_dir: Path) -> None:
    (month_dir / "manifest_notes.pdf").write_bytes(b"%PDF-1.4 not a timesheet")
    weeks, unrecognised = discover_week_files(month_dir)
    assert len(weeks) == 3
    assert [p.name for p in unrecognised] == ["manifest_notes.pdf"]


def test_merges_weeks_in_chronological_order(month_dir: Path) -> None:
    summary = merge_month_by_worker(month_dir)
    assert len(summary.merged) == 1

    merged = summary.merged[0]
    assert merged.pages == 5
    assert [w.week_ending for w in merged.weeks] == [
        date(2026, 7, 4), date(2026, 7, 11), date(2026, 8, 1)]

    reader = PdfReader(merged.path)
    # A PDF outline may nest; these are written flat, so take the top-level entries only.
    bookmarks = [item for item in reader.outline if isinstance(item, Destination)]
    # Resolve each bookmark to a page index by its indirect reference, which is what the
    # outline actually stores.
    page_ids = [page.indirect_reference.idnum for page in reader.pages
                if page.indirect_reference is not None]
    landed = []
    for bookmark in bookmarks:
        target = bookmark.page
        assert target is not None
        landed.append(page_ids.index(target.idnum))
    assert landed == [0, 2, 3]


def test_weekly_sources_are_left_untouched(month_dir: Path) -> None:
    before = {p.name: p.read_bytes() for p in month_dir.glob("*.pdf")}
    merge_month_by_worker(month_dir)
    after = {p.name: p.read_bytes() for p in month_dir.glob("*.pdf")}
    assert before == after


def test_remerging_does_not_duplicate_pages(month_dir: Path) -> None:
    """A merged document is rebuilt from source, never appended to."""
    first = merge_month_by_worker(month_dir).merged[0]
    first_bytes = first.path.read_bytes()

    second = merge_month_by_worker(month_dir).merged[0]
    assert second.pages == first.pages
    assert second.path.read_bytes() == first_bytes


def test_late_week_is_inserted_in_date_order(month_dir: Path) -> None:
    """A timesheet that clears approval later lands in its place, not at the end."""
    merge_month_by_worker(month_dir)
    _write_pdf(month_dir / "Doe_Jane__2026-07-18__DLTTS004.pdf", 1)

    merged = merge_month_by_worker(month_dir).merged[0]
    assert [w.week_ending for w in merged.weeks] == [
        date(2026, 7, 4), date(2026, 7, 11), date(2026, 7, 18), date(2026, 8, 1)]


def test_partial_month_is_reported_against_expected_rows(month_dir: Path) -> None:
    rows = [TimesheetRow(status="Invoiced", time_sheet_ref=f"DLTTS00{n}", worker_name="Doe, Jane",
                         end_date=f"0{n}/07/2026", st_hours="40", internal_id="x", host="h")
            for n in range(1, 6)]
    merged = merge_month_by_worker(month_dir, rows=rows).merged[0]
    assert merged.expected_weeks == 5
    assert not merged.is_complete


def test_rebadged_worker_stays_one_document(month_dir: Path) -> None:
    """Disjoint weeks across two worker ids are one person, reissued mid-cycle."""
    identities = {"Doe_Jane": [
        WorkerIdentity("DLTWK01", "Doe, Jane", frozenset({"DLTTS001", "DLTTS002"}),
                       frozenset({date(2026, 7, 4), date(2026, 7, 11)})),
        WorkerIdentity("DLTWK02", "Doe, Jane", frozenset({"DLTTS003"}),
                       frozenset({date(2026, 8, 1)}))]}

    summary = merge_month_by_worker(month_dir, identities=identities)
    assert len(summary.merged) == 1
    assert summary.split_workers == []
    assert summary.merged[0].worker_ids == ("DLTWK01", "DLTWK02")


def test_shared_name_splits_by_worker_id(month_dir: Path) -> None:
    """Two worker ids filing the same week are two people and must not be bound together."""
    identities = {"Doe_Jane": [
        WorkerIdentity("DLTWK01", "Doe, Jane", frozenset({"DLTTS001"}),
                       frozenset({date(2026, 7, 4)})),
        WorkerIdentity("DLTWK02", "Doe, Jane", frozenset({"DLTTS002", "DLTTS003"}),
                       frozenset({date(2026, 7, 4), date(2026, 8, 1)}))]}

    summary = merge_month_by_worker(month_dir, identities=identities)
    assert summary.split_workers == ["Doe_Jane"]
    assert sorted(m.path.name for m in summary.merged) == [
        "Doe_Jane__2026-07__DLTWK01.pdf", "Doe_Jane__2026-07__DLTWK02.pdf"]


def test_unattributable_week_is_reported_not_guessed(month_dir: Path) -> None:
    """A weekly PDF with no extracted row is left out of a split rather than misfiled."""
    identities = {"Doe_Jane": [
        WorkerIdentity("DLTWK01", "Doe, Jane", frozenset({"DLTTS001"}),
                       frozenset({date(2026, 7, 4)})),
        WorkerIdentity("DLTWK02", "Doe, Jane", frozenset({"DLTTS002"}),
                       frozenset({date(2026, 7, 4)}))]}

    summary = merge_month_by_worker(month_dir, identities=identities)
    assert any("DLTTS003" in reason for _, reason in summary.failed)
    for item in summary.merged:
        assert "DLTTS003" not in [w.timesheet_ref for w in item.weeks]


def test_stale_merged_file_is_removed(month_dir: Path) -> None:
    """Output is derived, so a document this run did not produce is stale and misleading."""
    summary = merge_month_by_worker(month_dir)
    stale = summary.output_dir / "Someone_Gone__2026-07.pdf" if summary.output_dir else None
    assert stale is not None
    _write_pdf(stale, 1)

    again = merge_month_by_worker(month_dir)
    assert stale.name in [p.name for p in again.removed_stale]
    assert not stale.exists()


def test_unreadable_source_is_skipped_not_fatal(month_dir: Path) -> None:
    (month_dir / "Doe_Jane__2026-07-25__DLTTS009.pdf").write_bytes(b"%PDF-1.4 truncated")
    summary = merge_month_by_worker(month_dir)
    assert len(summary.unreadable) == 1
    assert summary.merged[0].pages == 5
