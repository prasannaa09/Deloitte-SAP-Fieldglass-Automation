# SAP Fieldglass Automation Bot

A production-grade Python automation solution designed to automate workflows within **SAP Fieldglass**, including downloading timesheets, extracting invoice data, extracting payroll data, merging Excel workbooks, generating consolidated reports, and emailing outputs automatically.

This project replaces a legacy Microsoft Power Automate Desktop (PAD) bot with a robust, maintainable, and scalable Python Playwright architecture.

---

## Scaffolding Architecture & Features

- **Python 3.12+**: Asynchronous foundation using `asyncio`.
- **Playwright Automation**: Cross-browser engine support (Chromium, Firefox, WebKit) with configurable timeouts, download routing, and headless options.
- **Pydantic Settings**: Strongly typed environment variable management via `pydantic-settings` and `.env` files.
- **Loguru Logging**: Structured, daily-rotating log files (`app_YYYY-MM-DD.log`), dedicated error logs (`error_YYYY-MM-DD.log`), and colorized console logging.
- **Data & Excel Processing**: Pandas and OpenPyXL integration for data manipulation and report generation.
- **Code Quality**: Pre-configured with `black`, `isort`, `mypy`, and `pytest`.

---

## Directory Structure

```
fieldglass_bot/
│
├── main.py              # Application entry point (initialization & shutdown)
├── requirements.txt      # Project dependencies
├── pyproject.toml        # Tools configuration (Black, isort, Mypy, pytest)
├── .env.example          # Environment variables template
├── .env                  # Environment configuration (git-ignored)
├── .gitignore            # Git exclusion rules
├── README.md             # Project documentation
│
├── automation/           # Playwright automation modules
│   ├── __init__.py
│   ├── browser.py        # Playwright browser manager & async session handler
│   ├── login.py          # SAP Fieldglass login handler stub
│   ├── navigation.py     # Navigation & menu interaction stub
│   ├── downloads.py      # Timesheet & Work Order export handler stub
│   ├── invoice.py        # Resource invoice data extraction stub
│   └── payroll.py        # Weekly timesheet payroll data extraction stub
│
├── excel/                # Excel manipulation & report generation
│   ├── __init__.py
│   ├── merge.py          # Excel merging functions
│   └── reports.py        # Invoice & Payroll report generators
│
├── config/               # Application configuration
│   ├── __init__.py
│   └── settings.py       # Pydantic BaseSettings class & environment loader
│
├── utils/                # Utility modules
│   ├── __init__.py
│   ├── logger.py         # Loguru logger setup
│   └── helpers.py        # Shared file and timestamp helpers
│
├── downloads/            # Output directory for downloaded files (git-ignored)
├── reports/              # Output directory for generated reports (git-ignored)
├── screenshots/          # Output directory for failure screenshots (git-ignored)
├── logs/                 # Output directory for Loguru log files (git-ignored)
└── tests/                # Automated test suite
    └── __init__.py
```

---

## Getting Started

### Prerequisites

- **Python 3.12** or higher installed on Windows/Linux/macOS.
- **Git**

### Installation

1. **Clone or navigate to the repository**:
   ```bash
   cd C:\Users\User\.gemini\antigravity-ide\scratch\fieldglass_bot
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv .venv
   ```

3. **Activate the virtual environment**:
   - **Windows (PowerShell)**:
     ```powershell
     .\.venv\Scripts\Activate.ps1
     ```
   - **Windows (CMD)**:
     ```cmd
     .\.venv\Scripts\activate.bat
     ```
   - **Linux / macOS**:
     ```bash
     source .venv/bin/activate
     ```

4. **Install Python dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

5. **Install Playwright browser binaries**:
   ```bash
   playwright install chromium
   ```

---

## Configuration

1. Copy `.env.example` to create your local `.env` file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` to supply your SAP Fieldglass configuration:
   ```env
   SAP_URL=https://www.fieldglass.net
   SAP_USERNAME=your_username
   SAP_PASSWORD=your_password

   BROWSER_TYPE=chromium
   HEADLESS=true
   DEFAULT_TIMEOUT=30000.0
   ```

---

## Running the Bot

To verify the installation and run the entry point test:

```bash
python main.py
```

Expected output:
- Loguru output printed to console stdout.
- Log files created in `logs/app_YYYY-MM-DD.log`.
- Playwright Chromium initialized and closed cleanly.

---

## Development Tools

### Code Formatting
Format code according to Black and isort rules:
```bash
black .
isort .
```

### Type Checking
Run static type validation:
```bash
mypy main.py config/ automation/ utils/ excel/
```

### Running Tests
Execute test suite using pytest:
```bash
pytest
```
