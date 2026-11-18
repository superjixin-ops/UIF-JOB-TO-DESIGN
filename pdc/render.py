"""IR -> xlsx / markdown。渲染层没有模型，纯 IO。"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from pdc.ir import DesignDoc

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
BODY_FONT = Font(size=9)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(vertical="top", wrap_text=True)
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)

SEVERITY_FILL = {
    "high": PatternFill("solid", fgColor="F8D7DA"),
    "check": PatternFill("solid", fgColor="FFF3CD"),
    "info": PatternFill("solid", fgColor="E2E3E5"),
}


def _clean(value: str) -> str:
    # openpyxl 会把以 = 开头的字符串当公式
    return " " + value if value.startswith("=") else value


def render_xlsx(doc: DesignDoc, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    wb = Workbook()
    wb.remove(wb.active)

    for sheet in doc.sheets:
        ws = wb.create_sheet(sheet.name[:31])
        ws.sheet_view.showGridLines = False

        for c, h in enumerate(sheet.headers, 1):
            cell = ws.cell(row=1, column=c, value=h)
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = BORDER
        ws.row_dimensions[1].height = 28

        for r, row in enumerate(sheet.rows, 2):
            for c, value in enumerate(row, 1):
                cell = ws.cell(row=r, column=c, value=_clean(str(value)))
                cell.font = BODY_FONT
                cell.alignment = CENTER if str(value).isdigit() else WRAP
                cell.border = BORDER
            if sheet.name == "Review Notes" and len(row) > 1:
                fill = SEVERITY_FILL.get(str(row[1]))
                if fill:
                    ws.cell(row=r, column=2).fill = fill

        for i, w in enumerate(sheet.widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if sheet.rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(max(len(sheet.headers), 1))}{len(sheet.rows) + 1}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def render_review_md(doc: DesignDoc, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    order = {"high": 0, "check": 1, "info": 2}
    findings = sorted(doc.findings, key=lambda f: order.get(f.severity, 9))

    lines = [
        f"# 设计评审：{doc.pipeline}",
        "",
        f"- 源文件：`{doc.source_path}`",
        f"- 产出表：{len(doc.sheets)} 张",
        (
            f"- 发现：{len(findings)} 条"
            f"（high {sum(1 for f in findings if f.severity == 'high')} / "
            f"check {sum(1 for f in findings if f.severity == 'check')} / "
            f"info {sum(1 for f in findings if f.severity == 'info')}）"
        ),
        "",
        "## 发现",
        "",
    ]
    if not findings:
        lines.append("未发现问题。")
    for i, f in enumerate(findings, 1):
        lines += [
            f"### {i}. [{f.severity.upper()}] {f.title}",
            "",
            f"- **code**：`{f.code}`",
            f"- **详情**：{f.detail}",
        ]
        if f.rule_id:
            lines.append(f"- **依据**：`{f.rule_id}`")
        if f.evidence:
            lines.append(f"- **证据**：`{f.evidence}`")
        lines.append("")

    lines += ["## 工作表", ""]
    for s in doc.sheets:
        lines.append(f"- `{s.name}`：{len(s.rows)} 行 × {len(s.headers)} 列")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
