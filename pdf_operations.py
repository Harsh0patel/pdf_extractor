from __future__ import annotations

import json
import re
from collections import defaultdict
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
_WORDY_RE = re.compile(r"^(?=.*[A-Za-z].*[A-Za-z])^\d*$")
_YEAR_RE = re.compile(r"^\d{4}(?:\s*/\s*\d{2,4})?$")
_NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$|^\(\d[\d,]*(?:\.\d+)?\)$")
_YEAR_SUFFIX_FN = re.compile(r"^(\d{4}(?:/\d{2,4})?)\s+\d{1,2}$")
_MAX_HEADER_ROWS = 4

OUTPUT_DIR = Path("output")

class TableTree:
    """Lightweight recursive tree using defaultdict to build hierarchical structures."""

    def __init__(self) -> None:
        self.children: dict[str, TableTree] = defaultdict(TableTree)
        self.values: dict[str, Any] = {}
        self.records: list[dict[str, Any]] = []

    def insert(self, path: list[str], values: dict[str, Any]) -> None:
        curr = self
        for key in filter(None, path):
            curr = curr.children[key]

        has_scalars = any(not isinstance(v, dict) for v in curr.values.values())
        if curr.records:
            curr.records.append(values)
        elif has_scalars and values:
            curr.records = [dict(curr.values), values]
            curr.values.clear()
        else:
            for k, v in values.items():
                if k not in curr.values or isinstance(curr.values[k], dict):
                    curr.values[k] = v
                else:
                    curr.values[f"{k} [2]"] = v

    def to_dict(self) -> dict[str, Any] | list[dict[str, Any]] | None:
        if self.records:
            return self.records
        res = {k: v.to_dict() for k, v in self.children.items()}
        res.update(self.values)
        return res or None

def _clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u00a0", " ").replace("\r", "\n")
    return _CLEAN_SPACES.sub(" ", _CLEAN_NEWLINES.sub(" ", text)).strip()


def _strip_footnote_markers(text: str) -> str:
    if not text or _FOOTNOTE_ONLY.match(text):
        return text
    m = _YEAR_SUFFIX_FN.match(text)
    return m.group(1) if m else _FOOTNOTE_SUFFIX.sub("", _FOOTNOTE_PREFIX.sub("", text, count=1), count=1).strip()


def _looks_wordy(text: str) -> bool:
    if not text or len(text) > _MAX_KEY_LENGTH or not _WORDY_RE.match(text):
        return False
    return not all(t.isdigit() for t in re.split(r"[\s/]+", text) if t)


def _is_yearlike(text: str) -> bool: return bool(_YEAR_RE.match(text))


def _is_numeric(text: str) -> bool: return bool(_NUM_RE.match(text)) and any(ch.isdigit() for ch in text)


def _cell_type(text: str) -> str:
    if not text: return "empty"
    if _is_yearlike(text): return "year"
    if _is_numeric(text): return "num"
    return "word" if _looks_wordy(text) else "other"


def _parse_number(text: str) -> int | float | str:
    t = text.replace(",", "")
    if m := re.fullmatch(r"\((\d+(?:\.\d+)?)\)", t):
        val = float(m.group(1))
        return -int(val) if val.is_integer() else -val
    if re.fullmatch(r"-?\d+", t): return int(t)
    if re.fullmatch(r"-?\d*\.\d+", t): return float(t)
    return text

def _build_cell_grid_from_pymupdf(raw: list[list[str | None]]) -> list[list[dict[str, Any]]]:
    return [[{"text": _clean_cell(cell), "hspan": False, "vspan": cell is None} for cell in row] for row in raw]


def _vertical_merge_runs_from_grid(grid: list[list[dict[str, Any]]]) -> list[list[tuple[int, int, bool]]]:
    if not grid: return []
    runs = []
    for c in range(len(grid[0])):
        segments, start, in_merge = [], 0, False
        for r in range(len(grid)):
            if r == 0: continue
            cell, prev = grid[r][c], grid[r - 1][c]
            if cell["vspan"] and not prev["vspan"]:
                segments.append((start, r - 1, False))
                start, in_merge = r - 1, True
            elif not cell["vspan"] and in_merge:
                segments.append((start, r - 1, True))
                start, in_merge = r, False
        segments.append((start, len(grid) - 1, in_merge))
        runs.append(segments)
    return runs


def _merge_fill(grid: list[list[dict[str, Any]]], v_runs: list[list[tuple[int, int, bool]]], r: int, c: int) -> str:
    if text := grid[r][c]["text"]: return text
    if c < len(v_runs):
        for a, b, is_merge in v_runs[c]:
            if is_merge and a <= r <= b: return grid[a][c]["text"]
    return ""


