"""Excel cleaner module for SAP Fieldglass exported reports and list data."""

from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd
from loguru import logger
from utils.helpers import generate_timestamp_filename

# Standard 16-column header layout matching reference InvoiceSheet.xlsx
TARGET_COLUMNS = [
    "Status",
    "ID",
    "Revision",
    "Worker",
    "Site",
    "END",
    "ST",
    "OT",
    "DT",
    "Others",
    "NB",
    "BillRate",
    "Amount",
    "Comment",
    "WO_ID",
    "Legal Entity",
]


def clean_timesheet_excel(
    input_path: Union[str, Path],
    output_path: Union[str, Path, None] = None,
    wo_input_path: Union[str, Path, None] = None,
) -> Path:
    """Clean exported SAP Fieldglass timesheet data into the standardized InvoiceSheet.xlsx format.

    - Auto-parses worker grouping section headers (e.g. 'Worker : Abhilash  Domal') and forward-fills Worker names.
    - Matches Worker names to Work Order IDs (WO_ID) from Work Order list exports if available.
    - Formats numbers, dates, and column headers into the exact 16-column target layout:
      ['Status', 'ID', 'Revision', 'Worker', 'Site', 'END', 'ST', 'OT', 'DT', 'Others', 'NB', 'BillRate', 'Amount', 'Comment', 'WO_ID', 'Legal Entity']

    Args:
        input_path: Path to downloaded timesheet Excel / CSV file.
        output_path: Destination path for cleaned Excel output (.xlsx).
        wo_input_path: Optional path to work_order.supplier.list.csv for WO_ID lookups.

    Returns:
        Path: Path to saved cleaned Excel file.
    """
    input_file = Path(input_path).resolve()
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    logger.info(f"Cleaning exported Excel data from: {input_file}")

    # Determine default output path if not provided
    if output_path is None:
        out_name = generate_timestamp_filename("InvoiceSheet_Cleaned", "xlsx")
        output_file = input_file.parent / out_name
    else:
        output_file = Path(output_path).resolve()

    output_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. Attempt to locate Work Order export for WO_ID lookups
    worker_to_wo = {}
    wo_file = None

    if wo_input_path and Path(wo_input_path).exists():
        wo_file = Path(wo_input_path).resolve()
    else:
        # Check input file's parent directory for work_order.supplier.list.csv
        possible_wo = input_file.parent / "work_order.supplier.list.csv"
        if possible_wo.exists():
            wo_file = possible_wo

    if wo_file and wo_file.exists():
        try:
            logger.info(f"Loading Work Order mapping from: {wo_file}")
            if wo_file.suffix.lower() == ".csv":
                df_wo = pd.read_csv(wo_file)
            else:
                df_wo = pd.read_excel(wo_file)

            wo_col = next((c for c in df_wo.columns if "worker" in c.lower() or "seeker" in c.lower()), None)
            id_col = next((c for c in df_wo.columns if c.lower() in ("id", "work order id", "wo_id")), None)

            if wo_col and id_col:
                valid_wo = df_wo.dropna(subset=[wo_col, id_col]).copy()
                valid_wo["Worker_Clean"] = valid_wo[wo_col].astype(str).str.strip()
                worker_to_wo = dict(zip(valid_wo["Worker_Clean"], valid_wo[id_col].astype(str).str.strip()))
                logger.info(f"Loaded {len(worker_to_wo)} Worker -> WO_ID mappings.")
        except Exception as wo_err:
            logger.warning(f"Note: Could not build WO_ID mapping from {wo_file}: {wo_err}")

    # 2. Read input file
    raw_df: pd.DataFrame
    try:
        if input_file.suffix.lower() == ".csv":
            raw_df = pd.read_csv(input_file)
        else:
            try:
                raw_df = pd.read_excel(input_file)
            except Exception:
                html_tables = pd.read_html(input_file)
                raw_df = html_tables[0] if html_tables else pd.DataFrame()
    except Exception as read_err:
        logger.error(f"Failed to read input file {input_file}: {read_err}")
        raise read_err

    # 3. Transform rows and propagate Worker name
    current_worker = None
    cleaned_rows = []

    status_col = next((c for c in raw_df.columns if "status" in c.lower()), raw_df.columns[0])
    id_col = next((c for c in raw_df.columns if c.lower() in ("id", "time sheet id", "timesheet id")), raw_df.columns[1] if len(raw_df.columns) > 1 else None)

    for _, row in raw_df.iterrows():
        status_val = str(row[status_col]).strip() if pd.notna(row[status_col]) else ""

        # Handle Worker section headers (e.g. 'Worker : Abhilash  Domal')
        if status_val.startswith("Worker :"):
            raw_name = status_val.replace("Worker :", "").strip()
            parts = raw_name.split()
            if len(parts) >= 2:
                current_worker = f"{parts[0]}, {' '.join(parts[1:])}"
            else:
                current_worker = raw_name
            continue

        # Skip non-data / title rows without Time Sheet ID
        ts_id = str(row[id_col]).strip() if id_col and pd.notna(row[id_col]) else ""
        if not ts_id or ts_id.lower() in ("nan", "none", "id", "status"):
            continue

        # Worker name from column or section header
        worker_val = str(row.get("Worker", "")).strip() if pd.notna(row.get("Worker")) and str(row.get("Worker")).strip() not in ("", "nan") else current_worker

        # Lookup WO_ID from mapping
        wo_id = worker_to_wo.get(worker_val, np.nan) if worker_val else np.nan

        cleaned_rows.append({
            "Status": status_val,
            "ID": ts_id,
            "Revision": int(row.get("Revision", 0)) if pd.notna(row.get("Revision")) and str(row.get("Revision")).replace(".", "").isdigit() else 0,
            "Worker": worker_val,
            "Site": str(row.get("Site", "")).strip() if pd.notna(row.get("Site")) else np.nan,
            "END": str(row.get("End", row.get("END", ""))).strip() if pd.notna(row.get("End", row.get("END"))) else np.nan,
            "ST": float(row.get("ST", 0.0)) if pd.notna(row.get("ST")) else 0.0,
            "OT": float(row.get("OT", 0.0)) if pd.notna(row.get("OT")) else 0.0,
            "DT": float(row.get("DT", 0.0)) if pd.notna(row.get("DT")) else 0.0,
            "Others": float(row.get("Others", 0.0)) if pd.notna(row.get("Others")) else 0.0,
            "NB": float(row.get("NB", 0.0)) if pd.notna(row.get("NB")) else 0.0,
            "BillRate": row.get("BillRate", np.nan),
            "Amount": row.get("Amount", np.nan),
            "Comment": row.get("Comment", np.nan),
            "WO_ID": wo_id,
            "Legal Entity": row.get("Legal Entity", np.nan),
        })

    # 4. Create structured DataFrame matching exact 16 columns
    cleaned_df = pd.DataFrame(cleaned_rows, columns=TARGET_COLUMNS)

    logger.info(f"Cleaned dataset ready: {len(cleaned_df)} rows x {len(cleaned_df.columns)} columns.")

    # 5. Export to Excel (.xlsx) using openpyxl
    try:
        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            cleaned_df.to_excel(writer, sheet_name="Sheet1", index=False)
        logger.success(f"Saved formatted cleaned Excel file to: {output_file}")
        return output_file
    except PermissionError:
        alt_file = output_file.parent / f"{output_file.stem}_Updated.xlsx"
        logger.warning(f"Could not overwrite {output_file.name} because it is open in Microsoft Excel. Saving to {alt_file.name} instead...")
        with pd.ExcelWriter(alt_file, engine="openpyxl") as writer:
            cleaned_df.to_excel(writer, sheet_name="Sheet1", index=False)
        logger.success(f"Saved formatted cleaned Excel file to: {alt_file}")
        return alt_file

