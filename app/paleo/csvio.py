"""植物遗存 CSV 解析与字段规范化。

不同浮选批次的记录习惯不一致：表头中英文混用、计数有精确值 /
仅知上限（<10）/ 仅检出 / 未检出、置信度写法多样、现代根系污染
标记不统一。本模块把这些变体收敛为规范值，无法解析的行抛出
RowError，由导入服务写入拒绝清单。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

IMPORT_KINDS = ("samples", "batches", "fractions", "identifications")


class RowError(Exception):
    """单行数据无法解析，进入拒绝清单。"""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class FileError(Exception):
    """整文件无法解析（如缺少必需表头），导入直接失败。"""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


HEADER_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "samples": {
        "sample_code": ("sample_code", "sample", "sample_id", "sample_no", "样品号", "土样号", "样品编号"),
        "context_type": ("context_type", "context", "unit_type", "遗迹类型", "遗存类型", "类型"),
        "context_label": ("context_label", "unit", "locus", "feature", "遗迹编号", "单位", "层位"),
        "collected_at": ("collected_at", "date", "sampled_at", "采集日期", "日期"),
        "notes": ("notes", "note", "remark", "备注"),
    },
    "batches": {
        "batch_code": ("batch_code", "batch", "batch_id", "flot_id", "批次号", "浮选批次", "批次编号"),
        "sample_code": ("sample_code", "sample", "sample_id", "sample_no", "样品号", "土样号", "样品编号"),
        "volume_liters": ("volume_liters", "volume", "volume_l", "liters", "soil_volume", "体积", "土样体积", "浮选体积", "体积升"),
        "floated_at": ("floated_at", "date", "浮选日期", "日期"),
        "operator": ("operator", "floated_by", "操作人", "浮选人"),
        "notes": ("notes", "note", "remark", "备注"),
    },
    "fractions": {
        "batch_code": ("batch_code", "batch", "batch_id", "flot_id", "批次号", "浮选批次", "批次编号"),
        "fraction_type": ("fraction_type", "fraction", "组分", "组分类型", "浮选组分"),
        "mesh_size_mm": ("mesh_size_mm", "mesh", "mesh_mm", "sieve", "筛网", "筛网规格", "筛网孔径", "孔径"),
        "notes": ("notes", "note", "remark", "备注"),
    },
    "identifications": {
        "batch_code": ("batch_code", "batch", "batch_id", "flot_id", "批次号", "浮选批次", "批次编号"),
        "fraction_type": ("fraction_type", "fraction", "组分", "组分类型", "浮选组分"),
        "mesh_size_mm": ("mesh_size_mm", "mesh", "mesh_mm", "sieve", "筛网", "筛网规格", "筛网孔径", "孔径"),
        "item_no": ("item_no", "item", "seq", "序号", "编号"),
        "taxon": ("taxon", "taxa", "name", "identification", "种属", "鉴定结果", "分类", "植物名称"),
        "family": ("family", "科"),
        "genus": ("genus", "属"),
        "species": ("species", "种"),
        "count": ("count", "n", "quantity", "数量", "粒数", "计数"),
        "count_type": ("count_type", "计数类型", "数量类型"),
        "confidence": ("confidence", "certainty", "置信度", "鉴定置信度", "可靠性"),
        "contamination": ("contamination", "contaminant", "污染", "污染标记", "现代根系"),
        "analyst": ("analyst", "identifier", "鉴定人", "分析人"),
        "notes": ("notes", "note", "remark", "备注"),
    },
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "samples": ("sample_code", "context_type"),
    "batches": ("batch_code", "sample_code", "volume_liters"),
    "fractions": ("batch_code", "fraction_type"),
    "identifications": ("batch_code", "fraction_type", "taxon"),
}

CONTEXT_TYPES = {
    "paleochannel": "paleochannel", "paleo_channel": "paleochannel", "channel": "paleochannel",
    "古河道": "paleochannel", "古河床": "paleochannel",
    "dwelling": "dwelling", "house": "dwelling", "residence": "dwelling",
    "居址": "dwelling", "房址": "dwelling", "居住址": "dwelling",
    "ash_pit": "ash_pit", "ashpit": "ash_pit", "ash pit": "ash_pit", "pit": "ash_pit",
    "灰坑": "ash_pit",
    "other": "other", "其他": "other",
}

FRACTION_TYPES = {
    "light": "light", "l": "light", "flot": "light", "轻浮": "light", "轻浮物": "light", "轻组分": "light",
    "heavy": "heavy", "h": "heavy", "重浮": "heavy", "重浮物": "heavy", "重组分": "heavy",
}

CONFIDENCE_LEVELS = {
    "high": "high", "h": "high", "高": "high", "certain": "high", "确定": "high", "confirmed": "high",
    "medium": "medium", "m": "medium", "中": "medium", "probable": "medium", "可能": "medium", "likely": "medium",
    "low": "low", "l": "low", "低": "low", "possible": "low", "疑似": "low", "cf": "low", "cf.": "low", "tentative": "low",
}

COUNT_TYPE_HINTS = {
    "exact": "exact", "精确": "exact", "准确": "exact",
    "upper": "upper", "upper_bound": "upper", "上限": "upper", "仅知上限": "upper",
    "present": "present", "检出": "present", "仅检出": "present",
    "absent": "absent", "未检出": "absent",
}

UNKNOWN_TAXA = {"unknown", "unidentified", "indet", "indet.", "未知", "未定", "未知分类", "未鉴定", "unidentified seed"}

CLEAN_MARKERS = {"", "no", "n", "none", "否", "无", "0", "clean", "未污染"}

MODERN_ROOT_MARKERS = {"modern_root", "modern root", "root", "roots", "现代根系", "现代根", "根系", "现代植物根"}

_PRESENT_MARKERS = {"present", "p", "+", "有", "检出", "presence"}
_ABSENT_MARKERS = {"absent", "a", "-", "未检出", "无检出", "none"}
_UPPER_RE = re.compile(r"^(?:<|<=|≤|少于|不超过)\s*([0-9]+(?:\.[0-9]+)?)$")
_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def normalize_taxon(text: str) -> str:
    return " ".join(text.strip().split()).casefold()


def mesh_key(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def _norm_header(text: str) -> str:
    return " ".join(text.strip().split()).casefold()


def map_headers(kind: str, header_row: list[str]) -> dict[int, str]:
    """把原始表头映射到规范列名，返回 {列下标: 规范列名}。"""
    aliases = HEADER_ALIASES[kind]
    lookup = {_norm_header(alias): canonical for canonical, names in aliases.items() for alias in names}
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for index, raw in enumerate(header_row):
        canonical = lookup.get(_norm_header(raw))
        if canonical and canonical not in used:
            mapping[index] = canonical
            used.add(canonical)
    missing = [name for name in REQUIRED_COLUMNS[kind] if name not in used]
    if missing:
        raise FileError("missing_columns", f"缺少必需列: {', '.join(missing)}")
    return mapping


def read_rows(content: bytes) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """解码 CSV 内容，返回 (原始表头, [(行号, 列值列表)])。行号为 1 起始的数据行号。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileError("invalid_encoding", "文件不是有效的 UTF-8 编码") from exc
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise FileError("empty_file", "文件为空")
    header, data = rows[0], rows[1:]
    return header, [(i + 1, row) for i, row in enumerate(data)]