def _is_merge_covered(v_runs: list[list[tuple[int, int, bool]]], r: int, c: int) -> bool:
    return c < len(v_runs) and any(is_merge and a <= r <= b for a, b, is_merge in v_runs[c])


# structure analysis
def _header_band(grid: list[list[dict[str, Any]]], raw: list[list[str | None]] | None = None) -> int:
    if not grid: return -1
    end = -1
    for r in range(min(_MAX_HEADER_ROWS, len(grid))):
        row = grid[r]
        if all(not cell["text"] for cell in row): break
        first, rest = row[0]["text"], [cell["text"] for cell in row[1:]]
        filled = [x for x in rest if x]
        if r == 0:
            first_empty_header = not first and bool(filled) and any(_is_yearlike(x) for x in filled)
            if not _looks_wordy(first) and not first_empty_header: break
            has_years = any(_is_yearlike(x) for x in filled)
            non_year = [x for x in filled if not _is_yearlike(x)]
            body_row = grid[1] if len(grid) > 1 else None
            type_mismatch = body_row and non_year and not first_empty_header and any(
                _cell_type(row[c + 1]["text"]) not in ("empty", _cell_type(body_row[c + 1]["text"]))
                and _cell_type(body_row[c + 1]["text"]) != "empty" for c in range(len(grid[0]) - 1)
            )
            all_same = filled and all(x == filled[0] for x in filled) and _looks_wordy(filled[0])
            if not (has_years or type_mismatch or not non_year or all_same or first_empty_header): break
        else:
            non_year = [x for x in filled if not _is_yearlike(x)]
            prev_raw = raw[r - 1] if raw and 0 < r < len(raw) else None
            prev_spanning = prev_raw and any(isinstance(v, str) and _is_yearlike(v) for v in prev_raw if v) and any(
                v is None for v in prev_raw[1:])
            all_wordy = filled and all(_looks_wordy(x) for x in filled) and not any(_is_numeric(x) for x in filled)
            if len(non_year) > 1 and not (prev_spanning and all_wordy): break
        end = r
        if r + 1 < len(grid) and all(not c2["text"] for c2 in grid[r + 1][1:]): break
    return end


def _column_pairs_from_grid(grid: list[list[dict[str, Any]]], band_end: int, raw: list[list[str | None]]) -> list[
    dict[str, Any]]:
    if band_end < 0 or not grid: return []
    n_cols, n_rows = len(grid[0]), len(grid)
    band_raw = raw[band_end] if band_end < len(raw) else [None] * n_cols

    spanning_raw = next((r_raw for r in range(band_end) if
                         (r_raw := raw[r] if r < len(raw) else []) and any(v is None for v in r_raw[1:]) and any(
                             isinstance(v, str) and (_is_yearlike(v) or _looks_wordy(v)) for v in r_raw if v)), None)
    group_raw = spanning_raw or band_raw

    groups, current = [], [0]
    for c in range(1, n_cols):
        if group_raw[c] is None:
            current.append(c)
        else:
            groups.append(current); current = [c]
    groups.append(current)

    def _is_sequential_index(col: int) -> bool:
        filled = [v for r in range(band_end + 1, n_rows) if (v := grid[r][col]["text"])]
        if not filled or not all(_is_numeric(v) for v in filled): return False
        try:
            nums = [int(v.replace(",", "")) for v in filled]
        except ValueError:
            return False
        unique = sorted(set(nums))
        return unique[0] == 1 and unique == list(range(1, len(unique) + 1))

    label_cols = {0} if not _is_sequential_index(0) and (
                not (b_txt := grid[band_end][0]["text"] if band_end < n_rows else "") or (
                    not _is_yearlike(b_txt) and not _is_numeric(b_txt))) else set()
    skip_cols = {0} if _is_sequential_index(0) else set()

    for c in range(1, n_cols):
        b_txt = grid[band_end][c]["text"] if band_end < n_rows else ""
        body = [grid[r][c]["text"] for r in range(band_end + 1, n_rows) if grid[r][c]["text"]]
        if body and sum(1 for x in body if _is_numeric(x)) <= len(body) / 2 and not b_txt and any(
                _looks_wordy(x) for x in body):
            label_cols.add(c)

    pairs, seen_names, global_leafs = [], {}, {}
    for cols in groups:
        if all(c in skip_cols for c in cols): continue
        name = next(
            (_strip_footnote_markers(grid[r][c]["text"]) for r in range(band_end) for c in cols if grid[r][c]["text"]),
            "")
        if not name:
            name = next((_strip_footnote_markers(grid[band_end][c]["text"]) for c in cols if
                         c in label_cols and _looks_wordy(grid[band_end][c]["text"])), "")

        leafs = []
        for c in cols:
            raw_text = grid[band_end][c]["text"]
            if c in label_cols:
                leafs.append(None)
            elif not raw_text:
                anchor = next(
                    (_strip_footnote_markers(grid[band_end][pc]["text"]) for pc in reversed(cols[:cols.index(c)]) if
                     band_end < len(raw) and raw[band_end][pc] and grid[band_end][pc]["text"]), None)
                leafs.append(f"__hspan__{anchor}" if anchor else f"column {c + 1}")
            else:
                leaf = _strip_footnote_markers(raw_text)
                cnt = global_leafs.get(leaf, 0) + 1
                global_leafs[leaf] = cnt
                leafs.append(f"{leaf} [{cnt}]" if cnt > 1 else leaf)

        if name:
            seen_names[name] = seen_names.get(name, 0) + 1
            if seen_names[name] > 1: name = f"{name} [{seen_names[name]}]"

        pairs.append({"cols": cols, "name": name, "leafs": leafs, "label_cols": label_cols & set(cols)})

    return pairs


