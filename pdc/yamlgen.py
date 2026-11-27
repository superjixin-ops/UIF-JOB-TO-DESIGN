"""设计文档 -> pipeline YAML 的生成 Agent。

这是仓库里第二个 Agent，方向与 design 相反：

    design 方向：YAML  ->  确定性解析  ->  文档（模型只写叙述）
    build  方向：文档  ->  模型写 YAML  ->  确定性校验（模型写产物）

为什么这个方向才算"真 Agent"：

| 判定项 | design 方向 | build 方向 |
|---|---|---|
| 最终产物是谁写的 | 代码 | **模型** |
| 路径是否固定 | 固定 SOP | 模型自己决定读哪个例子、改哪一处 |
| 终止条件 | 步数用完 | **校验器通过**（断言，不是模型说完成） |
| 装得出来吗 | 装得出来 | **装不出来**：YAML 解析不了就是解析不了 |

输入有两种，校验强度不同，这个区别是有意保留的：

  结构化文档（本工具产出的 xlsx）→ 列级契约，逐字比对（强）
  自由文本（Confluence 导出的 md）→ 实体字面出现（弱，但能抓编造）

弱校验不是偷懒：自由文本里本来就没有"精确列清单"，
强行要求精确匹配只会逼模型编一个清单来满足校验器——那才是真的危险。

控制流三件事，缺一不可：

1. **裁判先行**（spec_validator + spec_grounding）
   没有裁判，模型说"完成了"就完成了。
2. **硬门禁**（finish 会被拒绝）
   模型可以在有错的时候调 finish，工具直接驳回并附上错误清单。
3. **卡住换策略**（stuck detection）
   连续三轮同一组错误 → 说明局部修补无效，强制整篇重写。

第 3 条是真实调试出来的：模型曾经连续 4 轮都在同一个列上
把 `DecimalType(18,2)` 写成 `DecimalType`，因为它在"改一行"的心智里
不会去动整体结构。强制重写一次就过了。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdc.llm import LLM
from pdc.models import PipelineFacts
from pdc.parser import PipelineParseError, parse_pipeline
from pdc.safety import scan as scan_injection
from pdc.safety import wrap_untrusted
from pdc.spec_grounding import SpecViolation
from pdc.spec_grounding import blocking as blocking_violations
from pdc.spec_validator import Issue, is_valid, render_issues, validate_text
from pdc.trace import Timer, Tracer

BUILD_SYSTEM = """你是 UIF pipeline 配置生成专家。你的任务是把一份设计需求转换成可运行的 pipeline YAML。

铁律（违反任何一条，产物都不可用）：
1. 只使用需求里出现过的 job 类型、数据源别名、列名、数据类型、连接器、凭证。
   需求里没有的，一律不许发明。宁可少写并说明，也不要编一个看起来合理的。
2. 列契约必须逐字复制：列名大小写、dataType 字符串、nullable、分区标记。
   不要"顺手规范化"，不要补 precision，不要改类型写法。
3. 每写完一版 YAML，必须调用 validate_yaml 自检。校验不过就按 fix 逐条改。
4. validate_yaml 返回 passed=true 之后，才能调用 finish。
   如果还有 issue 就调 finish，会被直接驳回。
5. 报错信息里的 code 是稳定的，按 code 定位问题，不要凭感觉重写整篇。

工作顺序（除非需求缺失，否则不要跳步）：
  read_requirements -> list_examples -> read_example（挑最像的一个）
  -> write_yaml -> validate_yaml -> （有错）改 -> validate_yaml -> finish

写 YAML 时的结构约定（与 samples 里一致）：
  apiVersion / kind / metadata.name
  spec.owner.{name,github,contact}
  spec.jobs[]: jobType, setups.size, extract[], transform[], load{}

