from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pymupdf

# constants
_MAX_KEY_LENGTH = 80
_CLEAN_SPACES = re.compile(r"[ \t]+")
_CLEAN_NEWLINES = re.compile(r"\s*\n\s*")
_FOOTNOTE_ONLY = re.compile(r"^\s*\d{1,2}\s*$")
_FOOTNOTE_PREFIX = re.compile(r"^\s*(\d)\s+(?=[A-Za-z0-9/])")
_FOOTNOTE_SUFFIX = re.compile(r"(?<=[A-Za-z/])\s+(\d)\s*$")
_WORDY_RE = re.compile(r"^(?=.*[A-Za-z].*[A-Za-z])[^\d]*$")
_YEAR_RE = re.compile(r"^\d{4}(?:\s*/\s*\d{2,4})?$")
_NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$|^\(\d[\d,]*(?:\.\d+)?\)$")
_YEAR_SUFFIX_FN = re.compile(r"^(\d{4}(?:/\d{2,4})?)\s+\d{1,2}$")
_MAX_HEADER_ROWS = 4

OUTPUT_DIR = Path("output")

@dataclass
class TableNode:
    """Represents a hierarchical node in a table tree structure."""
    name: str
    value: Any = None
    children: list[TableNode] = field(default_factory=list)
    parent: TableNode | None = field(default=None, repr=False)

    def add_child(self, name: str, value: Any = None) -> TableNode:
        """Find existing child or create a new child node."""
        for child in self.children:
            if child.name == name and child.value is None and value is None:
                return child

        child_node = TableNode(name=name, value=value, parent=self)
        self.children.append(child_node)
        return child_node

    def add_path(self, path: list[str], values: dict[str, Any]) -> None:
        """Traverse or build nodes along `path` and attach key-value pairs at the leaf."""
        curr = self
        for key in path:
            if key:
                curr = curr.add_child(key)

        for leaf_key, leaf_val in values.items():
            curr.add_child(name=leaf_key, value=leaf_val)

    def get_leaves(self) -> list[Any]:
        """Collect all scalar leaf values in the subtree."""
        if not self.children:
            return [self.value] if self.value is not None else []
        leaves: list[Any] = []
        for child in self.children:
            leaves.extend(child.get_leaves())
        return leaves

    def get_all_keys(self):
        """Collect all structural node names/keys in the subtree."""
        keys: set[str] = set()
        for child in self.children:
            if child.name:
                keys.add(child.name)
            keys.update(child.get_all_keys())
        return keys

    def to_dict(self) -> Any:
        """Serialize the tree into nested dicts, lists, or primitive values."""
        if not self.children:
            return self.value

        result: dict[str, Any] = {}
        for child in self.children:
            child_data = child.to_dict()
            if child.name in result:
                # Convert duplicate sibling nodes into lists
                if not isinstance(result[child.name], list):
                    result[child.name] = [result[child.name]]
                result[child.name].append(child_data)
            else:
                result[child.name] = child_data
        return result


# --- CELL HELPERS ---

def _clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u00a0", " ").replace("\r", "\n")
    text = _CLEAN_NEWLINES.sub(" ", text)
    text = _CLEAN_SPACES.sub(" ", text)
    return text.strip()


def _strip_footnote_markers(text: str) -> str:
    if not text or _FOOTNOTE_ONLY.match(text):
        return text
    m = _YEAR_SUFFIX_FN.match(text)
    if m:
        return m.group(1)
    cleaned = _FOOTNOTE_PREFIX.sub("", text, count=1)
    cleaned = _FOOTNOTE_SUFFIX.sub("", cleaned, count=1)
    return cleaned.strip()


def _looks_wordy(text: str) -> bool:
    if not text or len(text) > _MAX_KEY_LENGTH or not _WORDY_RE.match(text):
        return False
    tokens = [t for t in re.split(r"[\s/]+", text) if t]
    return not all(t.isdigit() for t in tokens)


def _is_yearlike(text: str) -> bool:
    return bool(_YEAR_RE.match(text))


def _is_numeric(text: str) -> bool:
    return bool(_NUM_RE.match(text)) and any(ch.isdigit() for ch in text)


def _cell_type(text: str) -> str:
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


# --- GRID & RUN BUILDERS ---