def _detect_sections(grid: list[list[dict[str, Any]]], v_runs: list[list[tuple[int, int, bool]]], band_end: int) -> dict[int, str]:
    sections = {}
    if not grid: return sections
    for r in range(max(band_end + 1, 1), len(grid)):
        row = grid[r]
        filled = [c for c in range(len(grid[0])) if row[c]["text"]]
        if 1 <= len(filled) <= 2 and not any(_is_merge_covered(v_runs, r, c) for c in filled):
            lbl, *others = [row[c]["text"] for c in filled]
            if (_looks_wordy(lbl) and all(_is_yearlike(o) for o in others)) or (
                    _is_yearlike(lbl) and all(_looks_wordy(o) for o in others)):
                sections[r] = lbl
    return sections


def _count_distinct(node: Any, seen: set[str]) -> None:
    if isinstance(node, dict):
        for v in node.values(): _count_distinct(v, seen)
    elif isinstance(node, list):
        for item in node: _count_distinct(item, seen)
    else:
        seen.add(str(node))


def _table_to_nested(
        grid: list[list[dict[str, Any]]],
        v_runs: list[list[tuple[int, int, bool]]],
        pairs: list[dict[str, Any]],
        raw: list[list[str | None]] | None = None,
) -> dict[str, Any] | list[Any] | None:
    n_rows, n_cols = len(grid), len(grid[0]) if grid else 0
    if n_rows < 1 or n_cols < 2: return None

    band_end = max(_header_band(grid, raw), 0)

    if n_cols == 2:
        r0 = grid[0]
        has_header = _looks_wordy(r0[0]["text"]) and _looks_wordy(r0[1]["text"]) and len(
            r0[0]["text"].split()) == 1 and len(r0[1]["text"].split()) == 1
        out = {grid[r][0]["text"]: _parse_number(v) if _is_numeric(v := grid[r][1]["text"]) else v for r in
               range(1 if has_header else 0, n_rows) if grid[r][0]["text"] and grid[r][1]["text"]}
        if not out: return None
        if has_header: return {r0[0]["text"]: out}
        seen: set[str] = set()
        _count_distinct(out, seen)
        return out if len(seen) >= 2 else None

    if not any(p["label_cols"] for p in pairs):
        records = []
        for r in range(band_end + 1, n_rows):
            if all(not cell["text"] for cell in grid[r]): continue
            rec = {}
            for pair in pairs:
                for i, c in enumerate(pair["cols"]):
                    if not (leaf := pair["leafs"][i]):
                        continue
                    if leaf.startswith("__hspan__"):
                        real_leaf = leaf[len("__hspan__"):]
                        if (text := grid[r][c]["text"]) and real_leaf not in rec:
                            rec[real_leaf] = _parse_number(text) if _is_numeric(text) else text
                    elif text := _merge_fill(grid, v_runs, r, c):
                        rec[leaf] = _parse_number(text) if _is_numeric(text) else text
            if rec: records.append(rec)
        if not records or (
                len(records) == 1 and (seen_r := set(), _count_distinct(records[0], seen_r), len(seen_r))[2] < 2):
            return None
        return records

    sections, tree, current_section = _detect_sections(grid, v_runs, band_end), TableTree(), None

    for r in range(band_end + 1, n_rows):
        if r in sections: current_section = sections[r]; continue
        if all(not cell["text"] for cell in grid[r]): continue

        path, values = [current_section] if current_section else [], {}
        for pair in pairs:
            is_year = pair["name"] and _is_yearlike(pair["name"]) and not pair["label_cols"]
            if pair["name"] and not is_year and (len(pair["cols"]) > 1 or pair["label_cols"]):
                path.append(pair["name"])
            for i, c in enumerate(pair["cols"]):
                leaf = pair["leafs"][i]
                if leaf is None:
                    if c in pair["label_cols"] and (text := _merge_fill(grid, v_runs, r, c)):
                        path.append(_FOOTNOTE_PREFIX.sub("", text, count=1).strip() or text)
                else:
                    text = grid[r][c]["text"]
                    if leaf.startswith("__hspan__"):
                        real_leaf = leaf[len("__hspan__"):]
                        if text:
                            if is_year:
                                values.setdefault(pair["name"], {})[real_leaf] = _parse_number(text) if _is_numeric(
                                    text) else text
                            else:
                                values.setdefault(real_leaf, _parse_number(text) if _is_numeric(text) else text)
                    elif text:
                        parsed = _parse_number(text) if _is_numeric(text) else text
                        if is_year:
                            values.setdefault(pair["name"], {})[leaf] = parsed
                        else:
                            values[leaf] = parsed

        if values: tree.insert(path, values)

    out = tree.to_dict()
    if not out: return None

    seen_val, seen_key = set(), set()
    _count_distinct(out, seen_val)

    def _keys(n: Any) -> None:
        if isinstance(n, dict):
            for k, v in n.items(): seen_key.add(k); _keys(v)

    _keys(out)

    return out if len(seen_val) >= 2 or len(seen_key) >= 2 else None


