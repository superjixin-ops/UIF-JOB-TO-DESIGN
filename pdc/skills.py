"""Skill 运行时：渐进披露，按需加载。

和 RAG 的区别：

    RAG      检索的是"片段"，目的是**找到相关信息**
    Skill    加载的是"一套做法"，目的是**照着做**

    所以 Skill 不能只切块扔进向量库——那样模型看到的是零散段落，
    不知道"这是一套完整流程"。

渐进披露（progressive disclosure）：

    第一步  只暴露 name + description（极小，几十 token）
    第二步  模型判断"这个任务需要它"，调 read_skill(name)
    第三步  才把全文放进上下文

    好处：10 个 skill 的元数据只占几百 token，而不是几万。

和 MCP 的区别：

    MCP      连接**外部服务**的协议（工具在另一个进程）
    Skill    本地的**知识与流程**（就是 markdown）

两者可以组合：Skill 里写"调用 mcp_xxx 工具"，MCP 提供能力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: str

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True)
class Skill:
    meta: SkillMeta
    body: str


def parse_skill(text: str, fallback_name: str = "") -> Skill:
    """解析 SKILL.md：YAML frontmatter + 正文。

    没有 frontmatter 时用目录名当 name、第一行当 description——
    宁可显示一个不完美的描述，也不要让这个 skill 消失。
    """
    meta: dict = {}
    match = FRONTMATTER.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
            if isinstance(loaded, dict):
                meta = loaded
        except yaml.YAMLError:
            meta = {}
        body = text[match.end():].strip()
    else:
        body = text.strip()

    name = str(meta.get("name") or fallback_name or "unnamed-skill").strip()
    description = str(meta.get("description") or "").strip()
    if not description:
        # 跳过标题行：`# My Skill` 不是"这个 skill 解决什么"
        first = next(
            (
                line.strip()
                for line in body.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ),
            "",
        )
        description = first[:160]

    return Skill(meta=SkillMeta(name=name, description=description, path=""), body=body)


class SkillRegistry:
    """扫描 skills 目录，提供 list / read 两个动作。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._skills: dict[str, Skill] = {}
        self._load()

    def _load(self) -> None:
        if not self.root.exists():
            return
        files = sorted(
            list(self.root.glob("*/SKILL.md")) + list(self.root.glob("*/skill.md"))
        )
        for path in files:
            skill = parse_skill(path.read_text(encoding="utf-8", errors="ignore"), path.parent.name)
            meta = SkillMeta(
                name=skill.meta.name,
                description=skill.meta.description,
                path=str(path),
            )
            self._skills[meta.name] = Skill(meta=meta, body=skill.body)

    def list(self) -> list[SkillMeta]:
        return sorted((s.meta for s in self._skills.values()), key=lambda m: m.name)

    def read(self, name: str) -> Skill:
        if name not in self._skills:
            known = ", ".join(sorted(self._skills)) or "(空)"
            raise KeyError(f"没有这个 skill: {name!r}。可用: {known}")
        return self._skills[name]

    def __len__(self) -> int:
        return len(self._skills)
