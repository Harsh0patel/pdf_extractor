from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import camelot
import pandas as pd
import pymupdf


class PDFExtractor:
    """Extract metadata, text and tables from a PDF.

    Tables are extracted with BOTH tabula-py methods (lattice and stream)
    and merged so all detectable data is returned.
    """

    def __init__(self, pdf_path: str | Path, logger: Any = None, **kwargs: Any) -> None:
        """
        Args:
            pdf_path: Path to the PDF file.
            logger: Optional Logger instance for logging.
            **kwargs: Global defaults applied to every extraction call,
                e.g. ``pages="all"``, ``password="secret"``.
        """
        self.pdf_path = Path(pdf_path)
        self.logger = logger
        self.global_kwargs: dict[str, Any] = kwargs
        if not self.pdf_path.exists():
            if self.logger:
                self.logger.log_error(f"PDF not found: {self.pdf_path}")
            raise FileNotFoundError(f"PDF not found: {self.pdf_path}")

        try:
            self.doc = pymupdf.open(self.pdf_path)
        except Exception as exc:
            raise ValueError(f"Invalid or corrupted PDF: {self.pdf_path}") from exc

        if self.doc.needs_pass:
            if not self.doc.authenticate(str(self.global_kwargs.get("password", ""))):
                self.doc.close()
                raise PermissionError(f"PDF authentication failed: {self.pdf_path}")

        if self.logger:
            self.logger.pdf_open_success(str(self.pdf_path))

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
        tables = camelot.read_pdf(str(self.pdf_path), flavor='lattice', **kwargs)
        return [table.df for table in tables]

    def extract_tables_stream(self, *args: Any, **kwargs: Any) -> list[pd.DataFrame]:
        """Extract tables using camelot's stream method (whitespace-aligned tables)."""
        kwargs.setdefault("pages", "all")
        tables = camelot.read_pdf(str(self.pdf_path), flavor='stream', **kwargs)
        return [table.df for table in tables]

    def extract_tables(self, *args: Any, **kwargs: Any) -> list[pd.DataFrame]:
        """Extract tables with BOTH methods and merge the results.

        Runs lattice and stream, drops empty frames and removes duplicates
        (same shape and identical cell values) so all data is covered once.
        """
        merged: list[pd.DataFrame] = []
        seen: set[tuple] = set()
        all_frames: list[pd.DataFrame] = []

        # Try lattice, skip if it fails
        try:
            all_frames.extend(self.extract_tables_lattice(*args, **kwargs))
        except Exception as e:
            if self.logger:
                self.logger.log_error(f"Lattice extraction failed: {e}")

        # Always try stream
        try:
            all_frames.extend(self.extract_tables_stream(*args, **kwargs))
        except Exception as e:
            if self.logger:
                self.logger.log_error(f"Stream extraction failed: {e}")

        # Merge - skip tiny tables and exact duplicates
        for df in all_frames:
            # Skip tiny tables (likely false positives)
            if df.shape[0] < 2 or df.shape[1] < 2:
                continue
            if df.empty:
                continue
            # Create a hash key from cell values for dedup
            key = tuple(map(tuple, df.fillna("").astype(str).values.tolist()))
            if key not in seen:
                seen.add(key)
                merged.append(df)

        return merged

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

    def to_json(self, data: dict[str, Any], path: str | Path | None = None) -> str:
        """Serialize extraction results to JSON (optionally write to ``path``)."""
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def tables_to_csv(self, tables: list[pd.DataFrame], out_dir: str | Path = ".") -> list[Path]:
        """Write each extracted table to its own CSV file; returns the file paths."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, df in enumerate(tables, start=1):
            p = out_dir / f"{self.pdf_path.stem}_table_{i}.csv"
            df.to_csv(p, index=False)
            paths.append(p)
        return paths

    def close(self) -> None:
        """Close the underlying PDF document."""
        self.doc.close()

    def __enter__(self) -> "PDFExtractor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
