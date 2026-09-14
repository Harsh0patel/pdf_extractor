from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import camelot
import pandas as pd
import pymupdf

from config import OUTPUT_DIR

# Minimum camelot confidence (0-1) for a table to be accepted
_MIN_CONFIDENCE = 0.5


class PDFExtractor:
    """Extract metadata, text and tables from a PDF.

    Tables are extracted with camelot: lattice (ruled tables) is preferred,
    stream is the fallback when lattice finds nothing confident. Resulting
    frames are post-processed to fill vertically-merged cells.
    """

    def __init__(self, pdf_path: str | Path, error_logger: Any = None, status_logger: Any = None, **kwargs: Any) -> None:
        """
        Args:
            pdf_path: Path to the PDF file.
            error_logger: Logger for problems (recoverable ones at INFO,
                known failures at ERROR, unexpected ones at CRITICAL).
            status_logger: Logger for the intended/known flow events.
            **kwargs: Global defaults applied to every extraction call,
                e.g. ``pages="all"``, ``password="secret"``.
        """
        self.pdf_path = Path(pdf_path)
        self.error_logger = error_logger
        self.status_logger = status_logger
        self.global_kwargs: dict[str, Any] = kwargs
        self._decrypted_buffer: io.BytesIO | None = None
        if not self.pdf_path.exists():
            if self.error_logger:
                self.error_logger.error(f"PDF not found: {self.pdf_path}")
                raise FileNotFoundError(f"PDF not found: {self.pdf_path}")

        try:
            self.doc = pymupdf.open(self.pdf_path)
        except Exception as exc:
            raise ValueError(f"Invalid or corrupted PDF: {self.pdf_path}") from exc

        self._used_password = False
        if self.doc.needs_pass:
            password = str(self.global_kwargs.get("password", ""))
            if not self.doc.authenticate(password):
                self.doc.close()
                raise PermissionError(f"PDF authentication failed: {self.pdf_path}")
            # A non-empty password worked: a real user-password PDF. Camelot
            # handles these itself via its ``password`` param.
            self._used_password = bool(password)

        if self.status_logger:
            self.status_logger.info(f"PDF_OPEN_SUCCESS | {self.pdf_path}")

    def _is_encrypted(self) -> bool:
        """True when the underlying file is encrypted (even with an empty
        user password, e.g. owner-restricted PDFs)."""
        return bool(self.doc.needs_pass or (self.doc.metadata or {}).get("encryption"))

    def _camelot_read(self, flavor: str, **kwargs: Any):
        """Call ``camelot.read_pdf`` on the best-available source.

        - Real user-password PDFs: pass ``password`` to camelot directly.
        - Owner-restricted PDFs (empty user password, extraction disallowed):
          camelot refuses them (PDFTextExtractionNotAllowed) even though
          PyMuPDF can read the text, so re-save the decrypted document to an
          IN-MEMORY buffer (no temp files on disk) and give that to camelot.
        - Falls back to the original file if the buffer path fails.
        """
        password = str(self.global_kwargs.get("password", ""))

        if self._used_password and password:
            return camelot.read_pdf(str(self.pdf_path), flavor=flavor, password=password, **kwargs)

        if not self._is_encrypted():
            return camelot.read_pdf(str(self.pdf_path), flavor=flavor, **kwargs)

        # Owner-restricted: build the decrypted buffer once, reuse it.
        if self._decrypted_buffer is None:
            try:
                self._decrypted_buffer = io.BytesIO(
                    self.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
                )
                if self.error_logger:
                    self.error_logger.info(
                        f"Encrypted PDF detected; camelot will read an in-memory decrypted copy: {self.pdf_path}"
                    )
            except Exception as e:
                if self.error_logger:
                    self.error_logger.info(f"In-memory decrypt failed, camelot will use the original file: {e}")

        if self._decrypted_buffer is not None:
            try:
                self._decrypted_buffer.seek(0)
                return camelot.read_pdf(self._decrypted_buffer, flavor=flavor, **kwargs)
            except Exception as e:
                if self.error_logger:
                    self.error_logger.info(f"camelot failed on decrypted copy, retrying original file: {e}")

        extra = {"password": password} if password else {}
        return camelot.read_pdf(str(self.pdf_path), flavor=flavor, **extra, **kwargs)

    def extract_metadata(self) -> dict[str, Any]:
        """Return document metadata plus page count and page sizes."""
        meta: dict[str, Any] = dict(self.doc.metadata or {})
        meta["page_count"] = self.doc.page_count
        meta["page_sizes"] = [
            {"index": i + 1, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2)}
            for i, p in enumerate(self.doc)
        ]
        return meta

    def extract_text(self, *args: Any, **kwargs: Any) -> str:
        """Extract all text from the document.

        Args:
            *args: Optional page numbers (0-based) to restrict extraction to.
            **kwargs: Forwarded to PyMuPDF's ``Page.get_text``,
                e.g. ``sort=True``, ``flags=pymupdf.TEXT_PRESERVE_WHITESPACE``.
        Returns:
            The extracted text (pages separated by form-feed characters).
        """
        pages = args if args else range(self.doc.page_count)
        chunks = [self.doc[p].get_text(**kwargs) for p in pages]
        return "\f".join(chunks)

    def extract_text_by_page(self, **kwargs: Any) -> list[str]:
        """Extract text as a list, one entry per page (``**kwargs`` forwarded to PyMuPDF)."""
        return [page.get_text(**kwargs) for page in self.doc]

    def extract_tables_lattice(self, *args: Any, **kwargs: Any) -> list[pd.DataFrame]:
        """Extract tables using camelot's lattice method (line-drawn tables)."""
        kwargs.setdefault("pages", "all")
        tables = self._camelot_read("lattice", **kwargs)
        return [table.df for table in tables]

    def extract_tables_stream(self, *args: Any, **kwargs: Any) -> list[pd.DataFrame]:
        """Extract tables using camelot's stream method (whitespace-aligned tables)."""
        kwargs.setdefault("pages", "all")
        tables = self._camelot_read("stream", **kwargs)
        return [table.df for table in tables]

    def extract_tables(self, *args: Any, **kwargs: Any) -> list[pd.DataFrame]:
        """Extract tables, preferring high-confidence lattice results.

        Strategy:
        1. Run lattice first (ruled tables). Accept it when it finds tables
           with good confidence.
        2. Only if lattice found nothing usable, fall back to stream.
        3. Post-process: fill vertically-merged cells down so split rows
           become complete records.
        """
        merged: list[pd.DataFrame] = []

        # 1. Lattice first - it is authoritative when present and confident
        lattice_frames: list[tuple[pd.DataFrame, float]] = []
        try:
            kwargs.setdefault("pages", "all")
            lattice_tables = self._camelot_read("lattice", **kwargs)
            lattice_frames = [
                (t.df, float(t.parsing_report.get("confidence", 0))) for t in lattice_tables
            ]
        except Exception as e:
            if self.error_logger:
                self.error_logger.info(f"Lattice extraction failed, tables might be missed: {e}")

        good_lattice = [
            df for df, conf in lattice_frames
            if conf >= _MIN_CONFIDENCE and not df.empty and df.shape[0] >= 2 and df.shape[1] >= 2
        ]
        if good_lattice:
            merged.extend(good_lattice)
        else:
            # 2. Stream fallback (whitespace-aligned tables)
            if lattice_frames:
                if self.error_logger:
                    self.error_logger.info(
                        "Lattice confidence below threshold "
                        f"({_MIN_CONFIDENCE}); falling back to stream"
                    )
            try:
                kwargs.setdefault("pages", "all")
                stream_tables = self._camelot_read("stream", **kwargs)
                stream_frames = [
                    (t.df, float(t.parsing_report.get("confidence", 0))) for t in stream_tables
                ]
            except Exception as e:
                stream_frames = []
                if self.error_logger:
                    self.error_logger.info(f"Stream extraction failed, tables might be missed: {e}")

            good_stream = [
                df for df, conf in stream_frames
                if conf >= _MIN_CONFIDENCE and not df.empty and df.shape[0] >= 2 and df.shape[1] >= 2
            ]
            merged.extend(good_stream)

            if not merged and (lattice_frames or stream_frames):
                if self.error_logger:
                    self.error_logger.warning(
                        "No table met the confidence threshold "
                        f"({_MIN_CONFIDENCE}); nothing accepted"
                    )

        # 3. Fill vertically-merged cells so continuation rows become records
        merged = [self._fill_merged_cells(df) for df in merged]
        return merged

    @staticmethod
    def _fill_merged_cells(df: pd.DataFrame, key_columns: int = 2) -> pd.DataFrame:
        """Fill vertically-merged cells down for the first ``key_columns`` columns.

        In tables with merged cells (e.g. one "State" cell spanning several
        district rows), camelot emits continuation rows with EMPTY key cells.
        Copying the value from the row above makes every row a complete record.
        Rows that are empty in ALL key columns AND have no data at all are
        dropped. Non-empty key cells start a new record, so genuinely separate
        rows (like wrapped-text rows carrying their own data) are untouched.
        """
        df = df.copy()
        key_columns = min(key_columns, df.shape[1])
        keys = df.iloc[:, :key_columns]
        data = df.iloc[:, key_columns:]

        # Row is a continuation when ALL key cells are empty
        key_empty = keys.replace("", pd.NA).isna().all(axis=1)
        row_has_data = data.replace("", pd.NA).notna().any(axis=1)

        # Drop rows that are empty everywhere (blank separator lines)
        df = df[~(key_empty & ~row_has_data)].copy()
        if df.empty:
            return df

        keys = df.iloc[:, :key_columns]
        data = df.iloc[:, key_columns:]
        key_empty = keys.replace("", pd.NA).isna().all(axis=1)

        # Forward-fill merged key cells on continuation rows
        keys = keys.replace("", pd.NA).ffill().fillna("")
        return pd.concat([keys, data], axis=1)

    def save_tables_to_json(self, tables: list[pd.DataFrame] | None = None, output_dir: str | Path = OUTPUT_DIR) -> list[Path]:
        """Save extracted tables to JSON files in ``output_dir``.

        Each table is written as a list of key-value objects where the FIRST
        row of the table is used as the column-name keys and subsequent rows
        are the values, e.g.::

            [{"Name": "Alice", "Department": "Engineering", "Salary": "90000"},
             {"Name": "Bob",   "Department": "Sales",       "Salary": "70000"}]
        """
        if tables is None:
            tables = self.extract_tables()

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        paths: list[Path] = []
        for i, df in enumerate(tables, start=1):
            header = df.iloc[0].tolist()
            body = df.iloc[1:]
            records = [dict(zip(header, row)) for row in body.itertuples(index=False, name=None)]
            file_path = out_dir / f"{self.pdf_path.stem}_table_{i}_{stamp}.json"
            file_path.write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            paths.append(file_path)
            if self.status_logger:
                self.status_logger.info(f"TABLE_SAVED | {file_path}")
        return paths

    def extract_all(self, **kwargs: Any) -> dict[str, Any]:
        """Extract metadata, text and tables in one call.

        Kwargs:
            pages: Page spec for table extraction (default ``"all"``).
            drop_tables: Set True to skip table extraction.
            Any other kwargs are forwarded to the table extraction calls.
        """
        kwargs.setdefault("pages", "all")
        drop_tables = kwargs.pop("drop_tables", False)
        result: dict[str, Any] = {
            "metadata": self.extract_metadata(),
            "text": self.extract_text(),
        }
        if not drop_tables:
            tables = self.extract_tables(**kwargs)
            result["tables"] = [
                {"page_shape": df.shape, "data": df.where(pd.notna(df), None).to_dict(orient="records")}
                for df in tables
            ]
        return result

    def __enter__(self) -> "PDFExtractor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.doc.close()