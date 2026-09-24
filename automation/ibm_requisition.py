"""IBM SAP Fieldglass Requisition Search and Extraction module.

This module automates:
1. Searching for Requisition / Job Posting IDs via the Fieldglass Global Search.
2. Navigating to the Job Posting Detail view (e.g. job_posting_detail.do).
3. Extracting metadata, key-value fields, rates, dates, and descriptions.
4. Exporting the extracted data to JSON, CSV, and Excel reports.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
import pandas as pd
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from config.settings import Settings


# Global search input selectors on SAP Fieldglass header
GLOBAL_SEARCH_SELECTORS = [
    "#s_globalSearchForm_searchText",
    "input[name='searchStr']",
    "input[name='s_globalSearchForm_searchText']",
    "#globalSearchInput",
    "input[placeholder*='Search']",
    "input.globalSearchInput",
]

# Loading overlays / spinners
LOADING_SELECTORS = ".sapUiBlockLayer, .loading-overlay, .busy-indicator, .spinner, .blockOverlay"


async def search_and_open_requisition(page: Page, req_id: str, timeout: float = 30000.0) -> bool:
    """Execute global search for a requisition ID and ensure navigation to job_posting_detail.do.

    Args:
        page: Active Playwright Page instance (logged in to SAP Fieldglass).
        req_id: Requisition / Job Posting ID (e.g., 'IBMFG2JP00040793').
        timeout: Maximum wait timeout in milliseconds.

    Returns:
        bool: True if landed on requisition detail page, False otherwise.
    """
    cleaned_req_id = req_id.strip()
    logger.info(f"Initiating global search for Requisition ID: '{cleaned_req_id}'")

    # Step 1: Locate the global search input
    search_input = None
    for selector in GLOBAL_SEARCH_SELECTORS:
        loc = page.locator(selector)
        if await loc.count() > 0 and await loc.first.is_visible():
            search_input = loc.first
            logger.debug(f"Found global search input using selector: {selector}")
            break

    if not search_input:
        # Check if search icon button needs to be clicked to reveal input
        search_icon = page.locator("#globalSearchBtn, .globalSearchIcon, button[aria-label*='Search'], [id*='searchButton']")
        if await search_icon.count() > 0 and await search_icon.first.is_visible():
            await search_icon.first.click()
            await page.wait_for_timeout(500)
            for selector in GLOBAL_SEARCH_SELECTORS:
                loc = page.locator(selector)
                if await loc.count() > 0 and await loc.first.is_visible():
                    search_input = loc.first
                    break

    if not search_input:
        logger.error("Could not find global search input box on current page!")
        return False

    # Step 2: Fill search input and submit
    await search_input.click()
    await search_input.fill("")
    await search_input.fill(cleaned_req_id)
    await page.wait_for_timeout(300)

    logger.info(f"Submitting search query for '{cleaned_req_id}'...")
    # Press Enter or click search button
    await search_input.press("Enter")

    # Step 3: Wait for redirection or search results list
    logger.info("Waiting for navigation to requisition detail page...")
    try:
        # Direct match redirect pattern: job_posting_detail.do
        await page.wait_for_url(lambda u: "job_posting_detail.do" in u or "job_posting" in u, timeout=timeout)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        logger.success(f"Successfully landed on Requisition Detail page: {page.url}")
        return True
    except PlaywrightTimeoutError:
        logger.warning("Did not immediately redirect to job_posting_detail.do. Checking search result listings...")

    # If not automatically redirected, check if search result links appeared
    result_link = page.locator(
        f"a:has-text('{cleaned_req_id}'), .searchResultsGroupList a, a[href*='job_posting_detail.do']"
    )
    if await result_link.count() > 0:
        logger.info("Found matching search result link. Clicking to navigate...")
        await result_link.first.click()
        await page.wait_for_url(lambda u: "job_posting_detail.do" in u, timeout=timeout)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        logger.success(f"Navigated to Requisition Detail page: {page.url}")
        return True

    # Check for 'no results' or error indicator
    if await page.locator("text='No results found', text='No Available Card found'").count() > 0:
        logger.error(f"No results found in SAP Fieldglass for Requisition ID: '{cleaned_req_id}'")
        return False

    logger.warning(f"Current page after search: {page.url} ({await page.title()})")
    return "job_posting_detail.do" in page.url


async def extract_job_posting_details(page: Page, req_id: str = "") -> dict[str, Any]:
    """Extract structured data fields from the job_posting_detail.do page.

    Args:
        page: Active Playwright Page instance positioned on job_posting_detail.do.
        req_id: Expected Requisition ID (fallback if not parsed from page).

    Returns:
        dict[str, Any]: Extracted details and all parsed key-value attributes.
    """
    logger.info("Extracting requisition details from page...")
    extracted_at = datetime.utcnow().isoformat() + "Z"
    current_url = page.url

    # Execute DOM query script in browser context to reliably extract labels and values
    extracted_raw = await page.evaluate(
        """() => {
            const data = {};
            const standardFields = {};

            // 1. Page Header Title & Subtitle
            const pageTitleElem = document.querySelector('h1, .pageTitle, #pageTitle, .headerTitle, .fd-action-header__title');
            standardFields['Page_Title'] = pageTitleElem ? pageTitleElem.innerText.trim() : '';

            // 2. Extract Key-Value rows from standard tables and field groups
            // Strategy A: Tables with th/td or .label / .value pairs
            const rows = document.querySelectorAll('tr, .fieldRow, .form-row, .fd-form-item, dl, .fieldGroup');
            rows.forEach(r => {
                // Table row label/value
                const labelCell = r.querySelector('th, td.label, .label, .fieldLabel, label, dt');
                const valCell = r.querySelector('td.value, .value, .fieldValue, dd, .formValue');
                if (labelCell && valCell) {
                    const k = labelCell.innerText.replace(/[:*]/g, '').trim();
                    const v = valCell.innerText.trim();
                    if (k && v && !data[k]) {
                        data[k] = v;
                    }
                }
            });

            // Strategy B: Any label tag associated with value elements
            document.querySelectorAll('label').forEach(lbl => {
                const k = lbl.innerText.replace(/[:*]/g, '').trim();
                if (k && k.length < 80) {
                    const parent = lbl.parentElement;
                    if (parent) {
                        const nextSibling = lbl.nextElementSibling;
                        if (nextSibling && (nextSibling.classList.contains('value') || nextSibling.tagName === 'SPAN' || nextSibling.tagName === 'DIV')) {
                            const v = nextSibling.innerText.trim();
                            if (v && !data[k]) data[k] = v;
                        }
                    }
                }
            });

            // 3. Status and ID badge
            const statusElem = document.querySelector('.status, .badge, .statusValue, [class*="status"], .fd-badge');
            if (statusElem) {
                standardFields['Status'] = statusElem.innerText.trim();
            }

            // 4. Job Description & Qualifications sections
            const descElem = document.querySelector('#jobDescription, [id*="description"], .jobDescriptionContent, .descriptionText');
            if (descElem) {
                standardFields['Job_Description'] = descElem.innerText.trim();
            }

            return {
                standard: standardFields,
                fields: data
            };
        }"""
    )

    fields = extracted_raw.get("fields", {})
    standard = extracted_raw.get("standard", {})

    # Helper function to find a field by case-insensitive key patterns
    def get_field_fuzzy(*patterns: str) -> str:
        for p in patterns:
            for k, v in fields.items():
                if p.lower() in k.lower():
                    return v.strip()
        return ""

    # Synthesize standard fields
    job_posting_id = (
        get_field_fuzzy("Job Posting ID", "Requisition ID", "Posting ID", "Req ID", "ID")
        or req_id
    )
    job_title = (
        standard.get("Page_Title")
        or get_field_fuzzy("Job Title", "Posting Title", "Title", "Position Title")
    )
    status = (
        standard.get("Status")
        or get_field_fuzzy("Status", "Job Posting Status", "Posting Status")
    )
    hiring_manager = get_field_fuzzy("Hiring Manager", "Manager", "Supervisor", "Requestor")
    business_unit = get_field_fuzzy("Business Unit", "Department", "Division", "Organization")
    start_date = get_field_fuzzy("Start Date", "Target Start Date", "Begin Date", "Effective Date")
    end_date = get_field_fuzzy("End Date", "Target End Date", "Expiration Date", "Completion Date")
    work_location = get_field_fuzzy("Work Location", "Location", "Site", "Work Address", "Address")
    bill_rate = get_field_fuzzy("Bill Rate", "Max Bill Rate", "Target Bill Rate", "Pay Rate", "Rate", "Budget")
    positions = get_field_fuzzy("Positions", "Number of Positions", "Openings", "Headcount")
    job_description = standard.get("Job_Description") or get_field_fuzzy("Description", "Job Summary")

    record: dict[str, Any] = {
        "Requisition_ID": job_posting_id or req_id,
        "Job_Title": job_title,
        "Status": status,
        "Hiring_Manager": hiring_manager,
        "Business_Unit": business_unit,
        "Start_Date": start_date,
        "End_Date": end_date,
        "Work_Location": work_location,
        "Bill_Rate": bill_rate,
        "Positions": positions,
        "Job_Description": job_description,
        "Extracted_At": extracted_at,
        "Source_URL": current_url,
        "All_Extracted_Fields": fields,
    }

    logger.success(
        f"Extracted data for '{record['Requisition_ID']}': Title='{record['Job_Title']}', "
        f"Status='{record['Status']}', Manager='{record['Hiring_Manager']}'"
    )
    return record


def save_requisition_records(
    records: list[dict[str, Any]],
    output_dir: Path,
    formats: list[str] | None = None,
) -> dict[str, Path]:
    """Save extracted requisition records to JSON, Excel, and CSV formats.

    Args:
        records: List of extracted requisition record dictionaries.
        output_dir: Directory where report files should be saved.
        formats: Formats to export ('json', 'excel', 'csv'). Defaults to all three.

    Returns:
        dict[str, Path]: Paths to the written output files.
    """
    if formats is None:
        formats = ["json", "excel", "csv"]

    output_dir.mkdir(parents=True, exist_ok=True)
    saved_files: dict[str, Path] = {}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Flatten records for tabular formats (Excel / CSV)
    flat_rows = []
    for r in records:
        flat = {k: v for k, v in r.items() if k != "All_Extracted_Fields"}
        # Include detailed raw fields as prefixed columns if desired
        all_fields = r.get("All_Extracted_Fields", {})
        for k, v in all_fields.items():
            col_name = f"Detail_{k}"
            if col_name not in flat:
                flat[col_name] = v
        flat_rows.append(flat)

    # 1. JSON output
    if "json" in formats:
        json_path = output_dir / "ibm_requisitions.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(records)} record(s) to JSON: {json_path}")
        saved_files["json"] = json_path

    # 2. Excel and CSV outputs via Pandas
    if flat_rows and ("excel" in formats or "csv" in formats):
        df = pd.DataFrame(flat_rows)

        if "excel" in formats:
            excel_path = output_dir / "ibm_requisitions.xlsx"
            df.to_excel(excel_path, index=False, engine="openpyxl")
            logger.info(f"Saved {len(records)} record(s) to Excel: {excel_path}")
            saved_files["excel"] = excel_path

        if "csv" in formats:
            csv_path = output_dir / "ibm_requisitions.csv"
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            logger.info(f"Saved {len(records)} record(s) to CSV: {csv_path}")
            saved_files["csv"] = csv_path

    return saved_files
