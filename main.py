from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
from uuid import uuid4

from config import APP_TITLE, HOST, PORT
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
    logger = Logger(request_id=request_id)
    pdf_path = Path(request.pdf_path)

    try:
        logger.request_received("/process-pdf", "POST")
        logger.file_path_validated(str(pdf_path))

        if not pdf_path.exists():
            logger.log_error(f"PDF file not found: {pdf_path}")
            raise HTTPException(status_code=404, detail=f"PDF file not found: {pdf_path}")
        if not pdf_path.is_file():
            logger.log_error(f"pdf_path is not a file: {pdf_path}")
            raise HTTPException(status_code=400, detail=f"pdf_path is not a file: {pdf_path}")

        logger.task_started("pdf_extraction")
        try:
            extract_kwargs = {}
            if request.password:
                extract_kwargs["password"] = request.password
            with PDFExtractor(pdf_path, logger=logger, **extract_kwargs) as extractor:
                data = extractor.extract_all()
        except FileNotFoundError as exc:
            logger.log_exception(f"File not found during extraction: {exc}")
            logger.task_failed("pdf_extraction")
            logger.response_sent(404)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            logger.log_exception(f"Invalid PDF: {exc}")
            logger.task_failed("pdf_extraction")
            logger.response_sent(400)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            logger.log_exception(f"PDF authentication failed: {exc}")
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface any extraction failure to the client
            logger.log_exception(f"PDF extraction failed: {exc}")
            raise HTTPException(status_code=500, detail=f"PDF extraction failed: {exc}") from exc

        logger.task_completed("pdf_extraction")
        logger.response_sent(200)
        return {"request_id": logger.request_id, "status": "success", "data": data}
    finally:
        logger.close()

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)