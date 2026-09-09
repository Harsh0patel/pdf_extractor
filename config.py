from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Server settings
APP_TITLE: str = "PDF Processing Service"
HOST: str = os.getenv("PDF_SERVICE_HOST", "0.0.0.0")
PORT: int = int(os.getenv("PDF_SERVICE_PORT", "8000"))

# Log settings
LOG_DIR: Path = Path(os.getenv("PDF_SERVICE_LOG_DIR", "logs"))
STATUS_LOG_FILE: str = os.getenv("PDF_SERVICE_STATUS_LOG_FILE", "status.log")
ERROR_LOG_FILE: str = os.getenv("PDF_SERVICE_ERROR_LOG_FILE", "error.log")
LOG_STATUS_FILE: Path = LOG_DIR / STATUS_LOG_FILE
LOG_ERROR_FILE: Path = LOG_DIR / ERROR_LOG_FILE

# Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_STATUS_LEVEL: str = os.getenv("PDF_SERVICE_LOG_STATUS_LEVEL", "DEBUG")
LOG_ERROR_LEVEL: str = os.getenv("PDF_SERVICE_LOG_ERROR_LEVEL", "ERROR")

# Log file encoding
LOG_FILE_ENCODING: str = os.getenv("PDF_SERVICE_LOG_FILE_ENCODING", "utf-8")

# Log file open mode: "a" for append, "w" for overwrite on startup
LOG_FILE_MODE: str = os.getenv("PDF_SERVICE_LOG_FILE_MODE", "a")

# Log timestamp format (strftime pattern)
LOG_TIMESTAMP_FORMAT: str = os.getenv(
    "PDF_SERVICE_LOG_TIMESTAMP_FORMAT", "%Y-%m-%d %H:%M:%S"
)

# PDF extraction settings
DEFAULT_TABLE_PAGES: str = os.getenv("PDF_SERVICE_TABLE_PAGES", "all")
