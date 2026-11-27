"""变换字典：语料（用法）+ 官方文档（语义）+ schema（合法性）。

为什么需要三方，而不是任意一方：

    语料  告诉你"大家怎么写"——频率、形状、真实例子
    文档  告诉你"默认值是什么、哪个必填"
    schema 告诉你"什么合法、有哪些枚举"

    只信语料：看不到默认值。`Sort.ascendingOrder` 不写就是降序，
    语料里 47% 的人显式写，看起来"可省"，省了就排反。
    只信文档：`Join` 文档要求 `joinMapping/left/right`，
    而 12,932 条真实流水线里 0% 这么写，100% 用文档没提的 `mapColumns`。
    只信 schema：schema 只约束类型，不告诉你哪种写法常见。

所以每个参数都有一个 trust（该信谁）：

    required        schema 必填，或文档必填且语料 ≥50%
    conventional    文档说可选，但语料 ≥95% 都写
    undocumented    文档没写，但语料 ≥95% 都写
    conflict        文档说必填，语料 <50%（版本差，要报警）
    deprecated      文档有，语料 0%（废弃或新版）
    optional        其余

这不是"选一个信"，是**按参数分别标注证据强度**。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

UNIVERSAL_TRANSFORM = {"transformType", "objectNames", "aliasResult"}
UNIVERSAL_CONNECTOR = {"credentials", "connector"}

REQUIRED = "required"
CONVENTIONAL = "conventional"
UNDOCUMENTED = "undocumented"
CONFLICT = "conflict"
DEPRECATED = "deprecated"
OPTIONAL = "optional"

# 语料少于此数，频率不可信（一条 pipeline 写两次不代表惯例）。
MIN_SAMPLES = 10
# "总写"的阈值。留 5% 给特例，避免把 97% 的写法误判成 100%。
ALWAYS = 0.95
# "必填但少见"的阈值。
RARE = 0.5

PARAM_RE = re.compile(r"<strong>([A-Za-z_][A-Za-z0-9_]*)(\*?)</strong>\s*\(([^)]{0,90})\)")
DEFAULT_RE = re.compile(r"defaults?\s+to\s+([^,)]+)", re.IGNORECASE)
CODE_RE = re.compile(r"<code>([^<]+)</code>")
H2_RE = re.compile(r'<h2 id="([^"]+)">([^<]*)</h2>')
LI_RE = re.compile(r"<li>(.*?)</li>", re.DOTALL)
# 参数名必须是标识符。真实语料里有把 `true:` 写成键的畸形数据，
# 不过滤会得到一行叫 "True" 的参数。
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------- 文档
def parse_doc_html(text: str, page: str) -> dict[str, dict[str, Any]]:
    """把一页 MkDocs 文档拆成"一个变换/连接器一节"。

    一个参数一定在一个 <li> 里。按 <li> 取，不要按位置截取——
    实测按位置会把上一段说明里的 <code> 当成参数取值，
    抓出 `&gt;`、`W-MON` 这种噪声。
    """
    out: dict[str, dict[str, Any]] = {}
    parts = H2_RE.split(text)
    for i in range(1, len(parts) - 2, 3):
        anchor, title, body = parts[i], parts[i + 1].strip(), parts[i + 2]
        params: list[dict[str, Any]] = []
        for li in LI_RE.findall(body):
            m = PARAM_RE.search(li)
            if not m:
                continue
            name, star, paren = m.group(1), m.group(2), m.group(3)
            dm = DEFAULT_RE.search(paren)
            params.append(
                {
                    "name": name,
                    "required": bool(star),
                    "type": paren.split(",")[0].strip(),
                    "default": dm.group(1).strip() if dm else None,
                    "codes": CODE_RE.findall(li),
                }
            )
        if params:
            out[title] = {"page": page, "anchor": anchor, "params": params}
    return out


def load_docs(path: str | Path) -> dict[str, dict[str, Any]]:
    """读抓取阶段产出的 docs.json（{类型: {page, params}}）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 语料
