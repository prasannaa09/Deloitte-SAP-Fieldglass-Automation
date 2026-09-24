"""PostgreSQL storage for IBM Requisition data extracted from SAP Fieldglass."""

import json
from typing import Any
import psycopg
from loguru import logger
from psycopg import sql
from psycopg.rows import dict_row

from config.settings import Settings
from db.postgres import connection_kwargs

IBM_SCHEMA_STATEMENT = """
CREATE TABLE IF NOT EXISTS ibm_requisitions (
    requisition_id    TEXT PRIMARY KEY,
    job_title         TEXT,
    status            TEXT,
    hiring_manager    TEXT,
    business_unit     TEXT,
    start_date        TEXT,
    end_date          TEXT,
    work_location     TEXT,
    bill_rate         TEXT,
    positions         TEXT,
    job_description   TEXT,
    source_url        TEXT,
    extracted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    all_fields        JSONB
);
"""


def ensure_ibm_schema(settings: Settings) -> None:
    """Ensure the ibm_requisitions table exists in the configured database."""
    try:
        with psycopg.connect(**connection_kwargs(settings)) as conn:
            with conn.cursor() as cur:
                cur.execute(IBM_SCHEMA_STATEMENT)
            conn.commit()
        logger.debug("Ensured ibm_requisitions table exists.")
    except Exception as exc:
        logger.warning(f"Could not initialize PostgreSQL schema for IBM requisitions: {exc}")


def upsert_ibm_requisitions(settings: Settings, records: list[dict[str, Any]]) -> int:
    """Insert or update IBM requisition records in PostgreSQL.

    Args:
        settings: Application settings.
        records: Extracted requisition records.

    Returns:
        int: Number of records inserted/updated.
    """
    if not records:
        return 0

    ensure_ibm_schema(settings)

    upsert_sql = """
    INSERT INTO ibm_requisitions (
        requisition_id, job_title, status, hiring_manager,
        business_unit, start_date, end_date, work_location,
        bill_rate, positions, job_description, source_url, all_fields
    ) VALUES (
        %(requisition_id)s, %(job_title)s, %(status)s, %(hiring_manager)s,
        %(business_unit)s, %(start_date)s, %(end_date)s, %(work_location)s,
        %(bill_rate)s, %(positions)s, %(job_description)s, %(source_url)s, %(all_fields)s
    )
    ON CONFLICT (requisition_id) DO UPDATE SET
        job_title = EXCLUDED.job_title,
        status = EXCLUDED.status,
        hiring_manager = EXCLUDED.hiring_manager,
        business_unit = EXCLUDED.business_unit,
        start_date = EXCLUDED.start_date,
        end_date = EXCLUDED.end_date,
        work_location = EXCLUDED.work_location,
        bill_rate = EXCLUDED.bill_rate,
        positions = EXCLUDED.positions,
        job_description = EXCLUDED.job_description,
        source_url = EXCLUDED.source_url,
        all_fields = EXCLUDED.all_fields,
        extracted_at = now();
    """

    count = 0
    try:
        with psycopg.connect(**connection_kwargs(settings)) as conn:
            with conn.cursor() as cur:
                for r in records:
                    params = {
                        "requisition_id": r.get("Requisition_ID", ""),
                        "job_title": r.get("Job_Title", ""),
                        "status": r.get("Status", ""),
                        "hiring_manager": r.get("Hiring_Manager", ""),
                        "business_unit": r.get("Business_Unit", ""),
                        "start_date": r.get("Start_Date", ""),
                        "end_date": r.get("End_Date", ""),
                        "work_location": r.get("Work_Location", ""),
                        "bill_rate": r.get("Bill_Rate", ""),
                        "positions": r.get("Positions", ""),
                        "job_description": r.get("Job_Description", ""),
                        "source_url": r.get("Source_URL", ""),
                        "all_fields": json.dumps(r.get("All_Extracted_Fields", {})),
                    }
                    cur.execute(upsert_sql, params)
                    count += 1
            conn.commit()
        logger.success(f"Upserted {count} IBM requisition(s) into PostgreSQL database.")
    except Exception as exc:
        logger.warning(f"Database upsert failed (PostgreSQL might be offline or unconfigured): {exc}")

    return count
