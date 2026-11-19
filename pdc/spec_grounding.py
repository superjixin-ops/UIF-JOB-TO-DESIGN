"""生成方向的幻觉检测：拿设计文档去核对生成的 YAML。

正向（design.py）时，事实来自 YAML，文档是模型写的，所以 ground() 核对文档。
反向（本文件）时，事实来自设计文档，YAML 是模型写的，所以核对 YAML。

**两者必须是两套代码。** 用同一套检查会发现"文档和 YAML 一致但都错了"
——那是自证，不是验证。

检查分两类，严重度不同，这个区分是整个模块的重点：

    漏（omission）  文档要求了、YAML 里没有 → 静默失败，最危险
    编（fabrication）YAML 里有、文档没要求 → 响亮失败，容易被看见

真实事故里，漏比编多得多，也更难发现：编出来的列一跑就报错，
漏掉的列会安静地少一列数据，三个月后对账才发现。

所以：**漏 = error，编 = error，类型不一致 = error，缺 schema = error，
但"文档里没有、YAML 里补了个合理默认值"= warning。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pdc.design_reader import DesignSpec
from pdc.models import PipelineFacts


@dataclass
class SpecViolation:
    code: str
    message: str
    severity: str = "error"  # error 直接判失败 / warning 记录但放行
    where: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "where": self.where,
        }

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


def _cols(facts: PipelineFacts, job: str) -> dict[str, str]:
    j = facts.job(job)
    if j is None or j.load is None:
        return {}
    return {c.name: c.data_type for c in j.load.schema}


def check(spec: DesignSpec, facts: PipelineFacts) -> list[SpecViolation]:
    """全量对齐检查。纯函数，无 IO，可单测。"""
    out: list[SpecViolation] = []

    spec_jobs = [j for j in spec.jobs]
    yaml_jobs = [j.job_type for j in facts.jobs]

    # ---------- job ----------
    for job in spec_jobs:
        if job not in yaml_jobs:
            out.append(SpecViolation("S_JOB_MISSING", f"文档要求 job {job}，YAML 里没有", where=job))
    for job in yaml_jobs:
        if job not in spec_jobs:
            out.append(
                SpecViolation(
                    "S_JOB_EXTRA",
                    f"YAML 多出 job {job}，文档里没有（不要自创 job 类型）",
                    where=job,
                )
            )

    # 顺序也是契约：landing -> eid -> datamart 的顺序错了，跑起来会找不到上游
    common = [j for j in spec_jobs if j in yaml_jobs]
    if common != [j for j in yaml_jobs if j in spec_jobs]:
        out.append(
            SpecViolation(
                "S_JOB_ORDER",
                f"job 顺序与文档不一致：文档 {common}，YAML "
                f"{[j for j in yaml_jobs if j in spec_jobs]}",
            )
        )

    # ---------- 容量档位 ----------
    # 这一项原来完全没比。模型把 M 写成 T，校验器一声不吭——
    # size 决定 Spark 分片和成本，写错不报错，但会多花钱或跑不动。
    for job, size in spec.job_sizes:
        j = facts.job(job)
        if j is None:
            continue
        if j.size != size:
            out.append(
                SpecViolation(
                    "S_SIZE",
                    f"{job}: setups.size 是 {j.size!r}，文档要求 {size!r}",
                    where=job,
                )
            )

    # ---------- 列契约 ----------
    for job in common:
        want = {c.name: c for c in spec.columns_of(job)}
        got = _cols(facts, job)
        if not want:
            continue
        if not got:
            out.append(
                SpecViolation(
                    "S_SCHEMA_MISSING",
                    f"文档给了 {job} 的 {len(want)} 列契约，YAML 的 load 没有 schema",
                    where=job,
                )
            )
            continue
        job_facts = facts.job(job)
        yaml_cols = {c.name: c for c in job_facts.load.schema} if job_facts and job_facts.load else {}

        for name, col in want.items():
            if name not in got:
                out.append(
                    SpecViolation(
                        "S_COL_MISSING",
                        f"{job}: 文档要求列 {name}，YAML 里没有（漏列 = 静默丢数据）",
                        where=job,
                    )
                )
                continue
            if got[name].lower() != col.data_type.lower():
                out.append(
                    SpecViolation(
                        "S_COL_TYPE",
                        f"{job}.{name}: 类型是 {got[name]}，文档要求 {col.data_type}",
                        where=job,
                    )
                )
            # nullable 和精度也是契约。之前只比名字和类型，
            # 模型把 nullAble: false 写成 nullable: false，字段被解析器忽略，
            # 校验器却判了通过——**YAML 能解析，但可空性变了**。
            ycol = yaml_cols.get(name)
            if ycol is not None and bool(ycol.nullable) != bool(col.nullable):
                out.append(
                    SpecViolation(
                        "S_COL_NULLABLE",
                        f"{job}.{name}: nullAble 是 {ycol.nullable}，文档要求 {col.nullable}",
                        where=job,
                    )
                )
            if (
                ycol is not None
                and col.precision_num is not None
                and (ycol.precision, ycol.scale) != (col.precision_num, col.scale_num)
            ):
                out.append(
                    SpecViolation(
                        "S_COL_PRECISION",
                        f"{job}.{name}: dataTypeArgs 是 "
                        f"({ycol.precision},{ycol.scale})，文档要求 "
                        f"({col.precision_num},{col.scale_num})",
                        where=job,
                    )
                )
        for name in got:
            if name not in want:
                out.append(
                    SpecViolation(
                        "S_COL_EXTRA",
                        f"{job}: YAML 多出列 {name}，文档里没有（不要发明列名）",
                        where=job,
                    )
                )

        # 分区列必须真的在 schema 里
        load = facts.job(job).load  # type: ignore[union-attr]
        for pcol in load.partition_by:
            if pcol not in want:
                out.append(
                    SpecViolation(
                        "S_PARTITION",
                        f"{job}: load.connections[].partitionBy 用了 {pcol}，"
                        f"但文档的列契约里没有这一列",
                        where=job,
                    )
                )

    # ---------- extract ----------
    spec_ex = {(e.job, e.alias) for e in spec.extracts}
    yaml_ex = {(j.job_type, e.alias) for j in facts.jobs for e in j.extracts}
    for job, alias in sorted(spec_ex - yaml_ex):
        out.append(
            SpecViolation("S_EXTRACT_MISSING", f"{job}: 文档要求数据源 {alias}，YAML 里没有", where=job)
        )
    for job, alias in sorted(yaml_ex - spec_ex):
        out.append(
            SpecViolation("S_EXTRACT_EXTRA", f"{job}: YAML 多出数据源 {alias}，文档里没有", where=job)
        )

    # 连接级参数：这些字段原来根本不进文档，所以产物丢掉它们也能判满分。
    # 实测一条 30 列的流水线丢了 14 个键值（conn alias / keyName / requestFlags），
    # 报告里却写着 grounding=1.0——**校验器看不见文档没有的东西**。
    yaml_extracts = {
        (j.job_type, e.alias): e for j in facts.jobs for e in j.extracts
    }
    for want in spec.extracts:
        got = yaml_extracts.get((want.job, want.alias))
        if got is None:
            continue  # S_EXTRACT_MISSING 已报
        wrong: list[str] = []
        if want.extract_type and want.extract_type != got.extract_type:
            wrong.append(f"extractType: {got.extract_type!r} != {want.extract_type!r}")
        if want.conn_alias and want.conn_alias != got.conn_alias:
            wrong.append(f"connections[].alias: {got.conn_alias!r} != {want.conn_alias!r}")
        if want.key_name and want.key_name != got.key_name:
            wrong.append(f"connections[].keyName: {got.key_name!r} != {want.key_name!r}")
        if want.flags and _norm_param(dict(want.flags)) != _norm_param(dict(got.flags)):
            wrong.append(
                f"connections[].requestFlags: {dict(got.flags)} != {dict(want.flags)}"
            )
        if wrong:
            out.append(
                SpecViolation(
                    "S_EXTRACT_PARAMS",
                    f"{want.job}.{want.alias}: " + "；".join(wrong),
                    where=want.job,
                )
            )

    # main_df 有且只有一个，且必须是文档指的那个
    for job in common:
        j = facts.job(job)
        wanted_main = {e.alias for e in spec.extracts if e.job == job and e.main_df}
        got_main = {e.alias for e in j.extracts if e.main_df} if j else set()
        if wanted_main and wanted_main != got_main:
            out.append(
                SpecViolation(
                    "S_MAIN_DF",
                    f"{job}: main_df 应为 {sorted(wanted_main)}，实际 {sorted(got_main) or '无'}",
                    where=job,
                )
            )

    # 凭证必须是文档里那几个，不能自己编一个
    allowed_creds = {e.credentials for e in spec.extracts if e.credentials}
    for j in facts.jobs:
        for e in j.extracts:
            if allowed_creds and e.credentials not in allowed_creds:
                out.append(
                    SpecViolation(
                        "S_CREDENTIAL",
                        f"{j.job_type}.{e.alias}: 凭证 {e.credentials!r} 不在文档里"
                        f"（可选：{sorted(allowed_creds)}）",
                        where=j.job_type,
                    )
                )

    # ---------- transform ----------
    spec_tx = {(t.job, t.transform_type) for t in spec.transforms}
    yaml_tx = {(j.job_type, t.transform_type) for j in facts.jobs for t in j.transforms}
    for job, ttype in sorted(spec_tx - yaml_tx):
        out.append(
            SpecViolation(
                "S_TRANSFORM_MISSING",
                f"{job}: 文档要求变换 {ttype}，YAML 里没有（血缘会断）",
                where=job,
            )
        )
    # 参数级比对：光有 transformType 不算实现。
    # 真实的坑：模型写出了 `- transformType: RenameColumns`，但把 mapColumns 整段丢了，
    # 结果 schema 里引用的新列名在数据帧里根本不存在——YAML 能解析，跑起来必错。
    # 校验器当时只看 transformType，所以判了通过。
    for t in spec.transforms:
        j = facts.job(t.job)
        if j is None or not t.params:
            continue
        cands = [x for x in j.transforms if x.transform_type == t.transform_type]
        if not cands:
            continue  # 已经由 S_TRANSFORM_MISSING 报过
        # 同名变换可能有多步，任意一步匹配即可
        if any(_params_match(t.params, x.detail) for x in cands):
            continue
        missing = [
            k for k in t.params if not any(k in x.detail for x in cands)
        ]
        detail = f"缺参数 {missing}" if missing else "参数值不一致"
        out.append(
            SpecViolation(
                "S_TRANSFORM_PARAMS",
                f"{t.job}.{t.transform_type}: {detail}（语义丢失 = 能解析但跑不对）",
                where=t.job,
            )
        )

    for job, ttype in sorted(yaml_tx - spec_tx):
        out.append(
            SpecViolation(
                "S_TRANSFORM_EXTRA",
                f"{job}: YAML 多出变换 {ttype}，文档里没有",
                severity="warning",  # 多一步中间变换通常是合理实现细节
                where=job,
            )
        )

    # ---------- load 目标 ----------
    # 一个 job 可能写多张表（load.connections 多行），所以按 job 比集合，
    # 不按单行比——否则"写两张表"的合法情况会被判成写错目标。
    for job in common:
        j = facts.job(job)
        if j is None:
            continue
        if j.load is None:
            # 文档里有 load，YAML 里没有 —— 整段写入消失了。
            # 这个分支原来是 `continue`，于是"删掉整段 load"能被判通过：
            # 已经踩过一次，模型把 JDBC 那段连接删了，报告仍然是 grounding=1.0。
            if any(x.job == job for x in spec.loads):
                out.append(
                    SpecViolation(
                        "S_LOAD_MISSING",
                        f"{job}: 文档要求写 {sorted({x.object_name or x.query for x in spec.loads if x.job == job})}，"
                        "YAML 里整段 load 缺失",
                        where=job,
                    )
                )
            continue
        # loadObject 和 metadata.name 是两个键，必须分开比。
        # 混成一列时模型只能猜，实测它把 query 写成了 loadObject，
        # 而校验器因为"目标名看着对"放过了——**错的是文档，不是模型**。
        want_obj = {x.object_name for x in spec.loads if x.job == job and x.object_name}
        got_obj = {o.load_object for o in j.load.objects if o.load_object}
        if want_obj and want_obj != got_obj:
            out.append(
                SpecViolation(
                    "S_TARGET",
                    f"{job}: load.connections[].loadObject 应为 {sorted(want_obj)}，"
                    f"实际 {sorted(got_obj) or '缺失'}",
                    where=job,
                )
            )
        want_meta = {x.metadata_name for x in spec.loads if x.job == job and x.metadata_name}
        got_meta = {j.load.object_name} - {""}
        if want_meta and want_meta != got_meta:
            out.append(
                SpecViolation(
                    "S_METADATA_NAME",
                    f"{job}: load.metadata.name 应为 {sorted(want_meta)}，"
                    f"实际 {sorted(got_meta) or '缺失'}",
                    where=job,
                )
            )
        want_mode = {load.mode for load in spec.loads if load.job == job and load.mode}
        if want_mode and j.load.mode not in want_mode:
            out.append(
                SpecViolation(
                    "S_LOAD_MODE",
                    f"{job}: load.connections[0].mode 应为 {sorted(want_mode)}，"
                    f"当前是 {j.load.mode or '缺失'}（append/overwrite 写错会毁数据）",
                    where=job,
                )
            )
        # 连接级：并行度、别名、表空间。写错并行度不一定报错，但会改变写入行为。
        for spec_load in [x for x in spec.loads if x.job == job]:
            wrong: list[str] = []
            if spec_load.partitions is not None and spec_load.partitions != j.load.partitions:
                wrong.append(
                    f"connections[].numPartitions: {j.load.partitions} != {spec_load.partitions}"
                )
            if spec_load.conn_alias and spec_load.conn_alias != j.load.conn_alias:
                wrong.append(
                    f"connections[].alias: {j.load.conn_alias!r} != {spec_load.conn_alias!r}"
                )
            if spec_load.table_space and spec_load.table_space != j.load.table_space:
                wrong.append(
                    f"connections[].tableSpace: {j.load.table_space!r} != {spec_load.table_space!r}"
                )
            if spec_load.connector and spec_load.connector != j.load.connector:
                wrong.append(
                    f"connections[].connector: {j.load.connector!r} != {spec_load.connector!r}"
                )
            if spec_load.credentials and spec_load.credentials != j.load.credentials:
                wrong.append(
                    f"connections[].credentials: {j.load.credentials!r} != "
                    f"{spec_load.credentials!r}（写错 connector 会写到别的系统）"
                )
            if spec_load.flip_alias != j.load.flip_alias:
                wrong.append(
                    f"connections[].flipAlias: {j.load.flip_alias} != {spec_load.flip_alias}"
                )
            if wrong:
                out.append(
                    SpecViolation(
                        "S_LOAD_PARAMS", f"{job}: " + "；".join(wrong), where=job
                    )
                )

        want_pby = {tuple(load.partition_by) for load in spec.loads if load.job == job}
        if want_pby and tuple(j.load.partition_by) not in want_pby:
            out.append(
                SpecViolation(
                    "S_PARTITION_BY",
                    f"{job}: 在 load.connections[0] 下写 partitionBy: "
                    f"{list(next(iter(sorted(want_pby))))}，"
                    f"当前是 {list(j.load.partition_by) or '缺失'}",
                    where=job,
                )
            )

    return out


def _norm_param(value: object) -> object:
    """把 YAML 里读出来的值归一化后比较。

    不归一化会出现"看起来一样但比不过"：YAML 的 `1` 和 `"1"`、
    列表顺序、嵌套 dict 的键序，都会造成假阳性。
    """
    if isinstance(value, dict):
        return {str(k): _norm_param(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_norm_param(v) for v in value]
    if isinstance(value, bool):
        return value
    return str(value).strip()


def _params_match(want: dict, got: dict) -> bool:
    for k, v in want.items():
        if k not in got:
            return False
        if _norm_param(got[k]) != _norm_param(v):
            return False
    return True


def blocking(violations: list[SpecViolation]) -> list[SpecViolation]:
    return [v for v in violations if v.severity == "error"]


def score(violations: list[SpecViolation], total_items: int) -> float:
    """对齐率：1 - 阻断问题 / 应生成项数。

    分母用"文档要求生成的项数"而不是"YAML 里的项数"，
    否则漏得越多、分数越好看——这正是漏检最难发现的地方。
    """
    if total_items <= 0:
        return 1.0
    bad = len(blocking(violations))
    return max(0.0, 1.0 - bad / total_items)


def expected_items(spec: DesignSpec) -> int:
    n = len(spec.jobs)
    n += len(spec.extracts)
    n += len(spec.transforms)
    n += sum(len(c) for c in (load.columns for load in spec.loads))
    n += sum(1 for load in spec.loads if load.object_name)
    return max(n, 1)


# ---------------------------------------------------------------------------
# 自由文本输入：弱契约，但能抓"编造"
# ---------------------------------------------------------------------------
# 结构化文档能逐字比对列契约；Confluence 导出的一页 md 不能。
# 这时退到一条更弱、但同样确定性的规则：
#   **生成的 YAML 里出现的每个实体标识符，必须在原文里字面出现过。**
#
# 这条规则抓不到"漏列"（原文本身就没列全），但能抓住最危险的一类编造：
# 模型补了一个看起来合理、但需求里根本没有的列名或连接器。
# 它也是可解释的：命中时直接给出"这个词在原文里找不到"，没有争议。

IDENT_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
CONNECTOR_WORDS = {"cos", "skapi", "jdbc", "jdbc_et", "erdm_api"}


def identifiers_in(text: str) -> set[str]:
    return set(IDENT_RE.findall(text or ""))


def check_text(text: str, facts: PipelineFacts) -> list[SpecViolation]:
    """自由文本版事实对齐：实体必须能在原文里找到。"""
    out: list[SpecViolation] = []
    haystack = (text or "").lower()
    allowed = identifiers_in(text)

    for job in facts.jobs:
        for col in (job.load.schema if job.load else ()):
            if col.name not in allowed and col.name.lower() not in haystack:
                out.append(
                    SpecViolation(
                        "T_COL_FABRICATED",
                        f"{job.job_type}: 列 {col.name} 在需求原文里找不到（疑似编造）",
                        where=job.job_type,
                    )
                )
        for e in job.extracts:
            if e.alias not in allowed and e.alias.lower() not in haystack:
                out.append(
                    SpecViolation(
                        "T_ALIAS_FABRICATED",
                        f"{job.job_type}: 数据源别名 {e.alias} 在需求原文里找不到",
                        where=job.job_type,
                    )
                )
            if e.connector.lower() not in haystack and e.connector.lower() not in CONNECTOR_WORDS:
                out.append(
                    SpecViolation(
                        "T_CONNECTOR_FABRICATED",
                        f"{job.job_type}: 连接器 {e.connector} 在需求原文里找不到",
                        where=job.job_type,
                    )
                )
    return out


def text_item_count(text: str, facts: PipelineFacts) -> int:
    """分母取"原文提到的实体数"和"产出实体数"的较大值。

    只取产出数会让"编得越多分越高"；只取原文数又会让"原文很短"时除零。
    """
    produced = sum(
        len(j.load.schema) for j in facts.jobs if j.load
    ) + sum(len(j.extracts) for j in facts.jobs)
    return max(len(identifiers_in(text)), produced, 1)
