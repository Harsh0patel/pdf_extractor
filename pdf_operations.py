from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import camelot
import pandas as pd
import pymupdf

from config import OUTPUT_DIR

# Minimum camelot confidence (0-1) for a lattice table to be accepted
_MIN_CONFIDENCE = 0.5

# Number of leading rows probed when locating the header band
_MAX_HEADER_ROWS = 4

# Cells longer than this cannot be keys, headers or group names
_MAX_KEY_LENGTH = 80

_CLEAN_SPACES = re.compile(r"[ \t]+")
_CLEAN_NEWLINES = re.compile(r"\s*\n\s*")
_FOOTNOTE_ONLY = re.compile(r"^\s*\d{1,2}\s*$")
# Single-digit markers only: '2 Information' / 'Information 2' / '2010/11 1'.
# Two-digit numbers ('24 hour high') are part of the phrase, not footnotes.
_FOOTNOTE_PREFIX = re.compile(r"^\s*(\d)\s+(?=[A-Za-z0-9/])")
_FOOTNOTE_SUFFIX = re.compile(r"(?<=[A-Za-z/])\s+(\d)\s*$")
_WORDY_RE = re.compile(r"^(?=.*[A-Za-z].*[A-Za-z])[^\d]*$")
_YEAR_RE = re.compile(r"^\d{4}(?:\s*/\s*\d{2,4})?$")
_NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$|^\(\d[\d,]*(?:\.\d+)?\)$")
_YEAR_SUFFIX_FOOTNOTE = re.compile(r"^(\d{4}(?:/\d{2,4})?)\s+\d{1,2}$")


def _clean_cell(value: Any) -> str:
    """Normalize a raw cell value to a clean single-line string."""
    text = "" if value is None else str(value)
    text = text.replace("\u00a0", " ")
    text = text.replace("\r", "\n")
    text = _CLEAN_NEWLINES.sub(" ", text)
    text = _CLEAN_SPACES.sub(" ", text)
    return text.strip()


def _strip_footnote_markers(text: str) -> str:
    """Remove standalone footnote digits from a label.

    '1 \\n2010/11' -> '2010/11', 'Information 2' -> 'Information',
    '2010/11 1' -> '2010/11'. Phrases with real numbers ('24 hour high')
    and pure numbers are returned unchanged.
    """
    if not text or _FOOTNOTE_ONLY.match(text):
        return text
    m = _YEAR_SUFFIX_FOOTNOTE.match(text)
    if m:
        return m.group(1)
    cleaned = _FOOTNOTE_PREFIX.sub("", text, count=1)
    cleaned = _FOOTNOTE_SUFFIX.sub("", cleaned, count=1)
    return cleaned.strip()


def _looks_wordy(text: str) -> bool:
    """Heuristic: text reads like a word label rather than a number."""
    if not text or len(text) > _MAX_KEY_LENGTH or not _WORDY_RE.match(text):
        return False
    tokens = [t for t in re.split(r"[\s/]+", text) if t]
    return not all(t.isdigit() for t in tokens)


def _is_yearlike(text: str) -> bool:
    """True for '2010', '2009/10', '2010/11' style year labels."""
    return bool(_YEAR_RE.match(text))


def _is_numeric(text: str) -> bool:
    return bool(_NUM_RE.match(text)) and any(ch.isdigit() for ch in text)


def _cell_type(text: str) -> str:
    """Coarse cell type used for header-band detection."""
    if not text:
        return "empty"
    if _is_yearlike(text):
        return "year"
    if _is_numeric(text):
        return "num"
    if _looks_wordy(text):
        return "word"
    return "other"


def _parse_number(text: str) -> int | float | str:
    """Parse numeric-looking cells into int/float ('(123)' -> -123)."""
    t = text.replace(",", "")
    m = re.fullmatch(r"\((\d+(?:\.\d+)?)\)", t)
    if m:
        value = float(m.group(1))
        return -int(value) if value.is_integer() else -value
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d*\.\d+", t):
        return float(t)
    return text


# ============================================================================
# Geometry / structure analysis (built on camelot cell boundary flags)
# ============================================================================

