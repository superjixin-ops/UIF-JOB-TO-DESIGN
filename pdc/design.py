"""PipelineFacts -> DesignDoc（IR）。

这一层是纯函数、零模型、零 IO。
它是整个 Agent 的"下限保证"：即使模型完全不工作，也能产出结构正确的文档骨架。
"""

from __future__ import annotations

import json

from pdc.checks import schema_diff
from pdc.ir import DesignDoc, Finding, Sheet
from pdc.models import Column, JobSpec, PipelineFacts

# 每个 job 的类型说明。未知类型给空串，不猜。
JOB_PURPOSE = {
    "landing": "读取源表并附加代理键，作为维度管理的原始层",
    "eid": "抽取 + 转换，产出 EID 落地数据",
    "datamart-parquet": "整理为 datamart 层，写 parquet",
    "datamart-self-serve": "把 datamart 发布到自助查询库",
    "datamart-epmm": "把 datamart 发布到受管报表库",
}

CONNECTOR_LABEL = {
    "cos": "IBM COS",
    "skapi": "SKAPI",
    "jdbc": "JDBC",
    "jdbc_et": "JDBC (ET)",
    "erdm_api": "ERDM API",
}


CRED_FAMILIES = ("cos", "skapi", "jdbc", "db2", "erdm")


def _credential_family(name: str) -> str:
    """从凭证名或 connector 名推断它属于哪个系统。

    认不出来就返回空串，表示"无法判断"，此时不报警——
    宁可漏报也不要制造噪音。
    """
    low = (name or "").lower()
    if "jdbc" in low:
        return "jdbc"
    for key in CRED_FAMILIES:
        if key in low:
            return "jdbc" if key == "db2" else key
    return ""


def _size_note(specs: list[JobSpec]) -> str:
    return " / ".join(f"{j.job_type}: {j.size}" for j in specs)


def build_overview(facts: PipelineFacts) -> Sheet:
    s = Sheet("Overview", ["Item", "Detail"], widths=[26, 110])
    total_tx = sum(len(j.transforms) for j in facts.jobs)
    total_ex = sum(len(j.extracts) for j in facts.jobs)
    s.add("Pipeline", facts.name)
    s.add("Kind", facts.kind)
    s.add("Owner", facts.owner)
    s.add("Source", facts.source_path)
    s.add("Jobs", len(facts.jobs))
    s.add("Extracts (total)", total_ex)
    s.add("Transforms (total)", total_tx)
    s.add("Sizing", _size_note(facts.jobs))
    s.add("", "")
    s.add("JOB PURPOSES", "")
    for j in facts.jobs:
        s.add(j.job_type, JOB_PURPOSE.get(j.job_type, "(未在映射表中，请人工补充)"))
    s.add("", "")
    s.add("NOTE", "本表由工具从 YAML 直接推导，不包含模型生成内容。")
    return s


def build_source_inventory(facts: PipelineFacts) -> Sheet:
    s = Sheet(
        "Source Inventory",
        [
            "#", "Job", "Alias", "Connector", "Credentials", "Query / Path", "main_df",
            "Natural key", "Type", "Conn Alias", "Key Name", "Flags",
        ],
        widths=[5, 20, 34, 12, 22, 56, 9, 40, 14, 18, 14, 34],
    )
    n = 0
    for j in facts.jobs:
        for e in j.extracts:
            n += 1
            s.add(
                n,
                j.job_type,
                e.alias,
                CONNECTOR_LABEL.get(e.connector, e.connector),
                e.credentials,
                e.query,
                "Y" if e.main_df else "N",
                ", ".join(e.natural_key) if e.natural_key else "-",
                e.extract_type,
                e.conn_alias or "-",
                e.key_name or "-",
                json.dumps(dict(e.flags), ensure_ascii=False) if e.flags else "-",
            )
    return s


def build_transform_chain(facts: PipelineFacts) -> Sheet:
    # Detail 给人看，Params 给机器读。
    #
    # 为什么要两列：Detail 为了可读性把 dict/list 参数丢掉了，
    # 而 columns / mapColumns / columnsToSelect 恰恰是变换的全部语义。
    # 只留 Detail 的文档是有损的——用这份文档反推 YAML 时，
    # 重命名映射会整体消失，而校验器（当时只核对列名集合）看不出问题。
    # 这是反向 round-trip 测试暴露出来的真实缺陷。
    s = Sheet(
        "Transform Chain",
        ["#", "Job", "Step", "transformType", "Inputs", "Output", "Detail", "Params"],
        widths=[5, 20, 6, 22, 44, 30, 60, 60],
    )
    for j in facts.jobs:
        for t in j.transforms:
            detail = ", ".join(
                f"{k}={v}" for k, v in t.detail.items() if not isinstance(v, (dict, list))
            )
            if t.sql:
                detail = (t.sql[:300] + "...") if len(t.sql) > 300 else t.sql
            params = json.dumps(t.detail, ensure_ascii=False, default=str) if t.detail else ""
            if len(params) > 4000:
                params = params[:4000] + "…"
            s.add(t.index, j.job_type, t.index, t.transform_type,
                  ", ".join(t.inputs), t.output or "-", detail, params)
    return s


