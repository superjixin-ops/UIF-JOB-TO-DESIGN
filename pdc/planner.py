"""Plan-and-Execute 引擎。

和 ReAct 的区别（面试 02 模块必考）：

    ReAct          每一步都看一步，局部贪心
                   优点：能根据观察随时改方向
                   缺点：没有全局视角，容易在细节里打转

    Plan-and-Execute  先产出完整计划，再逐步执行
                   优点：步骤可审查、可并行、可预算
                   缺点：计划错了会一路错下去

    生产里常见的是混合：先粗计划，执行中按 ReAct 调整（Plan-and-Act）。

这里的实现刻意保持简单：计划是 JSON，执行是串行复用现有 Agent 循环。
目的不是造框架，是让"两种范式的差异"可测量。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pdc.agent import AgentResult, run_agent
from pdc.llm import LLM
from pdc.tools import Toolbox

PLANNER_PROMPT = """你把任务拆成可执行的步骤。

规则：
- 最多 5 步，每步一个明确动作。
- 每步必须是"用工具能完成"的，不要写"思考一下"这种空话。
- 如果任务很小（比如只处理一个文件），就该只有 1~2 步，不要硬凑。

只输出 JSON：
{"steps": [{"title": "短标题", "goal": "这一步要做什么，具体到文件和动作"}]}"""


@dataclass
class PlanStep:
    index: int
    title: str
    goal: str
    result: str = ""
    steps_used: int = 0


@dataclass
class PlanResult:
    plan: list[PlanStep] = field(default_factory=list)
    final: str = ""
    total_steps: int = 0
    trace_stats: dict = field(default_factory=dict)


def make_plan(llm: LLM, goal: str, max_steps: int = 5) -> list[PlanStep]:
    reply = llm.chat(
        [
            {"role": "system", "content": PLANNER_PROMPT},
            {"role": "user", "content": goal},
        ],
        [],
    )
    raw = reply.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {"steps": []}

    steps = []
    for i, item in enumerate((data.get("steps") or [])[:max_steps], 1):
        if not isinstance(item, dict):
            continue
        steps.append(
            PlanStep(
                index=i,
                title=str(item.get("title", f"step {i}"))[:60],
                goal=str(item.get("goal", "")).strip(),
            )
        )
    return steps


def execute_plan(
    llm: LLM,
    toolbox: Toolbox,
    goal: str,
    *,
    max_steps_per_step: int = 4,
    system: str | None = None,
    schemas: list[dict] | None = None,
    digest: str = "",
    narrow_tools: bool = True,
) -> PlanResult:
    """先规划，再逐步执行，最后汇总。

    关键点：每一步都拿得到前面步骤的结论，但不是完整上下文——
    这正是 Plan-and-Execute 省 token 的地方。
    """
    plan = make_plan(llm, goal)
    if not plan:
        # 规划失败就退回单步执行，不要让整个任务失败
        result = run_agent(llm, toolbox, goal, system=system, schemas=schemas)
        return PlanResult(final=result.output, total_steps=result.steps)

    notes: list[str] = []
    total = 0

    for step in plan:
        carried = "\n".join(f"- {n}" for n in notes[-3:]) or "（无）"
        facts_block = (
            f"\n\n已知事实（工具已取回，不要重复 inspect）：\n{digest}" if digest else ""
        )
        step_goal = (
            f"{goal}{facts_block}\n\n当前步骤（{step.index}/{len(plan)}）：{step.title}\n"
            f"{step.goal}\n\n前序结论：\n{carried}"
        )
        # 第二步起不再给 inspect_pipeline：
        # 实测"在 prompt 里说不要重复 inspect"没用，模型还是会调。
        # 收窄能力比写提示有效——这也是"工具路由"的真实动机。
        step_schemas = schemas
        if narrow_tools and schemas is not None and step.index > 1:
            step_schemas = [sc for sc in schemas if sc["function"]["name"] != "inspect_pipeline"]

        result: AgentResult = run_agent(
            llm, toolbox, step_goal,
            max_steps=max_steps_per_step, system=system, schemas=step_schemas,
        )
        step.result = result.output
        step.steps_used = result.steps
        total += result.steps
        notes.append(f"[{step.title}] {result.output[:160]}")

    final = "\n".join(f"{s.index}. {s.title}：{s.result[:120]}" for s in plan)
    return PlanResult(plan=plan, final=final, total_steps=total)