def _build_cell_grid(t) -> list[list[dict[str, Any]]]:
    """Build a per-cell info grid from a camelot Table.

    grid[r][c] = {
        'text':  cleaned cell text,
        'hspan': True when the cell's left or right boundary line is missing
                 (region is horizontally merged in the source PDF),
        'vspan': True when the cell's top or bottom boundary line is missing
                 (region is vertically merged in the source PDF),
    }
    """
    df = t.df
    n_rows, n_cols = df.shape
    grid: list[list[dict[str, Any]]] = []
    for r in range(n_rows):
        row_cells: list[dict[str, Any]] = []
        for c in range(n_cols):
            text = _clean_cell(df.iat[r, c])
            try:
                cell = t.cells[r][c]
                hspan = bool(cell.hspan)
                vspan = bool(cell.vspan)
            except Exception:
                hspan = False  # geometry unavailable: treat as a plain cell
                vspan = False
            row_cells.append({"text": text, "hspan": hspan, "vspan": vspan})
        grid.append(row_cells)
    return grid


def _vertical_merge_runs(t, grid: list[list[dict[str, Any]]]) -> list[list[tuple[int, int, bool]]]:
    """Per column, partition rows into cell segments and mark merges.

    Returns: for each column c, a list of (start_row, end_row, is_merge)
    segments. A segment is a genuine vertical merge when it spans several
    rows and the cells inside it are missing their horizontal boundary
    lines (i.e. the PDF draws no line between them).
    """
    n_rows = len(grid)
    n_cols = len(grid[0]) if n_rows else 0
    runs: list[list[tuple[int, int, bool]]] = []
    for c in range(n_cols):
        segments: list[tuple[int, int]] = []
        start = 0
        for r in range(1, n_rows + 1):
            if r < n_rows:
                try:
                    above = t.cells[r - 1][c]
                    below = t.cells[r][c]
                    split = bool(above.bottom) or bool(below.top)
                except Exception:
                    split = True
            else:
                split = True
            if split:
                segments.append((start, r - 1))
                start = r
        marked: list[tuple[int, int, bool]] = []
        for a, b in segments:
            is_merge = False
            if b > a:
                for r in range(a, b):
                    try:
                        if not t.cells[r][c].bottom:
                            is_merge = True
                            break
                    except Exception:
                        pass
            marked.append((a, b, is_merge))
        runs.append(marked)
    return runs


def _merge_fill(grid: list[list[dict[str, Any]]], v_runs: list[list[tuple[int, int, bool]]], r: int, c: int) -> str:
    """Text of the cell at (r, c); when the cell is empty because it is the
    continuation of a vertically merged region, return the region's label."""
    text = grid[r][c]["text"]
    if text:
        return text
    if c < len(v_runs):
        for a, b, is_merge in v_runs[c]:
            if is_merge and a <= r <= b:
                return grid[a][c]["text"]
    return ""  # genuinely empty: never fabricate a value


def _is_merge_covered(v_runs: list[list[tuple[int, int, bool]]], r: int, c: int) -> bool:
    if c >= len(v_runs):
        return False
    return any(is_merge and a <= r <= b for a, b, is_merge in v_runs[c])


def _header_band(grid: list[list[dict[str, Any]]]) -> int:
    """Index of the LAST header row; -1 when the table has no header band.

    Row 0 is a header when its first cell is a word label and its remaining
    cells are years/labels rather than values of the same type the body
    has(a word/value row like 'Main character | Daniel Radcliffe' is DATA,
    not a header). Rows continue the band while they carry at most one
    non-year label beyond column 0 (e.g. spanning group titles).
    """
    if not grid:
        return -1
    n_cols = len(grid[0])
    end = -1
    for r in range(min(_MAX_HEADER_ROWS, len(grid))):
        row = grid[r]
        if all(not cell["text"] for cell in row):
            break
        first = row[0]["text"]
        rest = [cell["text"] for cell in row[1:]]
        filled = [x for x in rest if x]
        if r == 0:
            if not _looks_wordy(first):
                break
            # Header only when rest is label-like, not body-value-like
            has_years = any(_is_yearlike(x) for x in filled)
            non_year = [x for x in filled if not _is_yearlike(x)]
            body_row = grid[1] if len(grid) > 1 else None
            type_mismatch = False
            if body_row is not None and non_year:
                type_mismatch = any(
                    _cell_type(row[c + 1]["text"]) != "empty"
                    and _cell_type(row[c + 1]["text"]) != _cell_type(body_row[c + 1]["text"])
                    and _cell_type(body_row[c + 1]["text"]) != "empty"
                    for c in range(n_cols - 1)
                )
            if not (has_years or type_mismatch or len(non_year) == 0):
                break
        else:
            non_year = [x for x in filled if not _is_yearlike(x)]
            if len(non_year) > 1:
                break
        end = r
        # A fully empty next row also ends the band
        if r + 1 < len(grid) and all(not c2["text"] for c2 in grid[r + 1][1:]):
            break
    return end