def build_landing_sheet(facts: PipelineFacts, job: JobSpec, src_job: JobSpec | None) -> Sheet:
    """为某个 job 的 load 生成一列一行的落地契约表。"""
    assert job.load is not None
    title = f"Landing - {job.job_type}"[:31]
    s = Sheet(
        title,
        ["#", "Target Column", "Data Type", "Precision", "Nullable", "Partition", "Note"],
        widths=[5, 40, 16, 12, 10, 20, 52],
    )
    partition = set(job.load.partition_by)
    for i, col in enumerate(job.load.schema, 1):
        note = ""
        if src_job and src_job.load:
            note = "来自上游 landing" if col.name in {c.name for c in src_job.load.schema} else "新增列"
        s.add(
            i,
            col.name,
            col.data_type,
            f"{col.precision},{col.scale}" if col.precision is not None else "-",
            "Y" if col.nullable else "N",
            "Y" if col.name in partition else "-",
            note,
        )
    if not job.load.schema:
        s.add("-", "(该 job 未声明 schema)", "-", "-", "-", "-", "无法比对，见 Review Notes")
    return s


def build_load_targets(facts: PipelineFacts) -> Sheet:
    """列出每个 job 的所有 load object。

    有些 pipeline 一个 job 写多张表，且不声明 schema（列由 connector 推断）。
    这种 job 无法出列级契约表，但必须把目标表列清楚，否则文档不完整。
    """
    s = Sheet(
        "Load Targets",
        [
            "#", "Job", "Object", "Metadata Name", "Target Path / Table", "Mode",
            "Partitions", "Partition By", "Schema", "Conn Alias", "Table Space",
            "Connector", "Credentials", "Flip Alias",
        ],
        widths=[5, 20, 34, 34, 56, 12, 12, 20, 10, 18, 16, 12, 18, 10],
    )
    n = 0
    for j in facts.jobs:
        if not j.load:
            continue
        objects = j.load.objects or ()
        for o in objects:
            n += 1
            s.add(n, j.job_type, o.load_object or "-", j.load.object_name or "-", o.query, o.mode,
                  o.partitions if o.partitions is not None else "-",
                  ", ".join(o.partition_by) or "-",
                  "declared" if j.load.schema else "inferred",
                  j.load.conn_alias or "-",
                  j.load.table_space or "-",
                  j.load.connector or "-",
                  j.load.credentials or "-",
                  "Y" if j.load.flip_alias else "N")
    if n == 0:
        s.add("-", "-", "(无 load)", "-", "-", "-", "-", "-")
    return s


def build_schema_diff_sheet(facts: PipelineFacts) -> Sheet:
    s = Sheet(
        "Schema Diff",
        ["From Job", "To Job", "Issue", "Columns"],
        widths=[22, 22, 28, 92],
    )
    for i, job in enumerate(facts.jobs):
        for issue in schema_diff(facts, i):
            code = issue.split("]")[0].strip("[")
            rest = issue.split("]", 1)[1].strip() if "]" in issue else issue
            cols = rest.split(":", 1)[1].strip() if ":" in rest else rest
            s.add(job.job_type, facts.jobs[i + 1].job_type if i + 1 < len(facts.jobs) else "-", code, cols)
    if not s.rows:
        s.add("-", "-", "no_diff", "所有相邻 job 的列一致")
    return s


def build_design(facts: PipelineFacts) -> DesignDoc:
    """确定性构建设计文档 IR。不含任何模型调用。"""
    doc = DesignDoc(pipeline=facts.name, source_path=facts.source_path)

    doc.sheets.append(build_overview(facts))
    doc.sheets.append(build_source_inventory(facts))

    # 每个有 schema 的 load 出一张落地表
    for i, job in enumerate(facts.jobs):
        if job.load and job.load.schema:
            doc.sheets.append(build_landing_sheet(facts, job, facts.jobs[i - 1] if i else None))

    doc.sheets.append(build_load_targets(facts))
    doc.sheets.append(build_transform_chain(facts))
    doc.sheets.append(build_schema_diff_sheet(facts))

    doc.findings.extend(derive_findings(facts))
    doc.sheets.append(build_review_sheet(doc.findings))
    return doc


