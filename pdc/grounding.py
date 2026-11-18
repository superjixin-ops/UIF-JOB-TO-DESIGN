"""幻觉控制：把模型的每一条断言拉回可验证的事实。

核心思想：**幻觉不是一个"模型问题"，是一个"架构问题"。**
你没法让模型不说错话，但你可以让它说的话必须能对上证据。

三层防线（这也是面试要能讲清楚的结构）：

  第一层 预防（Prevention）
    - 工具优先于记忆：所有事实来自工具返回值
    - 低温 + 明确禁止推断
    - 事实不足时输出 "not asserted"

  第二层 检测（Detection）—— 本文件
    - 引用可回查：rule_id 必须真实存在
    - 实体可对齐：提到的列名必须出现在解析结果里
    - 数字可复算：说"6 列"就必须真的能数出 6 个
    - 证据可定位：evidence 路径必须存在

  第三层 兜底（Containment）
    - 检测失败不阻断，但降级：标记 unverified，不进入"已确认"清单
    - 关键路径（写库、发 PR）必须 grounding 通过才放行

这三层是递进的，缺任何一层都会漏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pdc.ir import DesignDoc, Finding
from pdc.models import PipelineFacts

# 只匹配带下划线的大写标识符，避免把 YAML / JSON / API 这类普通缩写误判成列名
IDENT_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
COUNT_LIST_RE = re.compile(r"(\d+)\s*(列|个|条)[^\[\]]{0,30}\[([^\[\]]*)\]")
BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")


@dataclass
class Violation:
    code: str
    claim: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message} <- {self.claim[:60]}"


class GroundingChecker:
    """拿事实去核对文档里的断言。

    注意：它检查的是"能不能被事实支持"，不是"说得对不对"。
    业务上对不对需要人，工程上能不能追溯需要这个。
    """

    def __init__(self, facts: PipelineFacts, rule_ids: set[str] | None = None) -> None:
        self.facts = facts
        self.rule_ids = rule_ids or set()
        self.known_columns = self._collect_columns()
        self.known_paths = {facts.source_path}

    def _collect_columns(self) -> set[str]:
        cols: set[str] = set()
        for job in self.facts.jobs:
            if job.load:
                cols.update(c.name for c in job.load.schema)
            for t in job.transforms:
                if t.output:
                    cols.add(t.output)
                cols.update(x for x in t.inputs if IDENT_RE.fullmatch(x))
        return cols

    # ------------------------------------------------------------------
    def check(self, doc: DesignDoc) -> list[Violation]:
        out: list[Violation] = []
        for i, f in enumerate(doc.findings, 1):
            out.extend(self._check_finding(i, f))
        return out

    def _check_finding(self, index: int, f: Finding) -> list[Violation]:
        prefix = f"finding#{index} {f.code}"
        out: list[Violation] = []

        # 1) 规则引用必须真实存在，防止模型编造一个看起来很像的 rule id
        if f.rule_id and self.rule_ids and f.rule_id not in self.rule_ids:
            out.append(Violation("unknown_rule", prefix,
                                 f"引用了不存在的规则: {f.rule_id}"))

        text = f"{f.title} {f.detail}"

        # 2) 提到的列名必须真的在这条流水线里出现过
        for ident in set(IDENT_RE.findall(text)):
            if ident not in self.known_columns:
                out.append(Violation("ungrounded_column", prefix,
                                     f"提到了不存在的列: {ident}"))

        # 3) "N 列" 必须和后面方括号里的实际数量一致
        out.extend(self._check_counts(prefix, text))

        # 4) 证据路径必须存在
        if f.evidence and not Path(f.evidence).exists():
            out.append(Violation("missing_evidence", prefix,
                                 f"证据路径不存在: {f.evidence}"))

        return out

    def _check_counts(self, prefix: str, text: str) -> list[Violation]:
        """只校验"数字紧跟一个方括号清单"的情况。

        形如 "6 列在数据帧里但 schema 未声明: ['A','B',...]"
        这样只比对能对得上的计数，不会把 "6 张表" 这类误判。
        """
        out: list[Violation] = []
        for raw, unit, body in COUNT_LIST_RE.findall(text):
            items = [x.strip().strip("'\"") for x in body.split(",") if x.strip()]
            declared = int(raw)
            if items and declared != len(items):
                out.append(
                    Violation(
                        "number_mismatch",
                        prefix,
                        f"声称 {declared} {unit}，清单里实际有 {len(items)} 个",
                    )
                )
        return out


def ground(doc: DesignDoc, facts: PipelineFacts, rule_ids: set[str] | None = None) -> list[Violation]:
    return GroundingChecker(facts, rule_ids).check(doc)


def grounding_score(violations: list[Violation], findings_count: int) -> float:
    """被事实支持的断言比例。没有任何 finding 时视为 1.0（空集不产生幻觉）。"""
    if findings_count == 0:
        return 1.0
    clean = max(findings_count - len({v.claim for v in violations}), 0)
    return round(clean / findings_count, 3)
