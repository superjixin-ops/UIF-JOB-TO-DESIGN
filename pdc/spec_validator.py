"""生成的 YAML 怎么判对错：确定性校验器。

这是 design→YAML agent 的"地面真相"。

没有它，模型写出来的东西只能靠人读；有了它，agent 才知道自己错了、
错在哪、下一步改什么。**幻觉治理的第一原则就是：先有裁判，再谈生成。**

规则分四类：
    结构   能不能解析、字段齐不齐
    契约   schema 有没有、相邻 job 是否一致
    命名   列名、别名是否符合规范
    安全   有没有写入越权路径、有没有硬编码密钥

每条 issue 都带 code（机器可判）和 fix（给模型看的修复建议）。
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from pdc.parser import PipelineParseError, parse_pipeline

UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ALLOWED_SIZES = {"T", "S", "M", "L", "XL", "XXL"}
ALLOWED_KIND = {"FactPipeline", "DimensionPipeline"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)(password|passwd|secret)\s*[:=]\s*\S+"),
]


@dataclass
class Issue:
    code: str
    message: str
    fix: str = ""
    severity: str = "error"   # error 阻断 / warning 建议

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "fix": self.fix,
            "severity": self.severity,
        }


def _parse(text: str) -> dict | None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def validate_text(text: str, filename: str = "pipeline.yaml") -> list[Issue]:
    """校验一段 YAML 文本。不落盘，纯函数。"""
    issues: list[Issue] = []

    # 1) 能不能解析
    data = _parse(text)
    if data is None:
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        return [Issue(
            "E_PARSE",
            f"YAML 无法解析，首个非空行是 {first[:60]!r}",
            "检查缩进是否统一为两个空格、冒号后是否有空格、字符串是否加了引号",
        )]

    # 2) 顶层字段
    kind = data.get("kind")
    if kind not in ALLOWED_KIND:
        issues.append(Issue(
            "E_KIND", f"kind 是 {kind!r}",
            "顶层写 kind: FactPipeline（事实）或 DimensionPipeline（维度）",
        ))

    meta = data.get("metadata") or {}
    name = meta.get("name")
    if not name:
        issues.append(Issue("E_NAME", "metadata.name 缺失", "写一个与文件名一致的 name"))
    elif not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", str(name)):
        issues.append(Issue(
            "W_NAME", f"metadata.name 是 {name!r}，不是 kebab-case",
            "用小写字母、数字和连字符，例如 fact-order-fulfillment",
            severity="warning",
        ))

    jobs = (data.get("spec") or {}).get("jobs") or []
    if not jobs:
        issues.append(Issue("E_JOBS", "spec.jobs 为空", "至少写一个 jobType: eid 的 job"))
        return issues

    # 3) 逐 job 检查
    for idx, job in enumerate(jobs):
        jt = job.get("jobType", f"#{idx}")
        size = (job.get("setups") or {}).get("size")
        if not size:
            issues.append(Issue(
                "E_SIZE", f"job {jt} 缺 setups.size",
                f"在 job {jt} 的 setups 里写 size: T（从小开始）",
            ))
        elif str(size).upper() not in ALLOWED_SIZES:
            issues.append(Issue(
                "E_SIZE_VALUE", f"job {jt} 的 size 是 {size!r}",
                f"只能是 {'/'.join(sorted(ALLOWED_SIZES))} 之一",
            ))

        extracts = job.get("extract") or []
        main_count = sum(1 for e in extracts if e.get("main_df") is True)
        if len(extracts) and main_count < 1:
            # 只要求"至少有一个主数据集"，不要求"只有一个"。
            #
            # 原来写的是 != 1，结果样例 fact-inventory-snapshot 直接过不了——
            # 那条流水线有两个 fact 源（库存快照 + 库存调整）join 在一起，
            # 两个都是 main_df: true。**是我的规则错了，不是数据错了。**
            #
            # 这正是"校验器把自己的假设当规范"的典型：
            # 一条从三五个样例里总结出来的规律，写进裁判就变成了硬约束。
            # 现在改成只检查"有没有"，具体哪几个由文档逐项比对（S_MAIN_DF）。
            issues.append(Issue(
                "E_MAIN_DF", f"job {jt} 有 {len(extracts)} 个 extract，但一个 main_df: true 都没有",
                "至少把一个主数据集的 main_df 写成 true",
            ))

        for e in extracts:
            alias = e.get("alias", "?")
            if not UPPER_SNAKE.match(str(alias)):
                issues.append(Issue(
                    "W_ALIAS", f"extract 别名 {alias!r} 不是 UPPER_SNAKE",
                    "改成大写加下划线，例如 ORDER_LINES",
                    severity="warning",
                ))
            conn = (e.get("connections") or [{}])[0]
            for key in ("connector", "query"):
                if not conn.get(key):
                    issues.append(Issue(
                        "E_CONN", f"extract {alias} 的 connection 缺 {key}",
                        f"补上 {key}，例如 connector: cos / query: landing/xxx",
                    ))

        # load 契约
        load = job.get("load")
        if load:
            schema = load.get("schema") or []
            if jt == "eid" and not schema:
                issues.append(Issue(
                    "E_LOAD_SCHEMA", f"job {jt} 的 load 没有 schema",
                    "把输出列写进 load.schema：columnName + dataType + nullAble",
                ))
            seen = set()
            for col in schema:
                cn = str(col.get("columnName", ""))
                if not UPPER_SNAKE.match(cn):
                    issues.append(Issue(
                        "W_COLNAME", f"列名 {cn!r} 不是 UPPER_SNAKE",
                        "统一成大写加下划线", severity="warning",
                    ))
                if cn in seen:
                    issues.append(Issue(
                        "E_DUP_COL", f"列名 {cn!r} 重复",
                        "schema 里同一列只能出现一次",
                    ))
                seen.add(cn)
                if not col.get("dataType"):
                    issues.append(Issue(
                        "E_COL_TYPE", f"列 {cn} 缺 dataType",
                        "写 IntegerType / StringType / DecimalType 等",
                    ))

    # 4) 注意：这里**不检查**相邻 job 的 schema 一致性。
    #
    # 这是踩过的一个坑。schema_diff 是"设计评审"的检查项，不是"生成"的检查项。
    # 输入文档本身可能就记录了 7 列漂移（样例 fact-order-fulfillment 就是），
    # 忠实实现这份文档的产物必然带着同样的漂移。把它当阻断项，
    # 模型只有两条路：要么永远过不了，要么偷偷加一列/删一列——后者是编造。
    #
    # 结论：**校验器只能校验你真正要求的东西。**
    # 要求"忠实实现"就不能同时要求"比输入更正确"。

    # 5) 语义：能不能被真正的解析器接受。
    #
    # 只做"结构可解析"，不做"schema 一致"——后者是设计评审的事，
    # 输入文档自己就带着缺陷时，强制修复等于逼模型编造。
    issues.extend(_parse_issues(text))

    # 6) 安全
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            issues.append(Issue(
                "E_SECRET", "内容里疑似有密钥或密码",
                "凭据只写名称（如 demo-cos），不要写值",
            ))
            break
    if ".." in text and "query:" in text:
        issues.append(Issue(
            "W_PATH", "路径里出现 ..",
            "避免越权路径，用相对仓库根的路径", severity="warning",
        ))

    return issues


def _parse_issues(text: str) -> list[Issue]:
    """用真实解析器试一次，把解析器的错误翻译成给模型看的说明。"""
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(text)
            tmp = Path(fh.name)
        parse_pipeline(tmp)
        return []
    except PipelineParseError as exc:
        return [Issue(
            "E_PARSE_SEMANTIC",
            f"能被 YAML 解析，但不符合 pipeline 结构：{exc}",
            "对照 read_example 的示例，检查 spec.jobs[].jobType / extract / transform / load 层级",
        )]
    except Exception as exc:  # noqa: BLE001
        # 校验器绝不能因为输入畸形而崩。崩了模型只看到 500，学不到任何东西。
        return [Issue(
            "E_PARSE_SEMANTIC",
            f"解析时出现意外错误：{type(exc).__name__}: {exc}",
            "对照示例检查字段层级；若确认是校验器缺陷，请保留输入以便复现",
        )]
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def is_valid(issues: list[Issue]) -> bool:
    return not any(i.severity == "error" for i in issues)


def render_issues(issues: list[Issue]) -> str:
    """给模型看的错误清单。带 code 便于它引用。"""
    if not issues:
        return "校验通过，没有发现问题。"
    lines = []
    for i, issue in enumerate(issues, 1):
        tag = "错误" if issue.severity == "error" else "建议"
        lines.append(f"{i}. [{issue.code}] {tag}：{issue.message}")
        if issue.fix:
            lines.append(f"   修复：{issue.fix}")
    return "\n".join(lines)
