"""PostgreSQL storage for timesheet data extracted from SAP Fieldglass.

Three tables, because one timesheet holds several days and several comments:

    timesheets          one row per timesheet, keyed on the DLTTS reference
    timesheet_days      one row per day of the week the timesheet covers
    timesheet_comments  one row per comment, with its author and timestamp

Writes are upserts keyed on the timesheet id, so re-running an extraction refreshes existing
rows instead of duplicating them - a timesheet that moves from Approved to Invoiced updates
in place, and a re-run after a failure is always safe.
"""

from typing import Any

import psycopg
from loguru import logger
from psycopg import sql
from psycopg.rows import dict_row

from config.settings import Settings

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS timesheets (
        timesheet_id     TEXT PRIMARY KEY,
        worker_id        TEXT,
        worker_name      TEXT,
        status           TEXT,
        period_start     DATE,
        period_end       DATE,
        buyer            TEXT,
        rate_category    TEXT,
        bill_rate        NUMERIC(14, 2),
        quantity         NUMERIC(14, 2),
        amount           NUMERIC(14, 2),
        pay_rate         NUMERIC(14, 2),
        pay_amount       NUMERIC(14, 2),
        currency         TEXT,
        total_worked     TEXT,
        total_minutes    INTEGER,
        legal_entity     TEXT,
        site             TEXT,
        business_unit    TEXT,
        comments_joined  TEXT,
        internal_id      TEXT,
        pdf_filename     TEXT,
        source_url       TEXT,
        extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS timesheet_days (
        timesheet_id TEXT NOT NULL REFERENCES timesheets(timesheet_id) ON DELETE CASCADE,
        day_index    SMALLINT NOT NULL,
        day_label    TEXT,
        day_name     TEXT,
        day_date     DATE,
        hours_text   TEXT,
        minutes      INTEGER,
        PRIMARY KEY (timesheet_id, day_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS timesheet_comments (
        timesheet_id  TEXT NOT NULL REFERENCES timesheets(timesheet_id) ON DELETE CASCADE,
        comment_index SMALLINT NOT NULL,
        entered_at    TIMESTAMPTZ,
        entered_text  TEXT,
        author        TEXT,
        comment_text  TEXT,
        PRIMARY KEY (timesheet_id, comment_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS timesheet_rates (
        timesheet_id TEXT NOT NULL REFERENCES timesheets(timesheet_id) ON DELETE CASCADE,
        party        TEXT NOT NULL,
        line_index   SMALLINT NOT NULL,
        category     TEXT,
        rate         NUMERIC(14, 2),
        quantity     NUMERIC(14, 2),
        days         NUMERIC(14, 2),
        amount       NUMERIC(14, 2),
        PRIMARY KEY (timesheet_id, party, line_index)
    )
    """,
    "ALTER TABLE timesheets ADD COLUMN IF NOT EXISTS rate_line_count SMALLINT",
    "CREATE INDEX IF NOT EXISTS idx_timesheets_worker ON timesheets (worker_id)",
    "CREATE INDEX IF NOT EXISTS idx_timesheets_period ON timesheets (period_start, period_end)",
    "CREATE INDEX IF NOT EXISTS idx_timesheets_status ON timesheets (status)",
)

_TIMESHEET_COLUMNS = (
    "timesheet_id", "worker_id", "worker_name", "status", "period_start", "period_end",
    "buyer", "rate_category", "bill_rate", "quantity", "amount", "pay_rate", "pay_amount",
    "currency", "total_worked", "total_minutes", "legal_entity", "site", "business_unit",
    "comments_joined", "internal_id", "pdf_filename", "source_url", "rate_line_count",
)


def connection_kwargs(settings: Settings, dbname: str | None = None) -> dict[str, Any]:
    """Build psycopg connection arguments from settings.

    Passed as keyword arguments rather than a connection string, so that passwords containing
    '@' or spaces are not misparsed.
    """
    return {
        "host": settings.PG_HOST,
        "port": settings.PG_PORT,
        "user": settings.PG_USER,
        "password": settings.PG_PASSWORD,
        "dbname": dbname or settings.PG_DATABASE,
        "connect_timeout": 10,
    }


def check_connection(settings: Settings) -> tuple[bool, str]:
    """Verify the configured database can be reached.

    Returns:
        tuple[bool, str]: (True, server version) on success, (False, reason) otherwise.
    """
    try:
        with psycopg.connect(**connection_kwargs(settings)) as conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            return True, (row[0] if row else "unknown")
    except Exception as exc:
        return False, str(exc).splitlines()[0]


def ensure_database(settings: Settings) -> None:
    """Create the target database if it does not exist yet.

    Connects to the maintenance database 'postgres' to do so, since a database cannot be
    created from within itself.
    """
    try:
        with psycopg.connect(**connection_kwargs(settings, dbname="postgres")) as conn:
            conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.PG_DATABASE,))
                if cur.fetchone():
                    logger.debug(f"Database '{settings.PG_DATABASE}' already exists.")
                    return
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.PG_DATABASE)))
                logger.success(f"Created database '{settings.PG_DATABASE}'.")
    except Exception as exc:
        raise RuntimeError(f"Could not ensure database '{settings.PG_DATABASE}': "
                           f"{str(exc).splitlines()[0]}") from exc


def ensure_schema(settings: Settings) -> None:
    """Create the tables and indexes if they are not already present."""
    ensure_database(settings)
    with psycopg.connect(**connection_kwargs(settings)) as conn, conn.cursor() as cur:
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
        conn.commit()
    logger.success(f"Schema ready in database '{settings.PG_DATABASE}'.")


def upsert_timesheet(settings: Settings, record: dict[str, Any]) -> None:
    """Insert or refresh one timesheet together with its days and comments.

    Days and comments are replaced wholesale rather than merged: the detail page is the single
    source of truth, so a re-extraction should leave exactly what the page currently shows.

    Args:
        settings: Application settings carrying the connection details.
        record: A parsed timesheet, as produced by automation.timesheet_data.parse_timesheet_detail.
    """
    with psycopg.connect(**connection_kwargs(settings)) as conn:
        _upsert_with_connection(conn, record)
        conn.commit()


def _upsert_with_connection(conn: psycopg.Connection, record: dict[str, Any]) -> None:
    """Write one record using an existing connection (used for batch writes)."""
    timesheet_id = record["timesheet_id"]
    values = [record.get(column) for column in _TIMESHEET_COLUMNS]

    assignments = sql.SQL(", ").join(
        sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(column))
        for column in _TIMESHEET_COLUMNS if column != "timesheet_id"
    )
    statement = sql.SQL(
        "INSERT INTO timesheets ({cols}) VALUES ({vals}) "
        "ON CONFLICT (timesheet_id) DO UPDATE SET {assignments}, extracted_at = now()"
    ).format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in _TIMESHEET_COLUMNS),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(_TIMESHEET_COLUMNS)),
        assignments=assignments,
    )

    with conn.cursor() as cur:
        cur.execute(statement, values)

        cur.execute("DELETE FROM timesheet_days WHERE timesheet_id = %s", (timesheet_id,))
        for index, day in enumerate(record.get("days") or []):
            cur.execute(
                "INSERT INTO timesheet_days "
                "(timesheet_id, day_index, day_label, day_name, day_date, hours_text, minutes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (timesheet_id, index, day.get("label"), day.get("name"),
                 day.get("date"), day.get("hours_text"), day.get("minutes")),
            )

        cur.execute("DELETE FROM timesheet_rates WHERE timesheet_id = %s", (timesheet_id,))
        for line in record.get("rate_lines") or []:
            cur.execute(
                "INSERT INTO timesheet_rates "
                "(timesheet_id, party, line_index, category, rate, quantity, days, amount) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (timesheet_id, line.get("party"), line.get("line_index"), line.get("category"),
                 line.get("rate"), line.get("quantity"), line.get("days"), line.get("amount")),
            )

        cur.execute("DELETE FROM timesheet_comments WHERE timesheet_id = %s", (timesheet_id,))
        for index, comment in enumerate(record.get("comments") or []):
            cur.execute(
                "INSERT INTO timesheet_comments "
                "(timesheet_id, comment_index, entered_at, entered_text, author, comment_text) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (timesheet_id, index, comment.get("entered_at"), comment.get("entered"),
                 comment.get("name"), comment.get("comment")),
            )


def upsert_many(settings: Settings, records: list[dict[str, Any]]) -> int:
    """Write a batch of timesheets over a single connection.

    Returns:
        int: Number of records written.
    """
    if not records:
        return 0
    with psycopg.connect(**connection_kwargs(settings)) as conn:
        for record in records:
            _upsert_with_connection(conn, record)
        conn.commit()
    return len(records)


def fetch_timesheet(settings: Settings, timesheet_id: str) -> dict[str, Any] | None:
    """Read one timesheet back, with its days and comments. Used to verify a run."""
    with psycopg.connect(**connection_kwargs(settings)) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM timesheets WHERE timesheet_id = %s", (timesheet_id,))
            record = cur.fetchone()
            if not record:
                return None
            cur.execute("SELECT * FROM timesheet_days WHERE timesheet_id = %s ORDER BY day_index",
                        (timesheet_id,))
            record["days"] = cur.fetchall()
            cur.execute("SELECT * FROM timesheet_comments WHERE timesheet_id = %s ORDER BY comment_index",
                        (timesheet_id,))
            record["comments"] = cur.fetchall()
            return record