def _build_cell_grid_from_pymupdf(raw: list[list[str | None]]) -> list[list[dict[str, Any]]]:
    grid: list[list[dict[str, Any]]] = []
    for row in raw:
        grid_row: list[dict[str, Any]] = []
        for cell in row:
            vspan = cell is None
            text = _clean_cell("" if cell is None else cell)
            grid_row.append({"text": text, "hspan": False, "vspan": vspan})
        grid.append(grid_row)
    return grid


def _vertical_merge_runs_from_grid(
    grid: list[list[dict[str, Any]]],
) -> list[list[tuple[int, int, bool]]]:
    if not grid:
        return []
    n_rows = len(grid)
    n_cols = len(grid[0])
    runs: list[list[tuple[int, int, bool]]] = []

    for c in range(n_cols):
        segments: list[tuple[int, int, bool]] = []
        start = 0
        in_merge = False

        for r in range(1, n_rows):
            cell = grid[r][c]
            prev_cell = grid[r - 1][c]

            if cell["vspan"] and not prev_cell["vspan"]:
                segments.append((start, r - 1, False))
                start = r - 1
                in_merge = True
            elif not cell["vspan"] and in_merge:
                segments.append((start, r - 1, True))
                start = r
                in_merge = False

        segments.append((start, n_rows - 1, in_merge))
        runs.append(segments)

    return runs


def _merge_fill(
    grid: list[list[dict[str, Any]]],
    v_runs: list[list[tuple[int, int, bool]]],
    r: int,
    c: int,
) -> str:
    text = grid[r][c]["text"]
    if text:
        return text
    if c < len(v_runs):
        for a, b, is_merge in v_runs[c]:
            if is_merge and a <= r <= b:
                return grid[a][c]["text"]
    return ""


def _is_merge_covered(
    v_runs: list[list[tuple[int, int, bool]]],
    r: int,
    c: int,
) -> bool:
    if c >= len(v_runs):
        return False
    return any(is_merge and a <= r <= b for a, b, is_merge in v_runs[c])


# --- STRUCTURE ANALYSIS ---

def _header_band(grid: list[list[dict[str, Any]]]) -> int:
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
            all_same_wordy = (
                len(filled) > 0
                and all(x == filled[0] for x in filled)
                and _looks_wordy(filled[0])
            )
            if not (has_years or type_mismatch or len(non_year) == 0 or all_same_wordy):
                break
        else:
            non_year = [x for x in filled if not _is_yearlike(x)]
            if len(non_year) > 1:
                break
        end = r
        if r + 1 < len(grid) and all(not c2["text"] for c2 in grid[r + 1][1:]):
            break
    return end


def _column_pairs_from_grid(
    grid: list[list[dict[str, Any]]],
    band_end: int,
    raw: list[list[str | None]],
) -> list[dict[str, Any]]:
    if band_end < 0 or not grid:
        return []
    n_cols = len(grid[0])
    n_rows = len(grid)

    band_raw = raw[band_end] if band_end < len(raw) else [None] * n_cols

    groups: list[list[int]] = []
    current: list[int] = [0]
    for c in range(1, n_cols):
        if band_raw[c] is None:
            current.append(c)
        else:
            groups.append(current)
            current = [c]
    groups.append(current)

    def _is_sequential_index(col: int) -> bool:
        body_vals = [
            grid[r][col]["text"]
            for r in range(band_end + 1, n_rows)
        ]
        filled = [v for v in body_vals if v]
        if not filled or not all(_is_numeric(v) for v in filled):
            return False
        nums = []
        for v in filled:
            try:
                nums.append(int(v.replace(",", "")))
            except ValueError:
                return False
        unique = sorted(set(nums))
        if unique[0] != 1:
            return False
        return unique == list(range(1, len(unique) + 1))

    label_cols: set[int] = set()
    skip_cols: set[int] = set()
    for c in range(n_cols):
        band_text = grid[band_end][c]["text"] if 0 <= band_end < n_rows else ""
        if c == 0:
            if _is_sequential_index(c):
                skip_cols.add(c)
            elif not band_text or (not _is_yearlike(band_text) and not _is_numeric(band_text)):
                label_cols.add(0)
            continue
        body = [
            grid[r][c]["text"]
            for r in range(band_end + 1, n_rows)
            if grid[r][c]["text"]
        ]
        if not body:
            continue
        numeric = sum(1 for x in body if _is_numeric(x))
        if numeric > len(body) / 2:
            continue
        if not band_text and any(_looks_wordy(x) for x in body):
            label_cols.add(c)

    pairs: list[dict[str, Any]] = []
    seen_names: dict[str, int] = {}
    global_leaf_counts: dict[str, int] = {}

    for cols in groups:
        if all(c in skip_cols for c in cols):
            continue
        name = ""
        for r in range(band_end):
            for c in cols:
                t = grid[r][c]["text"]
                if t:
                    name = _strip_footnote_markers(t)
                    break
            if name:
                break
        if not name:
            for c in cols:
                if c in label_cols:
                    ht = grid[band_end][c]["text"]
                    if ht and _looks_wordy(ht):
                        name = _strip_footnote_markers(ht)
                        break

        leafs: list[str | None] = []
        for c in cols:
            raw_text = grid[band_end][c]["text"]
            if c in label_cols:
                leafs.append(None)
                continue
            if not raw_text:
                leafs.append(f"column {c + 1}")
                continue
            leaf = _strip_footnote_markers(raw_text)
            count = global_leaf_counts.get(leaf, 0) + 1
            global_leaf_counts[leaf] = count
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


