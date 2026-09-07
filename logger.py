from __future__ import annotations

import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from config import (
    LOG_DIR,
    LOG_ERROR_FILE,
    LOG_ERROR_LEVEL,
    LOG_FILE_ENCODING,
    LOG_FILE_MODE,
    LOG_STATUS_FILE,
    LOG_STATUS_LEVEL,
    LOG_TIMESTAMP_FORMAT,
)


class Logger:
    """Custom logger that writes to two separate files:

    - ``logs/status.log`` — INFO and DEBUG level messages (everything except errors).
    - ``logs/error.log`` — ERROR and CRITICAL messages (errors and exceptions).

    Log format::
        timestamp | request_id | log_level | source_file | line_number | message
    """



    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self._ensure_log_dir()

        # --- status logger (INFO + DEBUG) -----------------------------------
        self._status_logger = logging.getLogger(f"status.{request_id}")
        self._status_logger.setLevel(getattr(logging, LOG_STATUS_LEVEL, logging.DEBUG))
        self._status_logger.propagate = False
        self._remove_handlers(self._status_logger)
        status_handler = logging.FileHandler(
            LOG_STATUS_FILE, mode=LOG_FILE_MODE, encoding=LOG_FILE_ENCODING
        )
        status_handler.setLevel(getattr(logging, LOG_STATUS_LEVEL, logging.DEBUG))
        status_handler.addFilter(_LevelFilter(max_level=logging.INFO))
        status_handler.setFormatter(logging.Formatter("%(message)s"))
        self._status_logger.addHandler(status_handler)

        # --- error logger (ERROR + CRITICAL) --------------------------------
        self._error_logger = logging.getLogger(f"error.{request_id}")
        self._error_logger.setLevel(getattr(logging, LOG_ERROR_LEVEL, logging.ERROR))
        self._error_logger.propagate = False
        self._remove_handlers(self._error_logger)
        error_handler = logging.FileHandler(
            LOG_ERROR_FILE, mode=LOG_FILE_MODE, encoding=LOG_FILE_ENCODING
        )
        error_handler.setLevel(getattr(logging, LOG_ERROR_LEVEL, logging.ERROR))
        error_handler.setFormatter(logging.Formatter("%(message)s"))
        self._error_logger.addHandler(error_handler)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def info(self, message: str) -> None:
        """Log an informational message → status.log."""
        self._write(self._status_logger, logging.INFO, message)

    def debug(self, message: str) -> None:
        """Log a debug message → status.log."""
        self._write(self._status_logger, logging.DEBUG, message)

    def error(self, message: str, exc_info: bool = False) -> None:
        """Log an error → error.log."""
        self._write(self._error_logger, logging.ERROR, message, exc_info=exc_info)

    def critical(self, message: str, exc_info: bool = False) -> None:
        """Log a critical error → error.log."""
        self._write(self._error_logger, logging.CRITICAL, message, exc_info=exc_info)

    def exception(self, message: str) -> None:
        """Convenience wrapper: logs ERROR with traceback → error.log."""
        self._write(self._error_logger, logging.ERROR, message, exc_info=True)

    # ------------------------------------------------------------------
    # High-level semantic methods
    # ------------------------------------------------------------------

    def request_received(self, endpoint: str, method: str = "POST") -> None:
        self.info(f"REQUEST_RECEIVED | {method} {endpoint}")

    def file_path_validated(self, pdf_path: str) -> None:
        self.info(f"FILE_PATH_VALIDATED | {pdf_path}")

    def pdf_open_success(self, pdf_path: str) -> None:
        self.info(f"PDF_OPEN_SUCCESS | {pdf_path}")

    def task_started(self, task_name: str) -> None:
        self.info(f"TASK_STARTED | {task_name}")

    def task_completed(self, task_name: str) -> None:
        self.info(f"TASK_COMPLETED | {task_name}")

    def response_sent(self, status_code: int) -> None:
        self.info(f"RESPONSE_SENT | status_code={status_code}")

    def log_error(self, message: str) -> None:
        self.error(message)

    def log_exception(self, message: str) -> None:
        self.exception(message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_log_dir() -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _remove_handlers(logger: logging.Logger) -> None:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    @staticmethod
    def _write(
        logger: logging.Logger,
        level: int,
        message: str,
        *,
        exc_info: bool = False,
    ) -> None:
        timestamp = datetime.now(timezone.utc).strftime(LOG_TIMESTAMP_FORMAT)[:-3] if LOG_TIMESTAMP_FORMAT.endswith("%f") else datetime.now(timezone.utc).strftime(LOG_TIMESTAMP_FORMAT)
        level_name = logging.getLevelName(level)

        # Walk the stack to find the caller's file and line number.
        source_file = "<unknown>"
        line_number = "-"
        frame = inspect.currentframe()
        if frame is not None:
            # Go up until we leave this module.
            caller_frame = frame.f_back
            while caller_frame is not None:
                caller_file = caller_frame.f_code.co_filename
                if caller_file != __file__:
                    source_file = os.path.basename(caller_file)
                    line_number = str(caller_frame.f_lineno)
                    break
                caller_frame = caller_frame.f_back

        # Request ID
        request_id = "SYSTEM"

        # Detect request_id from the Logger instance via a hack:
        # we store it on the frame locals — but that's fragile.
        # Instead, we extract it from the logger name.
        if "." in getattr(logger, "name", ""):
            candidate = logger.name.split(".", 1)[-1]
            if len(candidate) == 36 and candidate.count("-") == 4:  # UUID-like
                request_id = candidate

        formatted = f"{timestamp} | {request_id} | {level_name} | {source_file} | {line_number} | {message}"
        logger.log(level, formatted, exc_info=exc_info)


class _LevelFilter(logging.Filter):
    """Allow only messages whose level is <= ``max_level``."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level