def _column_pairs(t, grid: list[list[dict[str, Any]]], band_end: int) -> list[dict[str, Any]]:
    """Describe the logical column layout from the header band.

    Returns one entry per logical column group:
        {'cols': [c0, c1, ...],           # physical columns in the group
         'name': group title or '',       # spanning header text
         'leafs': [key per column or None]}  # None = label column
    Columns in one group are those not separated by a drawn vertical line
    in the last band row. The first column of a group whose leaf equals
    the group name is the row-label column (leaf None).
    """
    if band_end < 0 or not grid:
        return []
    n_cols = len(grid[0])
    groups: list[list[int]] = []
    current = [0]
    for c in range(1, n_cols):
        try:
            joined = not t.cells[band_end][c - 1].right
        except Exception:
            joined = False
        if joined:
            current.append(c)
        else:
            groups.append(current)
            current = [c]
    groups.append(current)

    n_rows = len(grid)

    # Classify columns: row-label chain columns vs value columns.
    # - column 0 is the row-identity column unless it clearly holds data
    # - other columns are label columns only when their band header is EMPTY
    #   and their body is wordy (the 'continuation label column' pattern);
    #   wordy band headers like 'Respondent A' or 'Actor' are DATA columns
    label_cols: set[int] = set()
    for c in range(n_cols):
        band = grid[band_end][c]["text"] if 0 <= band_end < n_rows else ""
        if c == 0:
            if not band or (not _is_yearlike(band) and not _is_numeric(band)):
                label_cols.add(0)
            continue
        body = [grid[r][c]["text"] for r in range(band_end + 1, n_rows) if grid[r][c]["text"]]
        if not body:
            continue
        numeric = sum(1 for x in body if _is_numeric(x))
        if numeric > len(body) / 2:
            continue  # numeric data column
        if not band and any(_looks_wordy(x) for x in body):
            label_cols.add(c)

    pairs: list[dict[str, Any]] = []
    seen_names: dict[str, int] = {}
    for cols in groups:
        # Spanning title lives STRICTLY ABOVE the band row; without one, the
        # first label column's band text acts as the group name.
        name = ""
        for r in range(band_end):
            for c in cols:
                if grid[r][c]["text"]:
                    name = _strip_footnote_markers(grid[r][c]["text"])
                    break
            if name:
                break
        if not name:
            for c in cols:
                if c in label_cols:
                    header_text = grid[band_end][c]["text"]
                    if header_text and _looks_wordy(header_text):
                        name = _strip_footnote_markers(header_text)
                        break

        leafs: list[str | None] = []
        group_leaf_counts: dict[str, int] = {}
        for c in cols:
            raw = grid[band_end][c]["text"]
            if c in label_cols:
                leafs.append(None)  # row-label chain column
                continue
            if not raw:
                leafs.append(f"column {c + 1}")  # data column with no header text
                continue
            leaf = _strip_footnote_markers(raw)  # years, words, anything
            count = group_leaf_counts.get(leaf, 0) + 1
            group_leaf_counts[leaf] = count
            if count > 1:
                leaf = f"{leaf} [{count}]"
            leafs.append(leaf)
        if name:
            seen_names[name] = seen_names.get(name, 0) + 1
            if seen_names[name] > 1:
                name = f"{name} [{seen_names[name]}]"
        pairs.append({
            "cols": cols,
            "name": name,
            "leafs": leafs,
            "label_cols": label_cols & set(cols),
        })
    return pairs


def _detect_sections(
    grid: list[list[dict[str, Any]]],
    v_runs: list[list[tuple[int, int, bool]]],
    band_end: int,
) -> dict[int, str]:
    """Rows that act as labels for the rows below them (e.g. '2010' or
    'Non-current assets' spanning the data area with no values of their
    own). Returns {row_index: section_label}."""
    sections: dict[int, str] = {}
    if not grid:
        return sections
    n_cols = len(grid[0])
    for r in range(max(band_end + 1, 1), len(grid)):
        row = grid[r]
        filled_idx = [c for c in range(n_cols) if row[c]["text"]]
        if not filled_idx or len(filled_idx) > 2:
            continue
        if any(_is_merge_covered(v_runs, r, c) for c in filled_idx):
            continue
        label_col = filled_idx[0]
        label = row[label_col]["text"]
        others = [row[c]["text"] for c in filled_idx[1:]]
        if _looks_wordy(label) and all(_is_yearlike(o) for o in others):
            sections[r] = label
        elif _is_yearlike(label) and all(_looks_wordy(o) for o in others):
            sections[r] = label
    return sections