# --- TABLE TO NESTED (REFACTORED WITH TREE) ---

def _table_to_nested(
    grid: list[list[dict[str, Any]]],
    v_runs: list[list[tuple[int, int, bool]]],
    pairs: list[dict[str, Any]],
) -> dict[str, Any] | list[Any] | None:
    """Build nested structure from the grid using a Tree Data Structure."""
    n_rows = len(grid)
    n_cols = len(grid[0]) if n_rows else 0
    if n_rows < 1 or n_cols < 2:
        return None

    band_end = _header_band(grid)

    # Two-column key-value tables
    if n_cols == 2:
        r0 = grid[0]
        has_header = (
            _looks_wordy(r0[0]["text"])
            and _looks_wordy(r0[1]["text"])
            and len(r0[0]["text"].split()) == 1
            and len(r0[1]["text"].split()) == 1
        )
        tree = TableNode(name="root")
        header_node = tree.add_child(r0[0]["text"]) if has_header else tree

        for r in range(1 if has_header else 0, n_rows):
            k = grid[r][0]["text"]
            v = grid[r][1]["text"]
            if k and v:
                parsed_val = _parse_number(v) if _is_numeric(v) else v
                header_node.add_child(name=k, value=parsed_val)

        out = tree.to_dict()
        if not out:
            return None
        seen_leaves = set(tree.get_leaves())
        return out if len(seen_leaves) >= 2 else None

    if band_end == -1:
        band_end = 0

    # Flat-table fast path
    all_label_cols: set[int] = set()
    for p in pairs:
        all_label_cols |= p["label_cols"]

    if not all_label_cols:
        records: list[dict[str, Any]] = []
        for r in range(band_end + 1, n_rows):
            if all(not cell["text"] for cell in grid[r]):
                continue
            record: dict[str, Any] = {}
            for pair in pairs:
                for i, c in enumerate(pair["cols"]):
                    leaf = pair["leafs"][i]
                    if leaf is None:
                        continue
                    text = _merge_fill(grid, v_runs, r, c)
                    if text:
                        record[leaf] = _parse_number(text) if _is_numeric(text) else text
            if record:
                records.append(record)
        if not records:
            return None
        if len(records) == 1:
            if len(set(records[0].values())) < 2:
                return None
        return records

    sections = _detect_sections(grid, v_runs, band_end)
    root_tree = TableNode(name="root")
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
            if pair["name"] and (len(pair["cols"]) > 1 or pair["label_cols"]):
                path.append(pair["name"])
            for i, c in enumerate(pair["cols"]):
                leaf = pair["leafs"][i]
                if leaf is None:
                    if c not in pair["label_cols"]:
                        continue
                    text = _merge_fill(grid, v_runs, r, c)
                    if not text:
                        continue
                    text = _FOOTNOTE_PREFIX.sub("", text, count=1).strip() or text
                    path.append(text)
                else:
                    text = grid[r][c]["text"]
                    if text:
                        values[leaf] = _parse_number(text) if _is_numeric(text) else text

        if not values:
            continue

        # Add path and values directly into the Tree structure
        root_tree.add_path(path, values)

    # Convert tree to dict structure
    out = root_tree.to_dict()
    if not isinstance(out, dict):
        return None

    # Validate non-degeneracy using tree metrics
    seen_values = set(root_tree.get_leaves())
    seen_keys = root_tree.get_all_keys()

    if len(seen_values) < 2 and len(seen_keys) < 2:
        return None

    return out


# --- PDF EXTRACTOR ---