def scan_corpus(root: str | Path, *, samples: int = 2) -> dict[str, Any]:
    """扫真实流水线，统计每个变换/连接器的参数出现率。

    只统计"键在不在"，不统计值——值是这条流水线独有的，
    统计出来也不能复用（AddColumns.value 43% 唯一）。
    """
    t_stats: dict[str, dict[str, Any]] = {}
    e_stats: dict[str, dict[str, Any]] = {}
    l_stats: dict[str, dict[str, Any]] = {}
    files = 0

    def bump(bucket: dict[str, dict[str, Any]], name: str, item: dict) -> None:
        card = bucket.setdefault(name, {"count": 0, "keys": {}, "samples": []})
        card["count"] += 1
        for k in item:
            card["keys"][str(k)] = card["keys"].get(str(k), 0) + 1
        if len(card["samples"]) < samples:
            card["samples"].append(
                {k: v for k, v in item.items() if k not in UNIVERSAL_TRANSFORM}
            )

    for p in Path(root).rglob("*.yaml"):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            # 一条坏文件不能拖垮整次扫描。真实语料里确实有解析不了的。
            continue
        if not isinstance(d, dict):
            continue
        jobs = (d.get("spec") or {}).get("jobs")
        if not isinstance(jobs, list):
            continue
        files += 1
        for j in jobs:
            if not isinstance(j, dict):
                continue
            for t in j.get("transform") or []:
                if isinstance(t, dict):
                    bump(t_stats, str(t.get("transformType")), t)
            for e in j.get("extract") or []:
                if not isinstance(e, dict):
                    continue
                for c in e.get("connections") or []:
                    if isinstance(c, dict):
                        bump(e_stats, str(c.get("connector") or "?"), c)
            load = j.get("load")
            if isinstance(load, dict):
                for c in load.get("connections") or []:
                    if isinstance(c, dict):
                        bump(l_stats, str(c.get("connector") or "?"), c)

    return {
        "files": files,
        "transforms": t_stats,
        "extracts": e_stats,
        "loads": l_stats,
    }


