from __future__ import annotations

import logging
from datetime import datetime, timezone
from logging import FileHandler
from pathlib import Path

from config import LOG_FILE_ENCODING, LOG_FILE_MODE, LOG_TIMESTAMP_FORMAT


class _RequestFormatter(logging.Formatter):
    """Format every record as:

        timestamp | request_id | log_level | source_file | line_number | message
    """

    def __init__(self, request_id: str) -> None:
        super().__init__()
        self.request_id = request_id

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            LOG_TIMESTAMP_FORMAT
        )
        return (
            f"{timestamp} | {self.request_id} | {record.levelname} | "
            f"{record.filename} | {record.lineno} | {record.getMessage()}"
        )


class Logger:
    """Configuration-only wrapper around a standard ``logging.Logger``.

    Its only job: attach a file handler + the line formatter to a standard
    logger for a specific file. No custom log methods, no level configuration —
    every record the caller logs is formatted and written to the file.
    """

    def __init__(self, request_id: str, log_file: str | Path) -> None:
        self.request_id = request_id
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_file.exists():
            self.log_file.touch()

        self._logger = self._configure_logger(name=f"{self.log_file.name}.{request_id}")

    def _configure_logger(self, name: str) -> logging.Logger:
        logger = logging.Logger(name)
        logger.propagate = False

        handler = FileHandler(self.log_file, mode=LOG_FILE_MODE, encoding=LOG_FILE_ENCODING)
        handler.setFormatter(_RequestFormatter(self.request_id))
        logger.addHandler(handler)
        return logger

    def close(self) -> None:
        """Detach handlers so the file handle is released when the request ends."""
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()

    def __getattr__(self, name: str):
        """Delegate everything else (debug, info, error, exception, ...) to the
        standard logging.Logger. No custom log methods defined in this class."""
        if name == "_logger":
            raise AttributeError(name)
        return getattr(self._logger, name)