class PDFExtractor:
    def __init__(
        self,
        pdf_path: str | Path,
        error_logger: Any = None,
        status_logger: Any = None,
        **kwargs: Any,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.error_logger = error_logger
        self.status_logger = status_logger
        self.global_kwargs: dict[str, Any] = kwargs

        if not self.pdf_path.exists():
            if self.error_logger:
                self.error_logger.error(f"PDF not found: {self.pdf_path}")
            raise FileNotFoundError(f"PDF not found: {self.pdf_path}")

        try:
            self.doc = pymupdf.open(self.pdf_path)
        except Exception as exc:
            raise ValueError(f"Invalid or corrupted PDF: {self.pdf_path}") from exc

        if self.doc.needs_pass:
            password = str(self.global_kwargs.get("password", ""))
            if not self.doc.authenticate(password):
                self.doc.close()
                raise PermissionError(f"PDF authentication failed: {self.pdf_path}")

        if self.status_logger:
            self.status_logger.info(f"PDF_OPEN_SUCCESS | {self.pdf_path}")

    def _page_range(self) -> range:
        spec = self.global_kwargs.get("pages", "all")
        if spec == "all":
            return range(self.doc.page_count)
        if isinstance(spec, int):
            return range(spec - 1, spec)
        indices: list[int] = []
        for part in str(spec).split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                indices.extend(range(int(a) - 1, int(b)))
            else:
                indices.append(int(part) - 1)
        return range(min(indices), max(indices) + 1) if indices else range(self.doc.page_count)

    def extract_metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = dict(self.doc.metadata or {})
        meta["page_count"] = self.doc.page_count
        meta["page_sizes"] = [
            {
                "index": i + 1,
                "width": round(p.rect.width, 2),
                "height": round(p.rect.height, 2),
            }
            for i, p in enumerate(self.doc)
        ]
        return meta

    def extract_text(self, *args: Any, **kwargs: Any) -> str:
        pages = args if args else range(self.doc.page_count)
        chunks = [self.doc[p].get_text(**kwargs) for p in pages]
        return "\f".join(chunks)

    def extract_text_by_page(self, **kwargs: Any) -> list[str]:
        return [page.get_text(**kwargs) for page in self.doc]

    def extract_tables(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        for page_idx in self._page_range():
            page = self.doc[page_idx]
            try:
                finder = page.find_tables()
            except Exception as e:
                if self.error_logger:
                    self.error_logger.warning(f"find_tables failed on page {page_idx + 1}: {e}")
                continue

            for t in finder.tables:
                try:
                    raw: list[list[str | None]] = t.extract()
                except Exception as e:
                    if self.error_logger:
                        self.error_logger.warning(f"table.extract() failed on page {page_idx + 1}: {e}")
                    continue

                if not raw or len(raw) < 2 or not raw[0] or len(raw[0]) < 2:
                    continue

                grid = _build_cell_grid_from_pymupdf(raw)
                v_runs = _vertical_merge_runs_from_grid(grid)

                band_end = _header_band(grid)
                if band_end == -1:
                    band_end = 0
                pairs = _column_pairs_from_grid(grid, band_end, raw)

                nested: dict[str, Any] | None = None
                try:
                    nested = _table_to_nested(grid, v_runs, pairs)
                except Exception as e:
                    if self.error_logger:
                        self.error_logger.warning(f"Nested build failed on page {page_idx + 1}: {e}")

                df = pd.DataFrame(
                    [
                        [("" if cell is None else cell) for cell in row]
                        for row in raw
                    ]
                )

                results.append({
                    "page": page_idx + 1,
                    "shape": [len(raw), len(raw[0])],
                    "df": df,
                    "nested": nested,
                })

        return results

    def extract_tables_as_dataframes(self) -> list[pd.DataFrame]:
        return [item["df"] for item in self.extract_tables()]

    def extract_all(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("pages", "all")
        drop_tables = kwargs.pop("drop_tables", False)
        self.global_kwargs.setdefault("pages", kwargs["pages"])

        result: dict[str, Any] = {
            "metadata": self.extract_metadata(),
            "text": self.extract_text(),
        }
        if not drop_tables:
            tables = self.extract_tables()
            result["tables"] = [
                {
                    "page": item["page"],
                    "shape": item["shape"],
                    "nested": item["nested"],
                }
                for item in tables
                if item["nested"] is not None
            ]
        return result

    def save_tables_to_json(
        self,
        tables: list[dict[str, Any]] | None = None,
        output_dir: str | Path = OUTPUT_DIR,
    ) -> list[Path]:
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