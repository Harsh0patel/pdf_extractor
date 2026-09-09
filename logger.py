from __future__ import annotations

import inspect
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

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
    """
    Log format::
        timestamp | request_id | log_level | source_file | line_number | message
    """

    def __init__(self, request_id: str) -> None:
        """
        Args:
            request_id: ID of the request, taken from the endpoint.
                The endpoint decides it (client-supplied or UUID4).
        """
        self.request_id = request_id
        self._ensure_log_files()

        # --- status logger (INFO + DEBUG) -----------------------------------
        self._status_logger = self._setup_logger(
            name=f"status.{self.request_id}",
            log_file=LOG_STATUS_FILE,
            level=LOG_STATUS_LEVEL,
            max_level=logging.INFO,  # cap at INFO for status
        )

        # --- error logger (ERROR + CRITICAL) --------------------------------
        self._error_logger = self._setup_logger(
            name=f"error.{self.request_id}",
            log_file=LOG_ERROR_FILE,
            level=LOG_ERROR_LEVEL,
            max_level=None,  # no cap for errors
        )


    def info(self, message: str) -> None:
        """Log an informational message to status.log."""
        self._write(self._status_logger, logging.INFO, message)

    def debug(self, message: str) -> None:
        """Log a debug message to status.log."""
        self._write(self._status_logger, logging.DEBUG, message)

    def error(self, message: str, exc_info: bool = False) -> None:
        """Log an error to error.log."""
        self._write(self._error_logger, logging.ERROR, message, exc_info=exc_info)

    def critical(self, message: str, exc_info: bool = False) -> None:
        """Log a critical error to error.log."""
        self._write(self._error_logger, logging.CRITICAL, message, exc_info=exc_info)

    def exception(self, message: str) -> None:
        """Convenience wrapper: logs ERROR with traceback to error.log."""
        self._write(self._error_logger, logging.ERROR, message, exc_info=True)



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

    def task_failed(self, task_name: str) -> None:
        self.info(f"TASK_FAILED | {task_name}")

    def close(self) -> None:
        """Close and remove all file handlers (call when the request is done)."""
        for py_logger in (self._status_logger, self._error_logger):
            self._remove_handlers(py_logger)


    def _setup_logger(
            self,
            name: str,
            log_file: Path,
            level: str,
            max_level: int | None = None,
    ) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, level, logging.DEBUG))
        logger.propagate = False
        self._remove_handlers(logger)

        handler = logging.FileHandler(
            log_file, mode=LOG_FILE_MODE, encoding=LOG_FILE_ENCODING
        )
        handler.setLevel(getattr(logging, level, logging.DEBUG))

        # only add max level filter if specified
        if max_level is not None:
            handler.addFilter(_LevelFilter(max_level=max_level))

        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger

    @staticmethod
    def _ensure_log_files() -> None:
        """Create log files if they don't exist."""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        for log_file in (LOG_STATUS_FILE, LOG_ERROR_FILE):
            if not log_file.exists():
                log_file.touch()

    @staticmethod
    def _remove_handlers(logger: logging.Logger) -> None:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def _write(
        self,
        py_logger: logging.Logger,
        level: int,
        message: str,
        *,
        exc_info: bool = False,
    ) -> None:
        timestamp = datetime.now(timezone.utc).strftime(LOG_TIMESTAMP_FORMAT)
        level_name = logging.getLevelName(level)

        # Walk the stack to find the caller's file and line number.
        source_file = "<unknown>"
        line_number = "-"
        frame = inspect.currentframe()
        if frame is not None:
            caller_frame = frame.f_back
            while caller_frame is not None:
                caller_file = caller_frame.f_code.co_filename
                if caller_file != __file__:
                    source_file = os.path.basename(caller_file)
                    line_number = str(caller_frame.f_lineno)
                    break
                caller_frame = caller_frame.f_back

        formatted = f"{timestamp} | {self.request_id} | {level_name} | {source_file} | {line_number} | {message}"
        py_logger.log(level, formatted, exc_info=exc_info)


class _LevelFilter(logging.Filter):
    """Allow only messages whose level is <= ``max_level``."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level