# ---------------------------------------------------------------- schema
def load_schema(path: str | Path) -> dict[str, Any]:
    """从 pipeline-schema.json 里取每种变换/连接器的必填参数。

    schema 用 90 个 `if transformType == X then {...}` 表达，
    每个分支的 `required` 才是真正的"必填"。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    defs = data.get("definitions") or {}

    def variants(node: dict, key: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for item in node.get("allOf") or []:
            props = ((item.get("if") or {}).get("properties") or {}).get(key) or {}
            pattern = props.get("pattern")
            if not pattern:
                continue
            name = str(pattern).strip("^()$")
            then = item.get("then") or {}
            out[name] = {
                "required": list(then.get("required") or []),
                "props": list(then.get("properties") or {}),
            }
        return out

    return {
        "transforms": variants(defs.get("transformItem") or {}, "transformType"),
        "connectors": variants(defs.get("extractConnectorParameters") or {}, "connector"),
        "connector_enum": (
            ((defs.get("extractConnection") or {}).get("properties") or {})
            .get("connector", {})
            .get("anyOf", [{}])[0]
            .get("enum", [])
        ),
    }


# ---------------------------------------------------------------- 信任规则
def trust_of(
    *,
    doc_present: bool,
    doc_required: bool,
    schema_required: bool,
    rate: float,
    count: int,
) -> str:
    """一个参数该信谁。规则顺序不能换：越具体的证据越先判。"""
    if schema_required:
        return REQUIRED
    if doc_required:
        return REQUIRED if rate >= RARE else CONFLICT
    if not doc_present and rate >= ALWAYS and count >= MIN_SAMPLES:
        return UNDOCUMENTED
    if doc_present and rate >= ALWAYS and count >= MIN_SAMPLES:
        return CONVENTIONAL
    if doc_present and rate == 0 and count >= MIN_SAMPLES:
        return DEPRECATED
    return OPTIONAL


def _card(
    name: str,
    corpus: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    universal: set[str],
) -> dict[str, Any]:
    corpus = corpus or {"count": 0, "keys": {}, "samples": []}
    doc_params = {p["name"]: p for p in (doc or {}).get("params", [])}
    schema_required = set((schema or {}).get("required") or [])
    count = int(corpus.get("count") or 0)

    params: dict[str, dict[str, Any]] = {}
    names = {
        n
        for n in set(doc_params) | set(corpus.get("keys") or {}) | schema_required - universal
        if IDENT_RE.match(str(n))
    }
    for pname in sorted(names):
        hit = int((corpus.get("keys") or {}).get(pname, 0))
        rate = hit / count if count else 0.0
        d = doc_params.get(pname)
        params[pname] = {
            "doc_required": bool(d and d["required"]),
            "schema_required": pname in schema_required,
            "rate": round(rate, 4),
            "count": hit,
            "default": (d or {}).get("default"),
            "trust": trust_of(
                doc_present=bool(d),
                doc_required=bool(d and d["required"]),
                schema_required=pname in schema_required,
                rate=rate,
                count=count,
            ),
        }

    flags = [
        f"{p['trust']}:{k}"
        for k, p in params.items()
        if p["trust"] in {CONFLICT, UNDOCUMENTED, DEPRECATED}
    ]
    return {
        "count": count,
        "doc_page": (doc or {}).get("page", ""),
        "params": params,
        "flags": flags,
        "samples": corpus.get("samples") or [],
    }


def build(
    corpus: dict[str, Any],
    docs: dict[str, dict[str, Any]],
    schema: dict[str, Any],
    *,
    version: int = 1,
) -> dict[str, Any]:
    """合成字典。语料是主语，文档和 schema 是两票修正。"""
    t_docs = {k: v for k, v in docs.items() if k in schema["transforms"] or k in corpus["transforms"]}
    c_docs = {k: v for k, v in docs.items() if k in schema["connectors"] or k in corpus["extracts"]}

    transforms = {}
    for name in sorted(set(corpus["transforms"]) | set(t_docs) | set(schema["transforms"])):
        transforms[name] = _card(
            name,
            corpus["transforms"].get(name),
            t_docs.get(name),
            schema["transforms"].get(name),
            UNIVERSAL_TRANSFORM,
        )

    connectors = {}
    for name in sorted(set(corpus["extracts"]) | set(c_docs) | set(schema["connectors"])):
        connectors[name] = _card(
            name,
            corpus["extracts"].get(name),
            c_docs.get(name),
            schema["connectors"].get(name),
            UNIVERSAL_CONNECTOR,
        )

    return {
        "version": version,
        "source": {
            "pipelines": corpus.get("files", 0),
            "transforms": sum(c["count"] for c in corpus["transforms"].values()),
            "connector_enum": schema.get("connector_enum", []),
        },
        "transforms": transforms,
        "connectors": connectors,
    }


# ---------------------------------------------------------------- 校验
def check(facts: Any, catalog: dict[str, Any]) -> list[Issue]:
    """拿字典检查一条 pipeline：缺必填、写了没见过的参数、用了没见过的类型。"""
    issues: list[Issue] = []
    types = catalog.get("transforms") or {}
    conns = catalog.get("connectors") or {}

    for job in facts.jobs:
        for t in job.transforms:
            card = types.get(t.transform_type)
            if card is None:
                issues.append(
                    Issue(
                        "C_UNKNOWN_TYPE",
                        f"{job.job_type}: 变换 {t.transform_type} 不在字典里",
                        "确认拼写；新类型先跑 catalog build",
                    )
                )
                continue
            present = set(t.detail)
            # SparkSql 的 SQL 存在 t.sql 里，不在 detail 里。
            # 不补这一行，每个 SparkSql 都会被误报"缺 query"。
            if t.sql is not None:
                present.add("query")
            for pname, meta in card["params"].items():
                if meta["trust"] == REQUIRED and pname not in present:
                    issues.append(
                        Issue(
                            "C_MISSING_PARAM",
                            f"{job.job_type}: {t.transform_type} 缺必填参数 {pname}",
                            f"补上 {pname}",
                        )
                    )
                if meta["trust"] == CONFLICT and pname not in present:
                    issues.append(
                        Issue(
                            "C_CONFLICT_PARAM",
                            f"{job.job_type}: {t.transform_type}.{pname} 文档说必填、"
                            f"语料 {meta['rate']:.0%} 才写",
                            "确认平台版本后再决定写不写",
                        )
                    )
                if pname not in present:
                    continue
            for pname in present:
                if pname not in card["params"]:
                    issues.append(
                        Issue(
                            "C_UNKNOWN_PARAM",
                            f"{job.job_type}: {t.transform_type} 出现字典外的参数 {pname}",
                            "确认拼写；如果是新参数，先更新字典",
                        )
                    )

        for e in job.extracts:
            if e.connector in ("", "unknown"):
                continue
            card = conns.get(e.connector)
            if card is None:
                issues.append(
                    Issue(
                        "C_UNKNOWN_CONNECTOR",
                        f"{job.job_type}: 连接器 {e.connector} 不在字典里",
                        "确认拼写；新连接器先跑 catalog build",
                    )
                )
    return issues


# ---------------------------------------------------------------- 读写
def write(catalog: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