def row_dict(header_map: dict[int, str], row: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for index, canonical in header_map.items():
        out[canonical] = row[index].strip() if index < len(row) else ""
    return out


def parse_context_type(raw: str) -> str:
    value = CONTEXT_TYPES.get(_norm_header(raw))
    if value is None:
        raise RowError("bad_context_type", f"无法识别的遗迹类型: {raw!r}")
    return value


def parse_fraction_type(raw: str) -> str:
    value = FRACTION_TYPES.get(_norm_header(raw))
    if value is None:
        raise RowError("bad_fraction_type", f"无法识别的浮选组分: {raw!r}")
    return value


def parse_float(raw: str, field_name: str, *, allow_empty: bool = False) -> float | None:
    if raw == "":
        if allow_empty:
            return None
        raise RowError("missing_number", f"{field_name} 不能为空")
    try:
        value = float(raw)
    except ValueError as exc:
        raise RowError("bad_number", f"{field_name} 不是数值: {raw!r}") from exc
    return value


def parse_volume(raw: str) -> float:
    value = parse_float(raw, "体积")
    assert value is not None
    if value < 0:
        raise RowError("negative_volume", f"体积不能为负: {raw!r}")
    return value


def parse_count(count_raw: str, hint_raw: str) -> tuple[str, float | None, float | None]:
    """解析计数，返回 (count_type, count_value, count_max)。

    支持精确计数、仅知上限（<10 / ≤10）、仅检出、未检出（检测限记录）。
    """
    hint = COUNT_TYPE_HINTS.get(_norm_header(hint_raw)) if hint_raw else None
    if hint_raw and hint is None:
        raise RowError("bad_count_type", f"无法识别的计数类型: {hint_raw!r}")
    text = count_raw.strip()
    key = _norm_header(text)
    if text == "":
        return (hint or "present"), None, None
    upper = _UPPER_RE.match(text)
    if upper:
        bound = float(upper.group(1))
        return "upper", None, bound
    if key in _PRESENT_MARKERS:
        return "present", None, None
    if key in _ABSENT_MARKERS:
        return "absent", 0.0, 0.0
    if _NUMBER_RE.match(text):
        value = float(text)
        if hint == "upper":
            return "upper", None, value
        if value == 0:
            return "absent", 0.0, 0.0
        return "exact", value, value
    raise RowError("bad_count", f"无法解析的计数: {count_raw!r}")


def parse_confidence(raw: str) -> tuple[str, str]:
    """返回 (原始值, 规范级别 high/medium/low 或 '')。"""
    if raw.strip() == "":
        return "", ""
    level = CONFIDENCE_LEVELS.get(_norm_header(raw))
    if level is None:
        raise RowError("bad_confidence", f"无法识别的置信度: {raw!r}")
    return raw.strip(), level


def parse_contamination(raw: str) -> str:
    """返回规范污染标记；未污染返回空串。现代根系污染统一为 modern_root。"""
    key = _norm_header(raw)
    if key in CLEAN_MARKERS:
        return ""
    if key in MODERN_ROOT_MARKERS:
        return "modern_root"
    return key


def parse_taxon(raw: str) -> tuple[str, str, int]:
    """返回 (原始名, 规范名, 是否未知分类)。"""
    if raw.strip() == "":
        raise RowError("missing_taxon", "分类名称不能为空")
    normalized = normalize_taxon(raw)
    if normalized in UNKNOWN_TAXA:
        return raw.strip(), "unidentified", 1
    return raw.strip(), normalized, 0


def parse_item_no(raw: str) -> int | None:
    if raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RowError("bad_item_no", f"序号不是整数: {raw!r}") from exc
    if value <= 0:
        raise RowError("bad_item_no", f"序号必须为正整数: {raw!r}")
    return value


@dataclass
class ParsedFile:
    kind: str
    header: list[str]
    records: list[dict[str, Any]] = field(default_factory=list)
    rejects: list[dict[str, Any]] = field(default_factory=list)

    def reject(self, row_number: int, raw: dict[str, str], error: RowError) -> None:
        self.rejects.append({
            "row_number": row_number,
            "raw": raw,
            "error_code": error.code,
            "error_message": error.message,
        })


def parse_file(kind: str, content: bytes) -> ParsedFile:
    """把 CSV 内容解析为规范记录与拒绝行，不做任何数据库写入。"""
    header, data_rows = read_rows(content)
    header_map = map_headers(kind, header)
    parsed = ParsedFile(kind=kind, header=header)
    parsers = {
        "samples": _build_sample,
        "batches": _build_batch,
        "fractions": _build_fraction,
        "identifications": _build_identification,
    }
    for row_number, row in data_rows:
        values = row_dict(header_map, row)
        try:
            record = parsers[kind](values)
        except RowError as error:
            parsed.reject(row_number, values, error)
            continue
        record["_row_number"] = row_number
        record["_raw"] = values
        parsed.records.append(record)
    return parsed


def _build_sample(values: dict[str, str]) -> dict[str, Any]:
    code = values["sample_code"]
    if not code:
        raise RowError("missing_sample_code", "样品号不能为空")
    return {
        "sample_code": code,
        "context_type": parse_context_type(values["context_type"]),
        "context_label": values.get("context_label", ""),
        "collected_at": values.get("collected_at", ""),
        "notes": values.get("notes", ""),
    }


def _build_batch(values: dict[str, str]) -> dict[str, Any]:
    if not values["batch_code"]:
        raise RowError("missing_batch_code", "批次号不能为空")
    if not values["sample_code"]:
        raise RowError("missing_sample_code", "样品号不能为空")
    return {
        "batch_code": values["batch_code"],
        "sample_code": values["sample_code"],
        "volume_liters": parse_volume(values["volume_liters"]),
        "floated_at": values.get("floated_at", ""),
        "operator": values.get("operator", ""),
        "notes": values.get("notes", ""),
    }


def _build_fraction(values: dict[str, str]) -> dict[str, Any]:
    if not values["batch_code"]:
        raise RowError("missing_batch_code", "批次号不能为空")
    mesh = parse_float(values.get("mesh_size_mm", ""), "筛网孔径", allow_empty=True)
    if mesh is not None and mesh <= 0:
        raise RowError("bad_mesh", f"筛网孔径必须为正: {values.get('mesh_size_mm')!r}")
    return {
        "batch_code": values["batch_code"],
        "fraction_type": parse_fraction_type(values["fraction_type"]),
        "mesh_size_mm": mesh,
        "notes": values.get("notes", ""),
    }


def _build_identification(values: dict[str, str]) -> dict[str, Any]:
    if not values["batch_code"]:
        raise RowError("missing_batch_code", "批次号不能为空")
    taxon_raw, taxon_normalized, is_unknown = parse_taxon(values["taxon"])
    count_type, count_value, count_max = parse_count(values.get("count", ""), values.get("count_type", ""))
    confidence_raw, confidence_level = parse_confidence(values.get("confidence", ""))
    mesh = parse_float(values.get("mesh_size_mm", ""), "筛网孔径", allow_empty=True)
    return {
        "batch_code": values["batch_code"],
        "fraction_type": parse_fraction_type(values["fraction_type"]),
        "mesh_size_mm": mesh,
        "item_no": parse_item_no(values.get("item_no", "")),
        "taxon_raw": taxon_raw,
        "taxon_normalized": taxon_normalized,
        "family": values.get("family", "").strip(),
        "genus": values.get("genus", "").strip(),
        "species": values.get("species", "").strip(),
        "count_type": count_type,
        "count_value": count_value,
        "count_max": count_max,
        "is_unknown": is_unknown,
        "contamination": parse_contamination(values.get("contamination", "")),
        "confidence_raw": confidence_raw,
        "confidence_level": confidence_level,
        "analyst": values.get("analyst", ""),
        "notes": values.get("notes", ""),
    }
