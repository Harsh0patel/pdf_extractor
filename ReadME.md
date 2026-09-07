# PDF Processing Service

A FastAPI-based service that extracts metadata, text, and tables from PDF files. Supports both lattice (line-drawn) and stream (whitespace-aligned) table extraction. Includes password-protected PDF support and structured logging.

## Features

- Extract PDF metadata (page count, page sizes, etc.)
- Extract text content from PDFs
- Extract tables using both lattice and stream methods
- Password-protected PDF support
- Structured logging to `logs/status.log` and `logs/error.log`
- Configurable via environment variables

## Requirements

- Python >= 3.13
- Java Runtime (for tabula-py table extraction)
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

### 1. Clone the repository

```bash
git clone <repository-url>
cd <repository-folder>
```

### 2. Install dependencies with uv

```bash
# Install uv if not already installed
pip install uv

# Create virtual environment and install dependencies
uv sync
```

This will create a `.venv` folder and install all dependencies from `pyproject.toml`.

### 3. Verify installation

```bash
uv run python -c "import fastapi, pymupdf, tabula; print('All dependencies installed successfully!')"
```

## Running the Server

### Start the server

```bash
uv run python main.py
```

The server will start on `http://0.0.0.0:8000` by default.

### Using environment variables

```bash
# Custom host and port
PDF_SERVICE_HOST=127.0.0.1 PDF_SERVICE_PORT=3000 uv run python main.py

# Custom log directory
PDF_SERVICE_LOG_DIR=/var/log/pdf-service uv run python main.py
```

### Available environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PDF_SERVICE_HOST` | `0.0.0.0` | Server host |
| `PDF_SERVICE_PORT` | `8000` | Server port |
| `PDF_SERVICE_LOG_DIR` | `logs` | Log directory path |
| `PDF_SERVICE_LOG_STATUS_LEVEL` | `DEBUG` | Status log level |
| `PDF_SERVICE_LOG_ERROR_LEVEL` | `ERROR` | Error log level |
| `PDF_SERVICE_LOG_FILE_MODE` | `a` | Log file mode (`a` append, `w` overwrite) |

## API Endpoints

### Health Check

```bash
curl http://localhost:8000/health
```

Response:
```json
{"status": "ok"}
```

### Process PDF

```bash
curl -X POST http://localhost:8000/process-pdf \
  -H "Content-Type: application/json" \
  -d '{"pdf_path": "/path/to/your/file.pdf"}'
```

For password-protected PDFs:
```bash
curl -X POST http://localhost:8000/process-pdf \
  -H "Content-Type: application/json" \
  -d '{"pdf_path": "/path/to/protected.pdf", "password": "your_password"}'
```

## Testing with Sample PDFs

The `samples/` folder contains test PDFs. Use the following Python script to test all scenarios:

### Test Script

```python
import urllib.request
import json
import os

BASE_URL = "http://localhost:8000"

def test_pdf(name, pdf_path, password=None):
    """Test a PDF file and print results."""
    print("=" * 60)
    print(f"TEST: {name}")
    print("=" * 60)

    body = {"pdf_path": os.path.abspath(pdf_path)}
    if password:
        body["password"] = password

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/process-pdf",
        data=data,
        headers={"Content-Type": "application/json"}
    )

    try:
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read().decode("utf-8"))

        print(f"  Status: {resp.status}")
        print(f"  Request ID: {result['request_id']}")
        print(f"  Result: {result['status']}")

        # Print metadata
        meta = result["data"]["metadata"]
        print(f"  Pages: {meta['page_count']}")

        # Print text preview
        text = result["data"]["text"][:200].replace("\f", " | ")
        print(f"  Text preview: {text}...")

        # Print tables
        tables = result["data"].get("tables", [])
        print(f"  Tables found: {len(tables)}")
        for i, t in enumerate(tables):
            print(f"    Table {i+1}: {t['page_shape'][0]} rows x {t['page_shape'][1]} cols")

    except urllib.error.HTTPError as e:
        error = json.loads(e.read().decode())
        print(f"  HTTP Error: {e.code}")
        print(f"  Detail: {error['detail']}")

    print()


# Run all tests
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("PDF PROCESSING SERVICE - TEST SUITE")
    print("=" * 60 + "\n")

    # Simple PDFs
    test_pdf("Simple PDF (invoice)", "samples/invoice.pdf")
    test_pdf("Simple PDF (no password)", "samples/simple_no_password.pdf")
    test_pdf("Multi-page PDF", "samples/multi_page.pdf")

    # PDFs with tables
    test_pdf("PDF with lattice table", "samples/lattice_table.pdf")
    test_pdf("PDF with employee table", "samples/employee_report.pdf")
    test_pdf("PDF with stream table (no lattice)", "samples/stream_table.pdf")

    # Password protected PDFs
    test_pdf(
        "Protected PDF (no password - should fail)",
        "samples/protected.pdf",
    )
    test_pdf(
        "Protected PDF (with password)",
        "samples/protected.pdf",
        password="secret123",
    )
    test_pdf(
        "Protected PDF with table (with password)",
        "samples/protected_with_table.pdf",
        password="secret123",
    )

    # Error cases
    test_pdf("Non-existent PDF (should fail)", "samples/does_not_exist.pdf")
    test_pdf("Corrupted PDF (should fail)", "samples/corrupted.pdf")

    print("=" * 60)
    print("ALL TESTS COMPLETED")
    print("=" * 60)
```

