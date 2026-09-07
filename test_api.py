"""Test script for PDF Processing Service.

Run this script after starting the server:
    uv run python main.py

Then in another terminal:
    uv run python test_api.py
"""

import json
import os
import urllib.request

BASE_URL = "http://localhost:8000"


def test_pdf(name: str, pdf_path: str, password: str | None = None) -> None:
    """Test a PDF file and print results."""
    print("=" * 60)
    print(f"TEST: {name}")
    print("=" * 60)

    body: dict[str, str] = {"pdf_path": os.path.abspath(pdf_path)}
    if password:
        body["password"] = password

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/process-pdf",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read().decode("utf-8"))

        print(f"  Status: {resp.status}")
        print(f"  Request ID: {result['request_id']}")
        print(f"  Result: {result['status']}")

        meta = result["data"]["metadata"]
        print(f"  Pages: {meta['page_count']}")

        text = result["data"]["text"][:200].replace("\f", " | ")
        print(f"  Text preview: {text}...")

        tables = result["data"].get("tables", [])
        print(f"  Tables found: {len(tables)}")
        for i, t in enumerate(tables):
            print(f"    Table {i+1}: {t['page_shape'][0]} rows x {t['page_shape'][1]} cols")

    except urllib.error.HTTPError as e:
        error = json.loads(e.read().decode())
        print(f"  HTTP Error: {e.code}")
        print(f"  Detail: {error['detail']}")

    print()


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
