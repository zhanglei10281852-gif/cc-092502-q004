"""植物考古 CSV 行解析。

单个 CSV 文件通过 record_type 列承载四类记录：batch / sample / fraction /
identification，字段名兼容中英文常见写法。每行独立解析；无法解释的行由调用方
写入拒绝清单，不影响其他行。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

RECORD_TYPES = {"batch", "sample", "fraction", "identification"}

CONTEXT_TYPES = {"paleochannel", "dwelling", "pit", "other"}
FRACTIONS = {"heavy", "light"}
CONFIDENCES = {"high", "medium", "low", "uncertain"}

COLUMN_ALIASES = {
    "record_type": {"record_type", "type", "记录类型", "行类型", "sheet"},
    "batch_code": {"batch_code", "batch", "批次", "批次号", "浮选批次"},
    "sample_code": {"sample_code", "sample", "样品编号", "土样编号", "样品号"},
    "context_code": {"context_code", "context", "遗迹编号", "单位编号"},
    "context_type": {"context_type", "遗迹类型", "单位类型"},
    "context_name": {"context_name", "遗迹名称"},
    "volume_liters": {"volume_liters", "volume", "体积", "土样体积", "体积_l", "体积升"},
    "mesh_mm": {"mesh_mm", "sieve_mesh", "筛网", "筛网规格", "筛网_mm"},
    "sieve_notes": {"sieve_notes", "筛网备注"},
    "fraction": {"fraction", "组分", "浮选组分"},
    "fraction_notes": {"fraction_notes", "组分备注"},
    "taxon_code": {"taxon_code", "taxon", "分类编号", "分类编码", "种名代码"},
    "scientific_name": {"scientific_name", "学名", "名称"},
    "family": {"family", "科"},
    "is_unknown": {"is_unknown", "未知", "未知分类", "是否未知"},
    "unknown_detail": {"unknown_detail", "未知说明", "未知描述"},
    "confidence": {"confidence", "置信度", "鉴定置信度"},
    "count": {"count", "count_value", "计数", "数量", "粒数"},
    "detection_limit": {"detection_limit", "limit", "检测限"},
    "polluted": {"polluted", "root_contamination", "污染", "现代根系", "根系污染"},
    "pollution_note": {"pollution_note", "污染说明", "污染备注"},
    "notes": {"notes", "note", "备注"},
}

RECORD_TYPE_ALIASES = {
    "batch": "batch", "批次": "batch", "浮选批次": "batch",
    "sample": "sample", "土样": "sample", "样品": "sample",
    "fraction": "fraction", "组分": "fraction", "浮选组分": "fraction",
    "identification": "identification", "id": "identification",
    "鉴定": "identification", "分类鉴定": "identification",
}

CONTEXT_TYPE_ALIASES = {
    "paleochannel": "paleochannel", "古河道": "paleochannel", "河道": "paleochannel", "channel": "paleochannel",
    "dwelling": "dwelling", "居址": "dwelling", "房址": "dwelling", "居住面": "dwelling",
    "pit": "pit", "灰坑": "pit", "坑": "pit",
    "other": "other", "其他": "other",
}

FRACTION_ALIASES = {
    "heavy": "heavy", "重": "heavy", "重组分": "heavy", "heavyfraction": "heavy",
    "light": "light", "轻": "light", "轻组分": "light", "lightfraction": "light",
}

CONFIDENCE_ALIASES = {
    "high": "high", "高": "high", "确定": "high",
    "medium": "medium", "中": "medium",
    "low": "low", "低": "low",
    "uncertain": "uncertain", "存疑": "uncertain", "不确定": "uncertain", "疑": "uncertain",
}

TRUE_TOKENS = {"1", "true", "yes", "y", "是", "真", "有", "污染"}
FALSE_TOKENS = {"0", "false", "no", "n", "否", "无", "未污染", ""}


class RowError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class RawRow:
    line: int
    kind: str
    fields: dict[str, str]
    raw_line: str


@dataclass
class ParsedSheet:
    rows: list[RawRow] = field(default_factory=list)
    header: list[str] = field(default_factory=list)


def _normalize_header(name: str) -> str:
    return name.strip().lstrip("﻿").replace(" ", "_").replace("（", "(").replace("）", ")")


def _header_index(header: list[str]) -> dict[str, int]:
    """把原始表头解析成 规范字段 -> 列序号，无法识别的列忽略。"""
    index: dict[str, int] = {}
    for pos, raw in enumerate(header):
        name = _normalize_header(raw).casefold()
        for canonical, aliases in COLUMN_ALIASES.items():
            if name in {a.casefold() for a in aliases}:
                index.setdefault(canonical, pos)
                break
    return index


def parse_sheet(content: bytes) -> ParsedSheet:
    """解析 CSV 字节；表头缺 record_type 列或文件完全为空时抛 RowError('bad_sheet')。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RowError("bad_encoding", "文件不是 UTF-8 编码，请另存为 UTF-8 后重试") from exc
    reader = csv.reader(io.StringIO(text))
    rows_raw = list(reader)
    while rows_raw and not any(cell.strip() for cell in rows_raw[0]):
        rows_raw.pop(0)
    if not rows_raw:
        raise RowError("empty_file", "CSV 文件为空")
    header = rows_raw[0]
    index = _header_index(header)
    if "record_type" not in index:
        raise RowError("bad_header", "CSV 表头缺少 record_type（记录类型）列")
    sheet = ParsedSheet(header=header)
    for physical, raw_cells in enumerate(rows_raw[1:], start=2):
        if not any(cell.strip() for cell in raw_cells):
            continue
        fields: dict[str, str] = {}
        for canonical, pos in index.items():
            fields[canonical] = raw_cells[pos].strip() if pos < len(raw_cells) else ""
        kind_raw = fields.get("record_type", "").strip().casefold()
        kind = RECORD_TYPE_ALIASES.get(kind_raw)
        if kind is None:
            # 保留原始类型串，由逐行校验拒绝
            kind = kind_raw or "unknown"
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(raw_cells)
        sheet.rows.append(RawRow(line=physical, kind=kind, fields=fields, raw_line=buffer.getvalue()))
    return sheet


