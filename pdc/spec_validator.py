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
    # 32 位以上才当密钥。真实 key 最短也在 32 位（DeepSeek 32、OpenAI 48），
    # 16 位会把 "sk-xxx-yyy" 这种占位标识符误判成泄漏。
    re.compile(r"sk-[A-Za-z0-9_-]{32,}"),
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
    """校验一段 YAML 文本。不落盘，纯函数。

    只做三件事：

        1. 结构：能不能解析、必需字段在不在、枚举值合法吗
        2. 契约：列名格式、类型、重复列、凭证、明文密钥
        3. 形状：遇到非 dict / 非 list 的字段不崩，转成可读错误

    **不做什么**（这条比做什么重要）：

        不做相邻 job 的 schema 一致性检查。
        那是"设计评审"的职责，不是"生成"的职责。
        输入文档本身可能记录着 7 列漂移，忠实实现它的产物必然带着漂移。
        把这一项放进生成门禁，模型只有两条路：永远失败，或偷偷改一列——
        后者就是编造。

    一句话：**校验器只能校验你真正要求的东西。**
    """
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
        if not isinstance(job, dict):
            issues.append(Issue(
                "E_JOB_SHAPE", f"spec.jobs 第 {idx + 1} 项不是 mapping，而是 {type(job).__name__}",
                "每个 job 必须是一个 mapping，至少包含 jobType",
            ))
            continue
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
        if not isinstance(extracts, list):
            issues.append(Issue(
                "E_EXTRACT_SHAPE", f"job {jt} 的 extract 不是列表",
                "extract 必须是列表，每一项是一个 mapping",
            ))
            extracts = []
        main_count = sum(1 for e in extracts if e.get("main_df") is True)
        if len(extracts) and main_count < 1:
            # 只提示，不阻断。
            #
            # 这条规则改过两次，每次都是"把观察当规范"：
            #   1.0  要求恰好一个 -> 样例 fact-inventory-snapshot 有两个 fact 源，卡死
            #   2.0  改成"至少一个" -> 在 CRD-7 的 5,545 条真实流水线上，
            #        19,688 个 job 里有 2,779 个（14%）确实没有 main_df，规则仍然错
            #   3.0  降级为 warning，具体哪几个由文档逐项比对（S_MAIN_DF）
            #
            # 结论：**"有没有"只能由输入文档决定，不能由校验器假设。**
            issues.append(Issue(
                "W_MAIN_DF", f"job {jt} 有 {len(extracts)} 个 extract，但一个 main_df: true 都没有",
                "如果文档里也没有标记主数据集，这是合法的；否则补上 main_df: true",
                severity="warning",
            ))

        for e in extracts:
            if not isinstance(e, dict):
                issues.append(Issue(
                    "E_EXTRACT_SHAPE", f"job {jt} 的 extract 里有非 mapping 项：{e!r}",
                    "每一项必须是 mapping",
                ))
                continue
            alias = e.get("alias") or e.get("templateName") or e.get("templateSource") or "?"
            if not UPPER_SNAKE.match(str(alias)):
                issues.append(Issue(
                    "W_ALIAS", f"extract 别名 {alias!r} 不是 UPPER_SNAKE",
                    "改成大写加下划线，例如 ORDER_LINES",
                    severity="warning",
                ))
            conns = e.get("connections") or []
            if not conns:
                # 真实语料里有大量"模板引用"型 extract：
                #   - templateName: xxx
                # 它没有内联 connections，连接定义在模板里。
                # 这不是错误，但也不能按内联连接去要求 connector。
                if e.get("templateName") or e.get("templateSource"):
                    issues.append(Issue(
                        "W_TEMPLATE_REF", f"extract {alias} 通过模板引用连接",
                        "连接定义在模板里，本文件不展开；如需列级契约，请展开模板",
                        severity="warning",
                    ))
                else:
                    issues.append(Issue(
                        "E_CONN", f"extract {alias} 没有任何 connections",
                        "补上 connections，或用 templateName 引用模板",
                    ))
                continue
            conn = conns[0] if isinstance(conns[0], dict) else {}
            if not conn.get("connector"):
                # 只提示。真实语料里有大量 connection 省略 connector 的写法
                # （credentials: cos 已经隐含了连接器）。强制要求会把合法文件判死。
                issues.append(Issue(
                    "W_CONN", f"extract {alias} 的 connection 没写 connector",
                    "补上 connector 更明确，例如 connector: cos",
                    severity="warning",
                ))
            # 不同 connector 的定位字段不同：cos/skapi 用 query，
            # box 用 file_name + path，jdbc 用 query/table。
            # 只认 query 会把真实语料里 36% 的文件判死。
            locators = ("query", "file_name", "table", "path", "topic", "directory")
            if not any(conn.get(k) for k in locators):
                issues.append(Issue(
                    "W_CONN_LOCATOR", f"extract {alias} 的 connection 没有定位字段",
                    "至少写 query / file_name / table 之一",
                    severity="warning",
                ))

        # load 契约
        load = job.get("load")
        if load:
            schema = load.get("schema") or []
            if not isinstance(schema, list):
                # 真实语料里大量写成 `schema: Template`（引用模板，不是内联列表）。
                # 这里不是错误，只是"未内联声明"——但不能拿去遍历，
                # 否则会对字符串做 .get()，整个校验器崩掉。
                issues.append(Issue(
                    "W_SCHEMA_TEMPLATE", f"job {jt} 的 load.schema 不是内联列表（{schema!r}）",
                    "如果确实由模板/connector 推断，保持原样；否则展开成完整列清单",
                    severity="warning",
                ))
                schema = []
            if jt == "eid" and not schema:
                # 同样降级。CRD-7 里 eid 的 schema 声明率是 73%——
                # 剩下 27% 靠 connector 推断（schema: Template 或省略）。
                # 强制要求 schema 会把大量合法流水线判死，而且会逼模型编列。
                issues.append(Issue(
                    "W_LOAD_SCHEMA", f"job {jt} 的 load 没有 schema",
                    "如果文档写明 schema 由 connector 推断，这是合法的；否则补上完整列",
                    severity="warning",
                ))
            seen = set()
            for col in schema:
                if not isinstance(col, dict):
                    issues.append(Issue(
                        "E_COL_SHAPE", f"job {jt} 的 schema 里有非 mapping 项：{col!r}",
                        "每一列必须是 {columnName, dataType, nullAble} 的 mapping",
                    ))
                    continue
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
