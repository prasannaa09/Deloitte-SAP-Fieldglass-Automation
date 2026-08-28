"""Export extracted timesheet data from PostgreSQL to a multi-sheet Excel workbook.

Written for review rather than for machines: the main sheet carries one row per timesheet with
the week's daily hours spread across Sun..Sat columns, and the supporting sheets hold the
one-to-many detail (comments, rate lines) that cannot fit on a single row.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import psycopg
from loguru import logger
from psycopg.rows import dict_row

from config.settings import Settings
from db.postgres import connection_kwargs
from utils.helpers import get_previous_month_date_range

DAY_ORDER = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

_TIMESHEET_QUERY = """
    SELECT timesheet_id      AS "Time Sheet ID",
           worker_id         AS "Worker ID",
           worker_name       AS "Worker",
           status            AS "Status",
           period_start      AS "Period Start",
           period_end        AS "Period End",
           bill_rate         AS "Bill Rate",
           quantity          AS "Quantity",
           amount            AS "Amount",
           currency          AS "Currency",
           rate_line_count   AS "Rate Lines",
           total_worked      AS "Total Worked",
           round(total_minutes / 60.0, 2) AS "Total Hours",
           pay_rate          AS "Pay Rate",
           pay_amount        AS "Pay Amount",
           legal_entity      AS "Legal Entity",
           site              AS "Site",
           business_unit     AS "Business Unit",
           comments_joined   AS "Comments",
           pdf_filename      AS "PDF File",
           extracted_at      AS "Extracted At"
    FROM timesheets
    WHERE period_end BETWEEN %(start)s AND %(end)s
    ORDER BY worker_name, period_start
"""

_DAYS_QUERY = """
    SELECT timesheet_id, day_index, day_name, day_date, hours_text, minutes
    FROM timesheet_days
    WHERE timesheet_id IN (SELECT timesheet_id FROM timesheets
                           WHERE period_end BETWEEN %(start)s AND %(end)s)
    ORDER BY timesheet_id, day_index
"""

_COMMENTS_QUERY = """
    SELECT c.timesheet_id AS "Time Sheet ID",
           t.worker_name  AS "Worker",
           c.comment_index AS "#",
           c.entered_at   AS "Entered",
           c.author       AS "Author",
           c.comment_text AS "Comment"
    FROM timesheet_comments c JOIN timesheets t USING (timesheet_id)
    WHERE t.period_end BETWEEN %(start)s AND %(end)s
    ORDER BY t.worker_name, c.timesheet_id, c.comment_index
"""

_RATES_QUERY = """
    SELECT r.timesheet_id AS "Time Sheet ID",
           t.worker_name  AS "Worker",
           r.party        AS "Party",
           r.line_index   AS "Line",
           r.category     AS "Rate Category",
           r.rate         AS "Rate",
           r.quantity     AS "Quantity",
           r.amount       AS "Amount"
    FROM timesheet_rates r JOIN timesheets t USING (timesheet_id)
    WHERE t.period_end BETWEEN %(start)s AND %(end)s
    ORDER BY t.worker_name, r.timesheet_id, r.party, r.line_index
