"""引用审计：检查模型在自然语言里引用的规则锚点是否真实存在。

为什么单独做这个：
  grounding.py 只能检查结构化字段（Finding.rule_id）。
  但模型真正的输出是**一段话**，它会写
      「按规则 08-common-mistakes-and-corrections#Load Schema ...」
  这个锚点可能是真的，也可能是编的，而且编得非常像。

  实测：DeepSeek 在第一次运行里引用 3 个锚点，全部真实；
  但这不构成保证 —— 换个模型、换个 prompt 就可能编。

审计规则：
  1. 文件名必须存在
  2. # 后的锚点必须能匹配到该文件的某个标题（忽略大小写、空格、编号）
  3. 纯数字锚点（#6）匹配 "### 6. xxx" 形式的编号标题
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pdc.paths import rules_dir as _rules_dir

# 锚点允许带空格（模型习惯写 "#Load Schema"），中文和标点天然终止匹配
CITE_RE = re.compile(r"\b([0-9]{2}-[a-z0-9-]+)(?:\.md)?#([A-Za-z0-9][A-Za-z0-9 ._/-]{0,60})")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def _norm(text: str) -> str:
    """归一化：去编号、去标点、转小写、压空格。"""
    text = re.sub(r"^[\d.]+\s*", "", text.strip())
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class Heading:
    level: int
    text: str
    number: str = ""      # "6" / "11.2"
    normalized: str = ""


class RuleCitations:
    def __init__(self, rules_dir: Path | None = None) -> None:
        self.rules_dir = Path(rules_dir) if rules_dir else _rules_dir()
        self._cache: dict[str, list[Heading]] = {}

    def headings(self, stem: str) -> list[Heading] | None:
        if stem in self._cache:
            return self._cache[stem]
        path = self.rules_dir / f"{stem}.md"
        if not path.exists():
            return None
        out: list[Heading] = []
        for m in HEADING_RE.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            raw = m.group(2).strip()
            num = ""
            nm = re.match(r"^(\d+(?:\.\d+)*)\.?\s+", raw)
            if nm:
                num = nm.group(1)
            out.append(Heading(len(m.group(1)), raw, num, _norm(raw)))
        self._cache[stem] = out
        return out

    def check(self, text: str) -> list[str]:
        """返回问题描述列表。空列表 = 所有引用都可回查。"""
        problems: list[str] = []
        for stem, anchor in CITE_RE.findall(text):
            hs = self.headings(stem)
            if hs is None:
                problems.append(f"[citation_file] 引用了不存在的规则文件: {stem}.md")
                continue
            if self._matches(hs, anchor):
                continue
            problems.append(f"[citation_anchor] {stem}#{anchor} 找不到对应标题")
        return problems

    @staticmethod
    def _matches(headings: list[Heading], anchor: str) -> bool:
        anchor = anchor.strip().rstrip(".")

        # 纯数字锚点：匹配 "### 6. xxx" 这种编号
        # 注意：必须先判数字再 normalize，因为 _norm 会把前导编号剥掉，
        # 导致 "6" 变成空串。
        if re.fullmatch(r"\d+(?:\.\d+)*", anchor.strip("#")):
            return any(h.number == anchor.strip("#") for h in headings)

        target = _norm(anchor)
        if not target:
            return False

        # 文字锚点：区分两种偏差
        #   截断（anchor 是标题的前缀）—— 可接受，模型常省略 "& Dimensions" 这类尾巴
        #   添油加醋（anchor 比标题更长更具体）—— 可疑，通常是编造
        from difflib import SequenceMatcher

        for h in headings:
            if h.normalized.startswith(target):
                return True

        best = 0.0
        for h in headings:
            ratio = SequenceMatcher(None, target, h.normalized).ratio()
            best = max(best, ratio)
        return best >= 0.85
