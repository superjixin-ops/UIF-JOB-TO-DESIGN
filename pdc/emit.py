"""计划字典 -> pipeline YAML 文本。

为什么单独一层：

    模型写 YAML 时，提示词里的形状会变成产物的形状。
    参数写成嵌套，模型就照抄嵌套；字段名写错，模型就照抄错的。
    实测丢过 Trim 的 columns、RenameColumns 的 mapColumns、Cast 的 columns，
    三次都是"校验通过"——因为校验器只看类型，不看参数是否为空。

    模板家族内能确定性生成的部分，直接由代码渲染，不经过模型。
    YAML 只是"计划"的序列化：计划是 dict，渲染是纯函数。

    模型仍然负责它擅长的部分：把自然语言段落翻译成表达式。
    它不再负责"记住 71 个变换的顺序和参数"——那不是语言能力，是编译。
"""

from __future__ import annotations

from pathlib import Path

import yaml


def dump(plan: dict) -> str:
    """把计划渲染成 YAML 文本。

    width 放到 200 而不是默认的 80：
    长表达式被折行后，人读起来要在脑子里拼回去，评审时很容易看漏。
    """
    text = yaml.safe_dump(
        plan,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=200,
    )
    if not text.endswith("\n"):
        text += "\n"
    return text


def write(plan: dict, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump(plan), encoding="utf-8")
    return target
