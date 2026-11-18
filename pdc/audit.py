"""审计模式：给它一个目录，它自己决定看什么。

和生成模式的根本区别：

    生成模式 —— 目标固定（"为 X 生成文档"），路径可变。写得出 SOP。
    审计模式 —— 目标开放（"找出值得关注的问题"），看哪些文件、看几个、
                什么时候停，全部由模型决定。**写不出 SOP。**

所以这个模式没有 offline 版本不是偷懒，是因为它无法用固定流程实现。
这也是"是不是真 agent"最直接的分界线。

防跑飞的三道闸：
    1. 预算（最多看几个文件、最多几步）写在 prompt 里
    2. 工具层强制：evidence 路径必须存在，severity 必须合法
    3. 循环层兜底：max_steps
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pdc.agent import AgentResult, run_agent
from pdc.tools import Toolbox

AUDIT_PROMPT = """你是数据流水线审计员。用户会给你一个目录，你在里面自主查找值得关注的问题。

工作方式：
1. 先用 list_pipelines 看目录里有什么。
2. 自己决定先看哪个、看几个。不需要全看，但要能说明为什么停。
3. 对看过的文件用 inspect_pipeline 拿事实，必要时用 diff_schema 做精确比对。
4. 需要判断某个做法是否符合规范时，用 search_rules 查规则。
5. 每确认一条问题，用 record_finding 记录，必须带 evidence（文件路径）。
6. 认为覆盖够了，调用 finish_audit 给出总结。

严重度判据（必须遵守）：
- high   = 静默失败：流水线会跑成功，但数据是错的
- check  = 需要人确认，可能是有意的设计
- info   = 失败了会立刻响，或者本来就属启发式判断

硬性约束：
- 只报告你能用工具证实的事情。猜到的不要写。
- 如果一个问题你无法确定是缺陷还是设计，写成 check 并在 detail 里说清不确定在哪。
- 不要重复记录同一条问题。

记录结论时必须声明 basis：
- 如果数字来自 diff_schema 等工具，basis="tool"
- 如果是你自己读文件看出来的，basis="file"

预算：最多检查 {max_files} 个文件，最多 {max_steps} 步。"""


VERIFY_PROMPT = """你在做事实核查。给你一条结论和它引用的文件内容，判断结论是否有依据。

规则：
- 只根据给出的文件内容判断，不要凭常识推测。
- 结论说"缺少 X"，但文件里明明有 X，判 false。
- 结论说"存在 Y"，但文件里找不到 Y，判 false。
- 结论需要文件之外的信息才能判断（比如"这会导致线上报错"），判 true，
  因为那属于推理，不属于事实错误。