### Running the Test Script

```bash
# Save the script as test_api.py, then:
uv run python test_api.py
```

### Expected Output

```
============================================================
PDF PROCESSING SERVICE - TEST SUITE
============================================================

============================================================
TEST: Simple PDF (no password)
============================================================
  Status: 200
  Request ID: 550e8400-e29b-41d4-a716-446655440000
  Result: success
  Pages: 1
  Text preview: Invoice #12345 | Date: 2026-09-07 | Bill To: John Doe...
  Tables found: 0

============================================================
TEST: PDF with lattice table
============================================================
  Status: 200
  Request ID: 6ba7b810-9dad-11d1-80b4-00c04fd430c8
  Result: success
  Pages: 1
  Text preview: Employee Report Name Department Salary Alice...
  Tables found: 1
    Table 1: 5 rows x 3 cols

============================================================
TEST: Multi-page PDF
============================================================
  Status: 200
  Request ID: 6ba7b811-9dad-11d1-80b4-00c04fd430c8
  Result: success
  Pages: 3
  Text preview: Page 1 This is content on page 1...
  Tables found: 0

============================================================
TEST: Protected PDF (no password - should fail)
============================================================
  HTTP Error: 401
  Detail: PDF authentication failed: /path/to/samples/protected.pdf

============================================================
TEST: Protected PDF (with password)
============================================================
  Status: 200
  Request ID: 6ba7b812-9dad-11d1-80b4-00c04fd430c8
  Result: success
  Pages: 1
  Text preview: Confidential Document This document is password protected...
  Tables found: 0

============================================================
TEST: Non-existent PDF (should fail)
============================================================
  HTTP Error: 404
  Detail: PDF file not found: /path/to/samples/does_not_exist.pdf

============================================================
ALL TESTS COMPLETED
============================================================
```

## Logging

Logs are saved in the `logs/` directory:

- **status.log** — All successful operations (INFO level)
- **error.log** — Errors and exceptions (ERROR level)

Log format:
```
timestamp | request_id | log_level | source_file | line_number | message
```

Example:
```
2026-09-07 09:33:00.822 | 2f073680-fdb3-408a-8863-879ded03de3b | INFO | main.py | 51 | REQUEST_RECEIVED | POST /process-pdf
```

## Project Structure

```
.
├── main.py              # FastAPI application and endpoints
├── pdf_operations.py    # PDF extraction logic
├── logger.py            # Custom logger class
├── config.py            # Configuration settings
├── pyproject.toml       # Project dependencies
├── test_api.py          # Test script for API
├── samples/             # Sample PDF files for testing
│   ├── invoice.pdf
│   ├── employee_report.pdf
│   ├── multi_page.pdf
│   ├── protected.pdf
│   ├── protected_with_table.pdf
│   ├── simple_no_password.pdf
│   ├── lattice_table.pdf
│   ├── stream_table.pdf
│   └── corrupted.pdf
└── logs/                # Log output directory
    ├── status.log
    └── error.log
```

## Error Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Invalid request or corrupted PDF |
| 401 | PDF authentication failed (wrong password) |
| 404 | PDF file not found |
| 500 | Internal server error |
