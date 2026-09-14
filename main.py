from pathlib import Path
from uuid import uuid4

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from config import (
    APP_TITLE,
    HOST,
    PORT,
    LOG_ERROR_FILE,
    LOG_STATUS_FILE,
)
from logger import Logger
from pdf_operations import PDFExtractor

app = FastAPI(title=APP_TITLE)

class ProcessPDFRequest(BaseModel):
    """Body for POST /process-pdf.

    ``request_id`` is optional: if omitted, the server generates one with UUID4.
    """

    request_id: str | None = None
    pdf_path: str
    password: str | None = None

    @field_validator("request_id")
    @classmethod
    def normalize_request_id(cls, value: str | None) -> str | None:
        """Treat blank or placeholder values (e.g. Swagger UI's "string"
        example) as absent so the server generates a UUID4."""
        if value is None:
            return None
        value = value.strip()
        if not value or value.lower() == "string":
            return None
        return value

    @field_validator("pdf_path")
    @classmethod
    def validate_pdf_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("pdf_path must not be empty")
        if not path.lower().endswith(".pdf"):
            raise ValueError("pdf_path must point to a .pdf file")
        return path


@app.get("/health")
def health() -> dict:
    """Liveness check."""
    return {"status": "ok"}

@app.post("/process-pdf")
def process_pdf(request: ProcessPDFRequest) -> dict:
    """Extract metadata, text and tables from the PDF at ``pdf_path``."""
    request_id = request.request_id or str(uuid4())
    status_logger = Logger(request_id=request_id, log_file=LOG_STATUS_FILE)
    error_logger = Logger(request_id=request_id, log_file=LOG_ERROR_FILE)
    pdf_path = Path(request.pdf_path)

    try:
        status_logger.info("REQUEST_RECEIVED | POST /process-pdf")
        status_logger.info(f"FILE_PATH_VALIDATED | {pdf_path}")

        if not pdf_path.exists():
            error_logger.error(f"PDF file not found: {pdf_path}")
            status_logger.info("RESPONSE_SENT | status_code=404")
            raise HTTPException(status_code=404, detail=f"PDF file not found: {pdf_path}")
        if not pdf_path.is_file():
            error_logger.error(f"pdf_path is not a file: {pdf_path}")
            status_logger.info("RESPONSE_SENT | status_code=400")
            raise HTTPException(status_code=400, detail=f"pdf_path is not a file: {pdf_path}")

        status_logger.info("TASK_STARTED | pdf_extraction")
        try:
            extract_kwargs = {}
            if request.password:
                extract_kwargs["password"] = request.password
            with PDFExtractor(
                pdf_path, error_logger=error_logger, status_logger=status_logger, **extract_kwargs
            ) as extractor:
                data = extractor.extract_all()
                if data.get("tables"):
                    saved_files = extractor.save_tables_to_json(
                        [pd.DataFrame(t["data"]) for t in data["tables"]]
                    )
                    data["tables_saved_to"] = [str(p) for p in saved_files]
        except FileNotFoundError as exc:
            error_logger.error(f"File not found during extraction: {exc}")
            status_logger.info("TASK_FAILED | pdf_extraction")
            status_logger.info("RESPONSE_SENT | status_code=404")
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            error_logger.error(f"Invalid PDF: {exc}")
            status_logger.info("TASK_FAILED | pdf_extraction")
            status_logger.info("RESPONSE_SENT | status_code=400")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            error_logger.error(f"PDF authentication failed: {exc}")
            status_logger.info("TASK_FAILED | pdf_extraction")
            status_logger.info("RESPONSE_SENT | status_code=401")
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface any extraction failure to the client
            error_logger.critical(f"PDF extraction failed unexpectedly: {exc}")
            status_logger.info("TASK_FAILED | pdf_extraction")
            status_logger.info("RESPONSE_SENT | status_code=500")
            raise HTTPException(status_code=500, detail=f"PDF extraction failed: {exc}") from exc

        status_logger.info("TASK_COMPLETED | pdf_extraction")
        status_logger.info("RESPONSE_SENT | status_code=200")
        return {"request_id": request_id, "status": "success", "data": data}
    finally:
        status_logger.close()
        error_logger.close()

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)