# main extractor

class PDFExtractor:
    """Extract metadata, text and tables from a PDF using PyMuPDF throughout."""

    def __init__(self, pdf_path: str | Path, error_logger: Any = None, status_logger: Any = None,
                 **kwargs: Any) -> None:
        self.pdf_path, self.error_logger, self.status_logger, self.global_kwargs = Path(
            pdf_path), error_logger, status_logger, kwargs
        if not self.pdf_path.exists(): raise FileNotFoundError(f"PDF not found: {self.pdf_path}")
        try:
            self.doc = pymupdf.open(self.pdf_path)
        except Exception as exc:
            raise ValueError(f"Invalid PDF: {self.pdf_path}") from exc
        if self.doc.needs_pass and not self.doc.authenticate(str(self.global_kwargs.get("password", ""))):
            self.doc.close()
            raise PermissionError(f"PDF authentication failed: {self.pdf_path}")

    def _page_range(self) -> range:
        spec = self.global_kwargs.get("pages", "all")
        if spec == "all": return range(self.doc.page_count)
        if isinstance(spec, int): return range(spec - 1, spec)
        indices = []
        for part in str(spec).split(","):
            indices.extend(range(int(a) - 1, int(b)) if "-" in part and (a := part.split("-")[0]) and (
                b := part.split("-")[1]) else [int(part.strip()) - 1])
        return range(min(indices), max(indices) + 1) if indices else range(self.doc.page_count)

    def extract_metadata(self) -> dict[str, Any]:
        meta = dict(self.doc.metadata or {})
        meta.update({"page_count": self.doc.page_count,
                     "page_sizes": [{"index": i + 1, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2)}
                                    for i, p in enumerate(self.doc)]})
        return meta

    def extract_text(self, *args: Any, **kwargs: Any) -> str:
        return "\f".join(self.doc[p].get_text(**kwargs) for p in (args or range(self.doc.page_count)))

    def extract_tables(self) -> list[dict[str, Any]]:
        results = []
        for page_idx in self._page_range():
            page = self.doc[page_idx]
            try:
                finder = page.find_tables()
            except Exception:
                continue
            for t in finder.tables:
                try:
                    raw = t.extract()
                except Exception:
                    continue
                if not raw or len(raw) < 2 or len(raw[0]) < 2: continue
                grid = _build_cell_grid_from_pymupdf(raw)
                v_runs = _vertical_merge_runs_from_grid(grid)
                band_end = max(_header_band(grid, raw), 0)
                pairs = _column_pairs_from_grid(grid, band_end, raw)
                nested = _table_to_nested(grid, v_runs, pairs, raw)
                results.append({"page": page_idx + 1, "shape": [len(raw), len(raw[0])],
                                "df": pd.DataFrame([[("" if c is None else c) for c in r] for r in raw]),
                                "nested": nested})
        return results

    def extract_all(self, **kwargs: Any) -> dict[str, Any]:
        return {"metadata": self.extract_metadata(), "text": self.extract_text(),
                "tables": [{"page": t["page"], "shape": t["shape"], "nested": t["nested"]} for t in
                           self.extract_tables() if t["nested"] is not None]}

    def save_tables_to_json(self, tables: list[dict[str, Any]] | None = None, output_dir: str | Path = OUTPUT_DIR) -> \
    list[Path]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        paths = []
        for idx, item in enumerate(tables or self.extract_tables(), 1):
            if (nested := item.get("nested")) is None: continue
            file_path = out_dir / f"{self.pdf_path.stem}_table_{idx}_{stamp}.json"
            file_path.write_text(json.dumps(nested, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            paths.append(file_path)
        return paths

    def __enter__(self) -> PDFExtractor:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.doc.close()