"""


def _drop_timezones(frame: pd.DataFrame) -> pd.DataFrame:
    """Make timestamp columns timezone-naive; Excel cannot represent an offset."""
    for column in frame.columns:
        values = frame[column]
        if isinstance(values.dtype, pd.DatetimeTZDtype):
            frame[column] = values.dt.tz_localize(None)
        elif values.dtype == object and not values.empty:
            sample = values.dropna()
            if not sample.empty and getattr(sample.iloc[0], "tzinfo", None) is not None:
                frame[column] = pd.to_datetime(values, errors="coerce", utc=True).dt.tz_localize(None)
    return frame


def export_month_to_excel(
    settings: Settings,
    output_path: Path | str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> Path:
    """Write one month of extracted timesheet data to an Excel workbook.

    Args:
        settings: Application settings carrying the database connection.
        output_path: Destination .xlsx. Defaults to <REPORT_DIR>/Timesheets_<YYYY>_<MM>.xlsx.
        period_start: Earliest week-ending date to include. Defaults to the previous month's 1st.
        period_end: Latest week-ending date. Defaults to a week past the previous month's end,
            so the week that starts in the month but ends just after it is still included.

    Returns:
        Path: The workbook that was written.
    """
    if period_start is None or period_end is None:
        start_text, end_text = get_previous_month_date_range()
        day, month, year = start_text.split("/")
        computed_start = date(int(year), int(month), 1)
        end_day, end_month, end_year = end_text.split("/")
        computed_end = date(int(end_year), int(end_month), int(end_day))
        period_start = period_start or computed_start
        # Fieldglass counts the week that ends just after the month, so reach a week further
        period_end = period_end or date.fromordinal(computed_end.toordinal() + 7)

    params = {"start": period_start, "end": period_end}

    if output_path is None:
        output_path = settings.REPORT_DIR / f"Timesheets_{period_start:%Y_%m}.xlsx"
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Exporting timesheets with week-ending {period_start} .. {period_end}")

    with psycopg.connect(**connection_kwargs(settings)) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_TIMESHEET_QUERY, params)
            timesheets = _drop_timezones(pd.DataFrame(cur.fetchall()))
            cur.execute(_DAYS_QUERY, params)
            days = _drop_timezones(pd.DataFrame(cur.fetchall()))
            cur.execute(_COMMENTS_QUERY, params)
            comments = _drop_timezones(pd.DataFrame(cur.fetchall()))
            cur.execute(_RATES_QUERY, params)
            rates = _drop_timezones(pd.DataFrame(cur.fetchall()))

    if timesheets.empty:
        raise RuntimeError(f"No timesheets found with week-ending between {period_start} and {period_end}")

    # Spread the week across Sun..Sat columns so one timesheet reads as one row
    if not days.empty:
        wide = days.pivot_table(index="timesheet_id", columns="day_name",
                                values="hours_text", aggfunc="first")
        wide = wide.reindex(columns=[d for d in DAY_ORDER if d in wide.columns])
        wide = wide.reset_index().rename(columns={"timesheet_id": "Time Sheet ID"})
        timesheets = timesheets.merge(wide, on="Time Sheet ID", how="left")

        # Keep the day columns next to the hours they explain, not stranded at the end
        # Insert in week order so the columns read Sun..Sat, each landing just before the total
        ordered = list(timesheets.columns)
        for column in [d for d in DAY_ORDER if d in timesheets.columns]:
            ordered.remove(column)
            ordered.insert(ordered.index("Total Worked"), column)
        timesheets = timesheets[ordered]

    summary = pd.DataFrame({
        "Metric": ["Timesheets", "Workers", "Total Amount", "Total Quantity (hrs)",
                   "Comments", "Multi-rate timesheets", "Week ending range", "Statuses"],
        "Value": [
            len(timesheets),
            timesheets["Worker ID"].nunique(),
            float(timesheets["Amount"].fillna(0).astype(float).sum()),
            float(timesheets["Quantity"].fillna(0).astype(float).sum()),
            len(comments),
            int((timesheets["Rate Lines"].fillna(0) > 1).sum()),
            f"{timesheets['Period End'].min()} .. {timesheets['Period End'].max()}",
            ", ".join(f"{k}: {v}" for k, v in timesheets["Status"].value_counts().items()),
        ],
    })

    # Excel holds an exclusive lock on an open workbook. Write alongside it rather than failing
    # the run, so a review session in progress never costs the export.
    try:
        with output_file.open("ab"):
            pass
    except PermissionError:
        locked = output_file
        output_file = output_file.with_name(f"{output_file.stem}_new{output_file.suffix}")
        logger.warning(f"{locked.name} is open in Excel; writing to {output_file.name} instead.")

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        timesheets.to_excel(writer, sheet_name="Timesheets", index=False)
        if not days.empty:
            days.rename(columns={
                "timesheet_id": "Time Sheet ID", "day_index": "#", "day_name": "Day",
                "day_date": "Date", "hours_text": "Hours", "minutes": "Minutes",
            }).to_excel(writer, sheet_name="Daily Hours", index=False)
        if not comments.empty:
            comments.to_excel(writer, sheet_name="Comments", index=False)
        if not rates.empty:
            rates.to_excel(writer, sheet_name="Rate Lines", index=False)

        # Freeze the header row and widen columns enough to read without resizing by hand
        for sheet_name, frame in (("Summary", summary), ("Timesheets", timesheets),
                                  ("Comments", comments), ("Rate Lines", rates)):
            if sheet_name not in writer.book.sheetnames or frame.empty:
                continue
            sheet = writer.book[sheet_name]
            sheet.freeze_panes = "A2"
            for index, column in enumerate(frame.columns, start=1):
                longest = max([len(str(column))] +
                              [len(str(v)) for v in frame[column].head(200).tolist()])
                sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = \
                    min(max(longest + 2, 10), 60)

    logger.success(f"Exported {len(timesheets)} timesheet(s) to {output_file}")
    return output_file
