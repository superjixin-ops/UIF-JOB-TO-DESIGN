"""Skill 运行时的测试：渐进披露是否真的成立。

核心断言只有一条：**list 不能把正文带出来。**
如果 list 返回全文，"渐进披露"就退化成"一次性全塞"，和 RAG 没区别。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdc.skills import SkillRegistry, parse_skill
from pdc.tools import Toolbox


def _registry(tmp_path: Path) -> SkillRegistry:
    root = tmp_path / "skills"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "SKILL.md").write_text(
        """---
name: demo-skill
description: 一句话说明这个 skill 解决什么
---

# Demo

## When to use

当任务满足条件 A 时使用。

## The pattern

第一步做什么，第二步做什么。

### 细节

多源维度不能各自 overwrite，否则最后一个来源会覆盖前面的行。
正确做法是按来源分区，再用 append 合并，并补一个 SOURCE_SYSTEM 列。
这一段故意写长，用来验证"元数据远小于全文"。
""",
        encoding="utf-8",
    )
    return SkillRegistry(root)


def test_list_returns_metadata_only(tmp_path) -> None:
    registry = _registry(tmp_path)
    metas = registry.list()
    assert [m.name for m in metas] == ["demo-skill"]
    payload = "".join(str(m.to_dict()) for m in metas)
    assert "demo-skill" in payload
    assert "第一步做什么" not in payload  # 正文不能出现在元数据里
    assert "## The pattern" not in payload


def test_read_returns_body_without_frontmatter(tmp_path) -> None:
    skill = _registry(tmp_path).read("demo-skill")
    assert skill.body.startswith("# Demo")
    assert "---" not in skill.body.splitlines()[0]
    assert "第一步做什么" in skill.body


def test_unknown_skill_lists_known_names(tmp_path) -> None:
    with pytest.raises(KeyError, match="demo-skill"):
        _registry(tmp_path).read("nope")


def test_parse_without_frontmatter_falls_back() -> None:
    skill = parse_skill("# My Skill\n\n这是第一行说明。", fallback_name="dir-name")
    assert skill.meta.name == "dir-name"
    assert "第一行说明" in skill.meta.description
    assert skill.body.startswith("# My Skill")


def test_toolbox_exposes_two_skill_tools(tmp_path) -> None:
    toolbox = Toolbox(
        repo=Path("samples/pipelines"),
        rules_dir=Path("samples/rules"),
        out_dir=tmp_path,
    )
    names = {s["function"]["name"] for s in toolbox.tool_schemas()}
    assert {"list_skills", "read_skill"} <= names

    metas = toolbox.list_skills()
    assert metas and "description" in metas[0]

    full = toolbox.read_skill(metas[0]["name"])
    assert full["body"]
    assert full["truncated"] is False


def test_list_is_much_smaller_than_full(tmp_path) -> None:
    """渐进披露的收益要能量出来：元数据必须远小于全文。"""
    registry = _registry(tmp_path)
    meta_size = sum(len(str(m.to_dict())) for m in registry.list())
    full_size = sum(len(s.body) for s in (registry.read(m.name) for m in registry.list()))
    assert meta_size < full_size / 3


def test_toolbox_read_unknown_returns_error(tmp_path) -> None:
    toolbox = Toolbox(
        repo=Path("samples/pipelines"),
        rules_dir=Path("samples/rules"),
        out_dir=tmp_path,
    )
    result = toolbox.call("read_skill", {"name": "nope"})
    assert not result.ok
    assert "没有这个 skill" in (result.error or "")
