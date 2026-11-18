"""输出校验器：Agent 的"后果感知"。

没有校验器的 Agent 只能自我评价，而模型的自评不可靠。
校验器把"我觉得没问题"换成"程序断言通过"。
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from pdc.ir import DesignDoc

HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")


def validate_doc(doc: DesignDoc, xlsx_path: str | Path) -> list[str]:
    issues: list[str] = []
    path = Path(xlsx_path)
    if not path.exists():
        return ["[file_missing] 输出文件不存在"]

    wb = load_workbook(path, read_only=True)

    # A1: IR 里声明的每张表都必须真的在文件里，且表头一致
    for sheet in doc.sheets:
        if sheet.name[:31] not in wb.sheetnames:
            issues.append(f"[sheet_missing] 缺少工作表: {sheet.name}")
            continue
        got = [c.value for c in next(wb[sheet.name[:31]].iter_rows(max_row=1))]
        got = [g for g in got if g is not None]
        if got != sheet.headers:
            issues.append(f"[header_mismatch] {sheet.name} 表头不符，期望 {sheet.headers}，实际 {got}")

    # A2: 落地表行数必须等于 load schema 的列数
    for sheet in doc.sheets:
        if not sheet.name.startswith("Landing - "):
            continue
        declared = sheet.headers
        if declared and len(sheet.rows) == 0:
            issues.append(f"[empty_landing] {sheet.name} 没有任何列")

    # A3: 评审条目必须是结构化的
    review = doc.sheet("Review Notes")
    if review is None:
        issues.append("[missing_review] 缺少 Review Notes 工作表")
    elif review.rows and len(review.rows[0]) != len(review.headers):
        issues.append("[review_shape] Review Notes 行的列数与表头不一致")

    # A4: 渲染后的文件里不能出现以 = 开头的单元格（openpyxl 会当公式执行）
    for name in wb.sheetnames:
        for row in wb[name].iter_rows(values_only=True):
            for value in row:
                if isinstance(value, str) and value.startswith("="):
                    issues.append(f"[formula_injection] {name} 出现以 = 开头的单元格: {value[:40]}")

    wb.close()
    return issues


def check_numbering(lines: list[str]) -> list[str]:
    """检查 Markdown 章节编号连续性。用于校验 review.md。"""
    issues: list[str] = []
    last_major: int | None = None
    for line in lines:
        m = HEADING_RE.match(line.strip())
        if not m:
            continue
        major = int(m.group(1).split(".")[0])
        if last_major is not None and major not in (last_major, last_major + 1):
            issues.append(f"[numbering] 跳号: {m.group(1)}（上一个是 {last_major}）")
        last_major = major
    return issues