只输出 JSON：{"supported": true 或 false, "reason": "一句话理由"}"""


def verify_findings(llm, findings: list[dict], max_chars: int = 8000) -> tuple[list[dict], list[dict]]:
    """逐条核查模型自己的结论。

    这是 generator-critic 模式：
        生成者（审计循环）容易"看到想看到的"，
        批评者（本函数）只看文件原文，不复用上下文。
    两轮成本很低，但能把"编造"挡在报告之外。
    """
    import json as _json

    kept: list[dict] = []
    rejected: list[dict] = []

    for f in findings:
        if f.get("basis") == "tool":
            # 确定性工具算出来的结论，模型读原文复核只会引入噪声
            kept.append({**f, "verify_reason": "由工具计算得出，跳过模型复核"})
            continue

        evidence = f.get("evidence", "")
        if not evidence or not Path(evidence).exists():
            rejected.append({**f, "verify_reason": "没有可核查的证据文件"})
            continue

        content = Path(evidence).read_text(encoding="utf-8", errors="ignore")[:max_chars]
        prompt = (
            f"结论：{f['title']}\n细节：{f['detail']}\n\n"
            f"文件 {Path(evidence).name} 的内容：\n{content}"
        )
        try:
            reply = llm.chat(
                [{"role": "system", "content": VERIFY_PROMPT}, {"role": "user", "content": prompt}],
                [],
            )
            data = _json.loads(reply.content or "{}")
            supported = bool(data.get("supported", True))
            reason = str(data.get("reason", ""))
        except Exception as exc:  # noqa: BLE001
            supported, reason = True, f"核查失败，保留原结论: {exc}"

        item = {**f, "verify_reason": reason}
        (kept if supported else rejected).append(item)

    return kept, rejected


@dataclass
class AuditResult:
    summary: str
    findings: list[dict] = field(default_factory=list)
    inspected: list[str] = field(default_factory=list)
    steps: int = 0
    stats: dict = field(default_factory=dict)
    rejected: list[dict] = field(default_factory=list)


def run_audit(
    llm,
    scope: str | Path,
    *,
    max_files: int = 6,
    max_steps: int = 18,
    out_dir: Path | None = None,
) -> AuditResult:
    scope = Path(scope)
    if not scope.exists():
        raise FileNotFoundError(f"目录不存在: {scope}")

    toolbox = Toolbox(repo=scope, out_dir=out_dir or Path("out/audit"))
    system = AUDIT_PROMPT.format(max_files=max_files, max_steps=max_steps)

    goal = (
        f"审计目录 `{scope}` 下的流水线配置，找出值得关注的问题。\n"
        f"你可以自主决定看哪些文件、看几个。最后用 finish_audit 收尾。"
    )

    result: AgentResult = run_agent(
        llm,
        toolbox,
        goal,
        max_steps=max_steps,
        system=system,
        schemas=Toolbox.schemas(include_audit=True),
    )

    kept, rejected = verify_findings(llm, list(toolbox.audit_findings))

    return AuditResult(
        summary=toolbox.audit_summary or result.output,
        findings=kept,
        rejected=rejected,
        inspected=list(toolbox.inspected),
        steps=result.steps,
        stats=result.trace.stats(),
    )


def render_report(result: AuditResult, scope: Path, out_path: Path) -> Path:
    order = {"high": 0, "check": 1, "info": 2}
    findings = sorted(result.findings, key=lambda f: order.get(f["severity"], 9))

    lines = [
        "# 流水线审计报告",
        "",
        f"- 范围：`{scope}`",
        f"- 检查文件数：{len(result.inspected)} / {len(list(scope.glob('*.yaml')))}",
        (
            f"- 发现问题：{len(findings)}"
            f"（high {sum(1 for f in findings if f['severity'] == 'high')} / "
            f"check {sum(1 for f in findings if f['severity'] == 'check')} / "
            f"info {sum(1 for f in findings if f['severity'] == 'info')}）"
        ),
        f"- 步数：{result.steps}",
        "",
        "## 看过的文件",
        "",
    ]
    for name in result.inspected:
        lines.append(f"- `{name}`")

    lines += ["", "## 发现", ""]
    if not findings:
        lines.append("未发现问题。")
    for i, f in enumerate(findings, 1):
        lines += [
            f"### {i}. [{f['severity'].upper()}] {f['title']}",
            "",
            f["detail"],
        ]

        if f.get("evidence"):
            lines.append(f"\n证据：`{f['evidence']}`")
        lines.append("")

    if result.rejected:
        lines += ["## 自检驳回（模型生成后自己核查没通过）", ""]
        for f in result.rejected:
            lines += [
                f"- **{f['title']}** — {f.get('verify_reason', '无理由')}",
                f"  - 原文：{f['detail'][:120]}",
            ]
        lines.append("")

    lines += ["## 结论", "", result.summary, ""]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main(scope: str, model: str = "deepseek-chat", out: str = "out/audit") -> int:
    from pdc.llm import OpenAICompatibleLLM

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = run_audit(OpenAICompatibleLLM(model), scope, out_dir=out_dir)
    report = render_report(result, Path(scope), out_dir / "audit-report.md")

    print(f"检查了 {len(result.inspected)} 个文件，{result.steps} 步")
    for name in result.inspected:
        print(f"  - {name}")
    print()
    for f in sorted(result.findings, key=lambda x: x["severity"]):
        print(f"[{f['severity']:5}] {f['title']}")
    if result.rejected:
        print(f"\n自检驳回 {len(result.rejected)} 条:")
        for f in result.rejected:
            print(f"  x {f['title'][:50]} — {f.get('verify_reason', '')[:50]}")
    print(f"\n报告: {report}")
    print(json.dumps(result.stats, ensure_ascii=False))
    return 0