def derive_findings(facts: PipelineFacts) -> list[Finding]:
    """从事实里推导评审发现。这里放"能用规则判断"的部分。"""
    out: list[Finding] = []

    # 1. schema 漂移
    for i, job in enumerate(facts.jobs):
        for issue in schema_diff(facts, i):
            code = issue.split("]")[0].strip("[")
            if code == "schema_undeclared":
                continue  # 下面第 2 组会统一报告，避免重复
            out.append(
                Finding(
                    code=code,
                    severity="high" if code == "schema_mismatch" else "check",
                    title=f"{job.job_type} 与下游 schema 不一致",
                    detail=issue,
                    rule_id="02-load-schema",
                    evidence=facts.source_path,
                )
            )

    # 2. 未声明 schema 的 load
    for job in facts.jobs:
        if job.load and not job.load.schema:
            out.append(
                Finding(
                    code="schema_undeclared",
                    severity="check",
                    title=f"{job.job_type} 的 load 没有 schema 声明",
                    detail="无法做列级契约校验，列名/类型完全依赖运行时推断。",
                    rule_id="02-load-schema",
                )
            )

    # 3. 空 transform 的 job
    for job in facts.jobs:
        if not job.transforms and job.load:
            out.append(
                Finding(
                    code="no_transform",
                    severity="info",
                    title=f"{job.job_type} 无 transform",
                    detail="纯搬运 job，确认是否有意为之。",
                )
            )

    # 4. 没有 main_df
    for job in facts.jobs:
        if job.extracts and not any(e.main_df for e in job.extracts):
            out.append(
                Finding(
                    code="no_main_df",
                    severity="check",
                    title=f"{job.job_type} 没有标记 main_df",
                    detail="多 extract 场景下缺少主数据集，join 顺序依赖隐式行为。",
                )
            )
        if sum(1 for e in job.extracts if e.main_df) > 1:
            out.append(
                Finding(
                    code="multi_main_df",
                    severity="high",
                    title=f"{job.job_type} 有多个 main_df",
                    detail="只应有一个主数据集，多个会导致不可预期的笛卡尔或丢失。",
                )
            )

    # 5. 凭证与 connector 不匹配
    #
    # 之前是白名单硬编码 ("", "skapi", "cos")，凡是叫 demo-api 的都被误报。
    # 改成按"凭证族"判断：只有凭证名明显属于另一个系统时才报。
    # 这也是为什么这条是 info——启发式必然有误报，不能给高严重度。
    for job in facts.jobs:
        for e in job.extracts:
            cred_family = _credential_family(e.credentials)
            conn_family = _credential_family(e.connector)
            if cred_family and conn_family and cred_family != conn_family:
                out.append(
                    Finding(
                        code="credential_mismatch",
                        severity="info",
                        title=f"{e.alias} 的凭证与 connector 可能不匹配",
                        detail=(
                            f"connector={e.connector}（{conn_family}），"
                            f"credentials={e.credentials}（{cred_family}）。"
                            "凭证可能是共享别名，需 owner 确认。"
                        ),
                    )
                )

    # 6. 大 job 无分区控制
    for job in facts.jobs:
        if len(job.transforms) > 50 and job.load and job.load.partitions is None:
            out.append(
                Finding(
                    code="no_partition_control",
                    severity="check",
                    title=f"{job.job_type} 有 {len(job.transforms)} 步 transform 但未设置 numPartitions",
                    detail="复杂 job 建议显式控制分区数，避免 shuffle 失控。",
                )
            )
    return out


def build_review_sheet(findings: list[Finding]) -> Sheet:
    s = Sheet(
        "Review Notes",
        ["#", "Severity", "Code", "Title", "Detail", "Rule"],
        widths=[5, 10, 22, 40, 80, 40],
    )
    order = {"high": 0, "check": 1, "info": 2}
    for i, f in enumerate(sorted(findings, key=lambda x: order.get(x.severity, 9)), 1):
        s.add(i, f.severity, f.code, f.title, f.detail, f.rule_id or "-")
    if not findings:
        s.add("-", "-", "-", "未发现问题", "-", "-")
    return s


def landing_columns(job: JobSpec) -> list[Column]:
    return list(job.load.schema) if job.load else []