def require(value: str, label: str, code: str = "missing_field") -> str:
    if not value.strip():
        raise RowError(code, f"缺少必填字段：{label}")
    return value.strip()


def parse_float(value: str, label: str) -> float:
    try:
        return float(value.strip())
    except ValueError as exc:
        raise RowError("bad_number", f"{label} 不是合法数字：{value!r}") from exc


def parse_positive_float(value: str, label: str) -> float:
    number = parse_float(value, label)
    if number <= 0:
        raise RowError("not_positive", f"{label} 必须大于 0，当前为 {number}")
    return number


def parse_bool(value: str, label: str) -> int:
    token = value.strip().casefold()
    if token in TRUE_TOKENS:
        return 1
    if token in FALSE_TOKENS:
        return 0
    raise RowError("bad_bool", f"{label} 无法识别为是/否：{value!r}")


def parse_count(value: str) -> tuple[str, float, float | None]:
    """解析计数文本，返回 (censor, count_value, detection_limit)。

    - 普通非负数字   → exact
    - "<n"           → below（仅知上限，至多 n-1 粒），n 必须 >=1
    - nd / ND / 未检出 → nd（低于检测限），检测限在 detection_limit 列单独给出
    """
    token = value.strip()
    low = token.casefold()
    if low in {"nd", "n.d.", "none detected", "未检出", "低于检测限"}:
        return "nd", 0.0, None
    if token.startswith("<"):
        tail = token[1:].strip()
        bound = parse_float(tail, "计数上限")
        if bound < 1:
            raise RowError("bad_censor", f"仅知上限计数 '<n' 要求 n>=1，收到 {token!r}")
        return "below", float(bound), None
    number = parse_float(token, "计数")
    if number < 0:
        raise RowError("negative_count", f"计数不能为负：{token}")
    return "exact", number, None


def build_rejection_csv(header: list[str], rows: list[tuple[int, str, str, str]]) -> str:
    """生成拒绝清单 CSV（UTF-8 BOM，便于 Excel 直接打开）。

    rows 元素：(行号, 错误代码, 错误说明, 原始行)
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["row_number", "error_code", "error_message", "raw_line"])
    for line, code, message, raw_line in rows:
        writer.writerow([line, code, message, raw_line])
    return "﻿" + buffer.getvalue()