每个 extract: extractType/alias/main_df/connections[{credentials,connector,query,...}]
每个 load: connection{objectName,connector,mode,query,partitions} + schema[] + partitionBy[]
"""

# 模型常见的"我不调用工具，直接把 YAML 贴在正文里"。这不是错误答案，
# 是接口误用，所以自动兜底收下，但要记进 trace（面试可讲的行为观测）。
FENCED_YAML = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)


@dataclass
class Requirement:
    """生成任务的输入抽象。

    `ground` 是裁判函数：拿解析后的事实返回违规清单。
    把它做成参数而不是在 run_build 里 if/else，
    是因为"输入形态"和"控制流"必须解耦——加第三种输入不该动循环。
    """

    pipeline: str
    prompt: str
    ground: Callable[[PipelineFacts], list[SpecViolation]]
    item_count: int = 1
    label: str = "structured"
    warnings: tuple[str, ...] = ()


@dataclass
class BuildResult:
    status: str  # passed / failed / cancelled
    yaml_path: str
    rounds: int
    steps: int
    passed: bool
    issues: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    grounding: float = 0.0
    tokens: int = 0
    stuck_events: int = 0
    trace: Tracer | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "yaml": self.yaml_path,
            "rounds": self.rounds,
            "steps": self.steps,
            "passed": self.passed,
            "grounding": round(self.grounding, 4),
            "tokens": self.tokens,
            "stuck_events": self.stuck_events,
            "issues": self.issues,
            "violations": self.violations,
        }


def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required or [],
            },
        },
    }


class BuildTools:
    """build 方向的工具集。状态极小：当前候选文本 + 上次校验结果。

    工具刻意做得少（6 个）。每多一个工具，模型选错的概率就高一次；
    工具的价值在"能不能被校验"，不在数量。
    """

    def __init__(
        self,
        req: Requirement,
        *,
        examples_dir: Path | None = None,
        out_dir: Path | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.req = req
        self.examples_dir = Path(examples_dir or Path("samples/pipelines"))
        self.out_dir = Path(out_dir or Path("out"))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.tracer = tracer or Tracer()

        self.text: str = ""
        self.issues: list[Issue] = []
        self.violations: list[SpecViolation] = []
        self.parsed = False
        self.finished = False
        self.finish_summary = ""
        self.rejected_finishes = 0
        self.example_reads = 0

        self._registry: dict[str, Callable[..., Any]] = {
            "read_requirements": self.read_requirements,
            "list_examples": self.list_examples,
            "read_example": self.read_example,
            "write_yaml": self.write_yaml,
            "validate_yaml": self.validate_yaml,
            "finish": self.finish,
        }

    # ---------------- 工具实现 ----------------

    def read_requirements(self) -> dict:
        return {"requirements": self.req.prompt, "warnings": list(self.req.warnings)}

    def list_examples(self, keyword: str = "") -> list[dict]:
        out: list[dict] = []
        kw = keyword.lower()
        if not self.examples_dir.exists():
            return out
        for p in sorted(self.examples_dir.rglob("*.yaml")):
            if kw and kw not in p.name.lower():
                continue
            try:
                facts = parse_pipeline(p)
            except PipelineParseError:
                continue
            out.append(
                {
                    "name": str(p.relative_to(self.examples_dir)),
                    "kind": facts.kind,
                    "jobs": [j.job_type for j in facts.jobs],
                    "extracts": sum(len(j.extracts) for j in facts.jobs),
                    "columns": sum(len(j.load.schema) for j in facts.jobs if j.load),
                }
            )
        return out

    def read_example(self, name: str, max_lines: int = 220) -> dict:
        self.example_reads += 1
        if self.example_reads > 4:
            # 真实观测到的行为：模型会在"看示例"里打转而不动手写。
            # 这不是能力问题，是决策问题，所以用护栏而不是改 prompt。
            raise RuntimeError(
                "读取示例已达上限（4 次）。停止收集信息，直接 write_yaml；"
                "写完后用 validate_yaml 的错误清单定位问题。"
            )
        p = self.examples_dir / name
        if not p.exists():
            matches = list(self.examples_dir.rglob(Path(name).name))
            if not matches:
                raise FileNotFoundError(f"没有这个示例: {name}")
            p = matches[0]
        lines = p.read_text(encoding="utf-8").splitlines()
        content, kept = _excerpt(lines, max_lines)
        return {
            "name": str(p.relative_to(self.examples_dir)),
            "total_lines": len(lines),
            "truncated": len(lines) > max_lines,
            "content": content,
            "kept": kept,
        }

    def write_yaml(self, text: str) -> dict:
        self.text = text
        self.parsed = False
        path = self.out_dir / f"{self.req.pipeline}.draft.yaml"
        path.write_text(text, encoding="utf-8")
        return {
            "saved": str(path),
            "lines": len(text.splitlines()),
            "note": "已保存草稿。下一步调用 validate_yaml。",
        }

    def validate_yaml(self) -> dict:
        """跑完整裁判：结构校验 + 事实对齐。

        顺序不能反：结构都过不了就没法解析，谈事实对齐没有意义。
        """
        if not self.text.strip():
            return {"passed": False, "issues": [_issue("E_EMPTY", "还没有写任何 YAML")]}

        self.issues = validate_text(self.text, f"{self.req.pipeline}.yaml")
        self.violations = []
        self.parsed = False

        if is_valid(self.issues):
            tmp = self.out_dir / f".{self.req.pipeline}.candidate.yaml"
            tmp.write_text(self.text, encoding="utf-8")
            try:
                facts = parse_pipeline(tmp)
                self.parsed = True
                self.violations = self.req.ground(facts)
            except PipelineParseError as exc:
                self.issues.append(
                    Issue(
                        "E_PARSE_SEMANTIC",
                        f"YAML 语法没问题，但不符合 pipeline 结构: {exc}",
                        "对照 read_example 返回的示例，检查 jobType/extract/transform/load 的层级",
                    )
                )
            finally:
                tmp.unlink(missing_ok=True)

        passed = is_valid(self.issues) and not blocking_violations(self.violations)
        return {
            "passed": passed,
            "error_count": sum(1 for i in self.issues if i.severity == "error")
            + len(blocking_violations(self.violations)),
            "issues": [i.to_dict() for i in self.issues if i.severity == "error"],
            "grounding": [v.to_dict() for v in blocking_violations(self.violations)],
            "advice": render_issues(self.issues) if self.issues else "",
        }

    def finish(self, summary: str = "") -> dict:
        """硬门禁：校验不过，finish 一定失败。

        这是整个 Agent 的"终止条件"——不是模型说完了，是裁判说过了。
        """
        check = self.validate_yaml()
        if not check["passed"]:
            self.rejected_finishes += 1
            return {
                "accepted": False,
                "reason": "校验未通过，不能结束。按下面的清单继续修。",
                "remaining": check,
            }
        self.finished = True
        self.finish_summary = summary
        return {"accepted": True, "summary": summary}

    # ---------------- 分发 ----------------

    def schemas(self) -> list[dict]:
        out = [
            _tool("read_requirements", "重新读取设计需求。", {}),
            _tool(
                "list_examples",
                "列出可参考的 pipeline 示例（含 job 与列数概要）。",
                {"keyword": {"type": "string", "description": "文件名关键词，可空"}},
            ),
            _tool(
                "read_example",
                "读取一个示例 YAML 的正文，作为字段结构与写法的参照。",
                {
                    "name": {"type": "string", "description": "list_examples 返回的 name"},
                    "max_lines": {"type": "integer", "description": "最多读多少行，默认 220"},
                },
                ["name"],
            ),
            _tool(
                "write_yaml",
                "保存当前 YAML 草稿（覆盖上一版）。写完必须调用 validate_yaml。",
                {"text": {"type": "string", "description": "完整的 YAML 文本"}},
                ["text"],
            ),
            _tool(
                "validate_yaml",
                "用确定性校验器检查当前草稿：结构、列契约、事实对齐。返回 passed 与错误清单。",
                {},
            ),
            _tool(
                "finish",
                "声明任务完成。只有 validate_yaml 返回 passed=true 时才会被接受。",
                {"summary": {"type": "string", "description": "一句话说明产物与遗留问题"}},
            ),
        ]
        if self.example_reads >= 4:
            # 到上限就把工具从 schema 里摘掉。
            # 只报错是不够的：实测模型会反复调用一个"明确拒绝"的工具 10 次。
            # 工具不存在，它才会换路。
            out = [t for t in out if t["function"]["name"] != "read_example"]
        return out

    def call(self, name: str, args: dict) -> dict:
        fn = self._registry.get(name)
        if fn is None:
            return {"ok": False, "error": f"未知工具 {name}"}
        with Timer() as t:
            try:
                data = fn(**(args or {}))
                ok, err = True, ""
            except Exception as exc:  # noqa: BLE001 - 工具错误要回给模型，不是崩掉
                data, ok, err = {"error": f"{type(exc).__name__}: {exc}"}, False, str(exc)
        self.tracer.tool_call(name, args or {}, data, t.elapsed_ms, err)
        return {"ok": ok, "data": data}


def _slim_args(name: str, args: dict) -> dict:
    """write_yaml 的正文不进消息历史。

    模型每轮都是整篇重写，历史里留着 8 个版本的 YAML 只是把
    上下文越撑越大（实测一次失败运行烧掉 90 万输入 token）。
    真正的内容在磁盘上，校验结果里有错误位置，模型不需要回看旧全文。
    """
    if name == "write_yaml":
        text = str((args or {}).get("text", ""))
        if len(text) > 400:
            # 关键：不保留 text 字段，只留一个说明。
            #
            # 第一版写成 {"text": "<已提交 11955 字符...>"}，结果模型下一轮
            # 直接把这个占位字符串当成自己的 YAML 又提交了一遍，
            # 校验器报"YAML 无法解析"，然后连续 8 轮卡死。
            # 模型会模仿它看到的上一次参数——所以占位符不能放在原字段位置上。
            return {
                "__omitted__": len(text),
                "__note__": (
                    "上一轮的 YAML 正文已保存到磁盘，此处不回显以节省上下文。"
                    "不要复用这个参数对象；需要修改时重新提交完整 YAML。"
                ),
            }
    return args


def _excerpt(lines: list[str], max_lines: int) -> tuple[str, str]:
    """截断时要保住 `load:` 段。

    真实教训：示例文件平均 600 行，`load:` 常在 220 行之后。
    直接取前 220 行，模型永远看不到 schema 长什么样，
    于是生成时**整段省略 schema**——而这是最关键的输出契约。

    一次 12×3 的 A/B 因此被污染：结构近邻组抽到的都是长文件，
    看起来"结构检索更差"，实际是"截断把 load 切掉了"。

    规则：不超长就全给；超长就 head + load 段，load 段必须完整。
    """
    if len(lines) <= max_lines:
        return "\n".join(lines), "full"

    load_at = next((i for i, line in enumerate(lines) if line.strip() == "load:"), None)
    if load_at is None:
        return "\n".join(lines[:max_lines]), "head"

    indent = len(lines[load_at]) - len(lines[load_at].lstrip())
    end = len(lines)
    for i in range(load_at + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        cur = len(lines[i]) - len(lines[i].lstrip())
        if cur <= indent and not stripped.startswith("#"):
            end = i
            break
    block = lines[load_at:end]

    sep = ["", "# ...（中间省略，已保留 load 段）...", ""]
    if len(block) + len(sep) >= max_lines:
        # load 段自己就超长：优先给 load，头部只留 20 行上下文
        head = lines[:20]
        keep = max_lines - len(head) - len(sep)
        return "\n".join(head + sep + block[:keep]), "load-first"

    head = lines[: max_lines - len(block) - len(sep)]
    if load_at <= len(head):
        return "\n".join(lines[:max_lines]), "head"

    merged = head + sep + block
    return "\n".join(merged), "head+load"


def _issue(code: str, message: str, fix: str = "") -> dict:
    return {"code": code, "message": message, "fix": fix, "severity": "error"}


def _codes(issues: list[Issue], violations: list[SpecViolation]) -> tuple[str, ...]:
    return tuple(
        sorted({i.code for i in issues if i.severity == "error"})
        + sorted({v.code for v in violations if v.severity == "error"})
    )


STUCK_PROMPT = """你已经连续 {n} 轮出现完全相同的一组错误：{codes}

