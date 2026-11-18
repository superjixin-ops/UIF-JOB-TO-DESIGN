"""设计文档的中间表示（IR）。

关键设计：Agent 不直接写 xlsx。
Agent 产出 IR（纯数据），渲染器负责 IR -> xlsx。
这样：
  - 渲染可单测（不用模型）
  - IR 可 diff、可存档、可人工修正后重渲染
  - 模型出错最多错在"内容"，不会错在"文件格式"
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Sheet:
    name: str
    headers: list[str]
    rows: list[list[str]] = field(default_factory=list)
    widths: list[int] = field(default_factory=list)

    def add(self, *cells: object) -> None:
        self.rows.append(["" if c is None else str(c) for c in cells])


@dataclass
class Finding:
    """一条评审发现。severity 用于排序，rule_id 用于回查规则库。"""

    code: str          # schema_mismatch / numbering / credential / ...
    severity: str      # high / check / info
    title: str
    detail: str
    rule_id: str = ""  # 命中的规则文档段落 id，用于回查
    evidence: str = ""  # 支撑该结论的原始事实（列名、行号、数字）


@dataclass
class DesignDoc:
    pipeline: str
    source_path: str
    sheets: list[Sheet] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def sheet(self, name: str) -> Sheet | None:
        for s in self.sheets:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict:
        return {
            "pipeline": self.pipeline,
            "source_path": self.source_path,
            "sheets": [
                {"name": s.name, "headers": s.headers, "rows": len(s.rows)}
                for s in self.sheets
            ],
            "findings": [
                {"code": f.code, "severity": f.severity, "title": f.title}
                for f in self.findings
            ],
        }