def _descend(root: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """Walk/creates nested dicts along ``keys``, suffixing '[n]' on
    collisions with existing scalars so no data is overwritten."""
    node = root
    for key in keys:
        if not key:
            continue
        existing = node.get(key)
        if existing is None:
            node[key] = {}
            node = node[key]
        elif isinstance(existing, dict):
            node = existing
        else:
            base, n = key, 1
            while f"{base} [{n}]" in node and isinstance(node.get(f"{base} [{n}]"), dict):
                n += 1
            new_key = f"{base} [{n}]"
            node[new_key] = {}
            node = node[new_key]
    return node


def _count_distinct(node: Any, seen: set[str]) -> None:
    if isinstance(node, dict):
        for v in node.values():
            _count_distinct(v, seen)
    else:
        seen.add(str(node))


def _table_to_nested(item: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    """Convert one extracted table into a nested dict, or None when the
    table is degenerate (e.g. caption text mistaken for a table).

    Structure rules (all derived from the PDF's real cell geometry):
    - spanning header cells   -> group keys wrapping their columns
    - vertically merged cells -> group keys wrapping their rows (the label
      is inherited structurally, never copied as a value)
    - label rows w/o values   -> section keys for the rows below them
    - genuinely empty cells   -> stay empty; nothing is ever filled in
    """
    t = item["table"]
    grid = item["geometry"]["grid"]
    v_runs = item["geometry"]["v_runs"]
    n_rows = len(grid)
    n_cols = len(grid[0]) if n_rows else 0
    if n_rows < 1 or n_cols < 2:
        return None

    band_end = _header_band(grid)

    # Two-column tables: side-by-side key/value pairs. The first row is a
    # real header only when both its cells are single words (e.g.
    # 'Role | Actor'); otherwise it is a data record like
    # 'Main character | Daniel Radcliffe'.
    if n_cols == 2:
        r0 = grid[0]
        has_header = (
            _looks_wordy(r0[0]["text"])
            and _looks_wordy(r0[1]["text"])
            and len(r0[0]["text"].split()) == 1
            and len(r0[1]["text"].split()) == 1
        )
        out: dict[str, Any] = {}
        for r in range(1 if has_header else 0, n_rows):
            k, v = grid[r][0]["text"], grid[r][1]["text"]
            if k and v:
                out[k] = _parse_number(v) if _is_numeric(v) else v
        if not out:
            return None
        if has_header:
            return {r0[0]["text"]: out}
        seen: set[str] = set()
        _count_distinct(out, seen)
        return out if len(seen) >= 2 else None

    if band_end == -1:
        band_end = 0  # no header detected: treat the first row as band

    pairs = _column_pairs(t, grid, band_end)
    sections = _detect_sections(grid, v_runs, band_end)

    out = {}
    current_section: str | None = None
    for r in range(band_end + 1, n_rows):
        if r in sections:
            current_section = sections[r]
            continue
        if all(not cell["text"] for cell in grid[r]):
            continue

        path: list[str] = []
        if current_section:
            path.append(current_section)
        values: dict[str, Any] = {}
        for pair in pairs:
            # Group name (spanning title or label-column header) nests first
            if pair["name"] and (len(pair["cols"]) > 1 or pair["label_cols"]):
                path.append(pair["name"])
            for i, c in enumerate(pair["cols"]):
                leaf = pair["leafs"][i]
                if leaf is None:
                    if c not in pair["label_cols"]:
                        continue  # continuation column of a spanning header
                    text = _merge_fill(grid, v_runs, r, c)
                    if not text:
                        continue
                    text = _FOOTNOTE_PREFIX.sub("", text, count=1).strip() or text
                    path.append(text)  # label-chain level, in column order
                else:
                    text = grid[r][c]["text"]
                    if text:
                        values[leaf] = _parse_number(text) if _is_numeric(text) else text
        if not values:
            continue  # label/blank rows carry no record of their own

        node = _descend(out, path)
        for vkey, vval in values.items():
            existing = node.get(vkey)
            if vkey not in node:
                node[vkey] = vval
            elif isinstance(existing, dict):
                node[f"{vkey} [2]"] = vval
            elif existing != vval:
                node[f"{vkey} [2]"] = vval

    seen = set()
    _count_distinct(out, seen)
    if len(seen) < 2:
        return None  # degenerate: caption text, repeated placeholder, etc.
    return out


class PDFExtractor:
    """Extract metadata, text and tables from a PDF.

    Tables are extracted with camelot: lattice (ruled tables) is preferred,
    stream is the fallback when lattice finds nothing confident. Table
    structure (groups / sections / merged cells) is derived from camelot's
    per-cell geometry (boundary-line flags), not guessed from the text.
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
        kwargs.setdefault("split_text", True)  # split lines crossing cell borders (side-by-side key/value tables)

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

    def _extract_camelot_tables(self, flavor: str) -> list[Any]:
        """Run one camelot flavor and return its raw Table objects that pass
        basic validity checks (>= 2 rows, >= 2 columns, confidence threshold
        for lattice)."""
        try:
            kwargs: dict[str, Any] = {"pages": str(self.global_kwargs.get("pages", "all"))}
            tables = self._camelot_read(flavor, **kwargs)
        except Exception as e:
            if self.error_logger:
                self.error_logger.info(f"{flavor} extraction failed, tables might be missed: {e}")
            return []
        valid = [
            t for t in tables
            if t.df is not None and not t.df.empty and t.df.shape[0] >= 2 and t.df.shape[1] >= 2
        ]
        if flavor == "lattice":
            valid = [t for t in valid if float(t.parsing_report.get("confidence", 0)) >= _MIN_CONFIDENCE]
        return valid

    def extract_tables_lattice(self) -> list[pd.DataFrame]:
        """Extract tables using camelot's lattice method (line-drawn tables)."""
        return [t.df for t in self._extract_camelot_tables("lattice")]

    def extract_tables_stream(self) -> list[pd.DataFrame]:
        """Extract tables using camelot's stream method (whitespace-aligned tables)."""
        return [t.df for t in self._extract_camelot_tables("stream")]

    def extract_tables(self) -> list[dict[str, Any]]:
        """Extract tables with structure analysis.

        Lattice is preferred; stream is only used when lattice yields
        nothing. Each result carries the raw camelot Table, its grid/merge
        geometry and the built nested structure (``nested`` is None for
        degenerate tables such as caption fragments).
        """
        results: list[dict[str, Any]] = []
        for flavor in ("lattice", "stream"):
            for t in self._extract_camelot_tables(flavor):
                grid = _build_cell_grid(t)
                v_runs = _vertical_merge_runs(t, grid)
                item: dict[str, Any] = {
                    "table": t,
                    "df": t.df,
                    "confidence": float(t.parsing_report.get("confidence", 0)),
                    "geometry": {
                        "flavor": flavor,
                        "grid": grid,
                        "v_runs": v_runs,
                    },
                    "shape": list(t.df.shape),
                }
                try:
                    item["nested"] = _table_to_nested(item)
                except Exception as e:
                    item["nested"] = None
                    if self.error_logger:
                        self.error_logger.warning(f"Nested structure build failed for a table: {e}")
                results.append(item)
            if results:
                break  # lattice produced tables; skip the stream fallback
        return results

    def extract_all(self, **kwargs: Any) -> dict[str, Any]:
        """Extract metadata, text and tables in one call.

        Kwargs:
            pages: Page spec for table extraction (default ``"all"``).
            drop_tables: Set True to skip table extraction.
            Any other kwargs are forwarded to the extraction calls.
        """
        kwargs.setdefault("pages", "all")
        drop_tables = kwargs.pop("drop_tables", False)
        result: dict[str, Any] = {
            "metadata": self.extract_metadata(),
            "text": self.extract_text(),
        }
        if not drop_tables:
            self.global_kwargs.setdefault("pages", kwargs["pages"])
            tables = self.extract_tables()
            result["tables"] = [
                {
                    "shape": item["shape"],
                    "flavor": item["geometry"]["flavor"],
                    "confidence": round(item["confidence"], 4),
                    "nested": item["nested"],
                }
                for item in tables
                if item["nested"] is not None
            ]
        return result

    def save_tables_to_json(self, tables: list[dict[str, Any]] | None = None, output_dir: str | Path = OUTPUT_DIR) -> list[Path]:
        """Save each extracted table's nested structure to JSON in ``output_dir``.

        Accepts the list returned by :meth:`extract_tables` (or by
        :meth:`extract_all`, whose items already carry the built ``nested``
        structure). Degenerate tables (``nested is None``) are skipped.
        """
        if tables is None:
            tables = self.extract_tables()
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        paths: list[Path] = []
        index = 0
        for item in tables:
            nested = item.get("nested") if isinstance(item, dict) else None
            if nested is None:
                continue
            index += 1
            file_path = out_dir / f"{self.pdf_path.stem}_table_{index}_{stamp}.json"
            file_path.write_text(
                json.dumps(nested, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            paths.append(file_path)
            if self.status_logger:
                self.status_logger.info(f"TABLE_SAVED | {file_path}")
        return paths

    def __enter__(self) -> "PDFExtractor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.doc.close()