局部修补没有生效。停止逐行改，改为：
1. 从 read_example 重新拿一个最接近的示例，整篇重写；
2. 重写时优先保证列契约逐字一致（列名/类型/nullable/分区）；
3. 写完先只检查这几类错误对应的位置，再调 validate_yaml。
"""


def run_build(
    llm: LLM,
    req: Requirement,
    *,
    max_rounds: int = 8,
    max_tool_chars: int = 12000,
    examples_dir: Path | None = None,
    out_dir: Path | None = None,
    tracer: Tracer | None = None,
    budget: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> BuildResult:
    """生成循环。终止条件只有一个：validate_yaml 通过 + finish 被接受。

    设计原则：

        1. **门禁在循环里，不在循环外。** `finish` 会重新跑一次校验，
           所以"模型说完成"永远不是终止条件——校验通过才是。
           如果把校验放在循环结束后，模型中途说"完成了"就再也拉不回来。
        2. **错误清单原样退回。** 不重写、不总结、不替模型判断哪条重要。
           模型需要看到稳定的 code 和位置，才能逐条修。
        3. **卡住换策略。** 连续三轮同一组错误 -> 强制整篇重写。
           局部修补已经被证明无效（实测 8 轮改不动一个字段）。

    代价：每轮都要把完整错误清单塞进上下文。错误多时会挤占预算，
    所以错误清单要在 `render_issues` 里做截断/排序。
    """
    tools = BuildTools(req, examples_dir=examples_dir, out_dir=out_dir, tracer=tracer)
    tracer = tools.tracer

    hits = scan_injection(req.prompt)
    if hits:
        tracer.record(
            "safety",
            "requirement_injection_scan",
            input=req.pipeline,
            output=[{"kind": h.kind, "excerpt": h.excerpt} for h in hits],
        )

    goal = (
        f"请根据下面这份设计需求，生成可运行的 pipeline YAML。\n"
        f"pipeline 名称必须是 {req.pipeline}。\n\n"
        f"{req.prompt}\n\n"
        f"先读一个最接近的示例，然后 write_yaml -> validate_yaml 循环，"
        f"通过后 finish。"
    )
    messages: list[dict] = [
        {"role": "system", "content": BUILD_SYSTEM},
        {"role": "user", "content": goal},
    ]

    history: list[tuple[str, ...]] = []
    stuck_events = 0
    spent = 0
    steps = 0
    rounds = 0

    for _ in range(max_rounds * 4 + 2):
        if should_stop is not None and should_stop():
            tracer.record("final", "cancelled", output="用户取消")
            return _result(req, tools, "cancelled", steps, rounds, spent, stuck_events)

        with Timer() as t:
            reply = llm.chat(messages, tools.schemas())
        steps += 1
        spent += reply.tokens_in + reply.tokens_out
        if budget and spent > budget:
            tracer.record("final", "budget_exceeded", output=f"{spent} > {budget}")
            return _result(req, tools, "failed", steps, rounds, spent, stuck_events)

        tracer.record(
            "llm",
            getattr(llm, "name", "llm"),
            input=messages[-1].get("content", "")[:2000],
            output=reply.content,
            latency_ms=t.elapsed_ms,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
        )

        calls = list(reply.tool_calls)

        # 兜底：模型把 YAML 写在正文里而不调用工具
        if not calls:
            m = FENCED_YAML.search(reply.content or "")
            if m:
                tracer.record(
                    "safety", "yaml_in_content", output="模型未调用 write_yaml，已自动收下正文代码块"
                )
                tools.call("write_yaml", {"text": m.group(1)})
            else:
                messages.append({"role": "assistant", "content": reply.content})
                messages.append(
                    {
                        "role": "user",
                        "content": "你没有调用任何工具。请继续：write_yaml -> validate_yaml，通过后 finish。",
                    }
                )
                continue

        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(_slim_args(c.name, c.arguments), ensure_ascii=False),
                        },
                    }
                    for c in calls
                ],
            }
        )

        if any(c.name == "read_example" for c in calls):
            # 示例正文动辄几千 token，只保留最近两次。
            #
            # 注意这里踩过一个坑：最初是"只保留最新一次"，结果模型发现
            # 上一个示例"消失"了，就回头再读，读回来又把新的擦掉——
            # 实测连续 36 次 read_example、一次都没写 YAML。
            # 保留两份就稳定了，因为模型总能看到"当前参照 + 上一条对照"。
            seen = [
                msg
                for msg in messages
                if msg.get("role") == "tool" and msg.get("name") == "read_example"
            ]
            for msg in seen[:-2]:
                msg["content"] = "（更早读取的示例已省略）"

        for call in calls:
            res = tools.call(call.name, call.arguments)
            if call.name == "validate_yaml":
                rounds += 1
            payload = json.dumps(res, ensure_ascii=False, default=str)
            if len(payload) > max_tool_chars:
                payload = payload[:max_tool_chars] + f"...<truncated {len(payload) - max_tool_chars}>"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": wrap_untrusted(payload, label=f"tool:{call.name}"),
                }
            )

        # ---------- 卡住检测 ----------
        # 只在这一步真的产生了校验结果时才判断（validate 或 finish 都会重新校验）。
        #
        # 为什么不能每个 step 都判断：tools.issues 会一直保留到下一次 validate_yaml。
        # 模型"校验失败 -> 读两个示例 -> 重写一版"是正常推进，但旧 issues 还在，
        # 旧实现会把这几步当成"连续三次同样的错误"，白注入一次 STUCK_PROMPT。
        # 实测 12 条用例报了 12 次 stuck，其中 dim-date 只有 2 轮却报了 2 次。
        if any(c.name in ("validate_yaml", "finish") for c in calls):
            codes = _codes(tools.issues, tools.violations)
            if codes:
                history.append(codes)
                if len(history) >= 3 and history[-1] == history[-2] == history[-3]:
                    stuck_events += 1
                    history.clear()
                    tracer.record("safety", "stuck_detected", output=list(codes))
                    messages.append(
                        {"role": "user", "content": STUCK_PROMPT.format(n=3, codes=list(codes))}
                    )
            else:
                # 这一轮校验通过了，之前累积的失败历史不再代表"卡住"
                history.clear()

        if tools.finished:
            tracer.record("final", "verified_pass", output=tools.finish_summary)
            return _result(req, tools, "passed", steps, rounds, spent, stuck_events)

    tracer.record("final", "round_limit", output=f"用了 {rounds} 轮仍未通过")
    return _result(req, tools, "failed", steps, rounds, spent, stuck_events)


def _result(
    req: Requirement,
    tools: BuildTools,
    status: str,
    steps: int,
    rounds: int,
    spent: int,
    stuck_events: int,
) -> BuildResult:
    if tools.text:
        tools.validate_yaml()
    final_path = tools.out_dir / f"{req.pipeline}.yaml"
    if tools.text:
        final_path.write_text(tools.text, encoding="utf-8")

    bad = len(blocking_violations(tools.violations))
    total = max(req.item_count, 1)
    if req.label == "text" and tools.parsed:
        # 自由文本模式的分母要等解析出事实才知道，装配阶段算不出来
        from pdc.spec_grounding import text_item_count

        total = text_item_count(req.prompt, parse_pipeline(final_path))
    return BuildResult(
        status=status,
        yaml_path=str(final_path) if tools.text else "",
        rounds=rounds,
        steps=steps,
        passed=status == "passed",
        issues=[i.to_dict() for i in tools.issues if i.severity == "error"],
        violations=[v.to_dict() for v in tools.violations if v.severity == "error"],
        # 没解析成功 = 对齐检查根本没跑，不能报 1.0。
        # 这是上线前发现的一个假通过：结构校验失败导致 ground() 没执行，
        # 结果报告里 grounding=1.0，看起来完美，其实什么都没查。
        grounding=max(0.0, 1.0 - bad / total) if tools.parsed else 0.0,
        tokens=spent,
        stuck_events=stuck_events,
        trace=tools.tracer,
    )


# ---------------------------------------------------------------------------
# 两种输入的装配器
# ---------------------------------------------------------------------------
def requirement_from_design(spec) -> Requirement:
    """结构化文档：强契约，列级逐字比对。

    外来文档（weak=True）走弱契约：变换是自然语言翻译来的，
    模型可能合并/改写步骤，逐字比对必然失败。弱契约只查
    "列名有没有编造"和"实体在不在原文里"。
    """
    from pdc.spec_grounding import check as check_spec
    from pdc.spec_grounding import check_text, expected_items

    if getattr(spec, "weak", False):
        prompt = spec.to_prompt()
        return Requirement(
            pipeline=spec.pipeline,
            prompt=prompt,
            ground=lambda facts: check_text(
                prompt,
                facts,
                tuple((getattr(spec, "facts", None) or {}).get("allowed_aliases", ())),
            ),
            item_count=expected_items(spec),
            label="imported",
            warnings=tuple(spec.warnings),
        )

    return Requirement(
        pipeline=spec.pipeline,
        prompt=spec.to_prompt(),
        ground=lambda facts: check_spec(spec, facts),
        item_count=expected_items(spec),
        label="structured",
        warnings=tuple(spec.warnings),
    )


def requirement_from_text(text: str, pipeline: str = "") -> Requirement:
    """自由文本：弱契约，实体字面出现即可。

    pipeline 名没给就从文本里找一个"fact-xxx"形状的词，
    找不到就用 `generated-pipeline`——**不猜**。
    """
    from pdc.spec_grounding import check_text

    name = pipeline.strip()
    if not name:
        m = re.search(r"\b((?:fact|dim)-[a-z0-9-]+)\b", text or "")
        name = m.group(1) if m else "generated-pipeline"

    return Requirement(
        pipeline=name,
        prompt=text,
        ground=lambda facts: check_text(text, facts),
        item_count=1,  # 占位；真实分母在 _result 里按产出算
        label="text",
    )
