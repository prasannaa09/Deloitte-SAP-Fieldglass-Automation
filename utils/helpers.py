"""Date helpers shared across the pipeline."""

import calendar
from datetime import date, datetime
from typing import Union


def get_previous_month_date_range(ref_date: Union[datetime, date, None] = None) -> tuple[str, str]:
    """Calculate the start (1st day) and end (last day) dates for the previous calendar month.

    Example: If ref_date is 2026-08-18, returns ('01/07/2026', '31/07/2026').

    Args:
        ref_date: Reference date (defaults to current system date).

    Returns:
        tuple[str, str]: (start_date_str, end_date_str) in 'DD/MM/YYYY' format.
    """
    if ref_date is None:
        ref_date = datetime.now()

    year = ref_date.year
    month = ref_date.month

    if month == 1:
        prev_month = 12
        prev_year = year - 1
    else:
        prev_month = month - 1
        prev_year = year

    last_day = calendar.monthrange(prev_year, prev_month)[1]

    start_date_obj = date(prev_year, prev_month, 1)
    end_date_obj = date(prev_year, prev_month, last_day)

    return start_date_obj.strftime("%d/%m/%Y"), end_date_obj.strftime("%d/%m/%Y")

