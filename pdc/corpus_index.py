"""把 pipeline 投影成"结构卡"，再拿去做语义检索。

为什么不是直接把 YAML 喂给 embedding：

    抽样 300 条实测，58% 的 token 是结构键和标点（jobType / connections /
    schema / {} / :）。直接 embed 原文，向量会按"格式"聚类，
    两条业务完全无关的 pipeline 余弦相似度也有 0.92。

    所以先做一个确定性的投影：job 链、连接器、目标层、变换类型、列名。
    这一步是特征工程，不是模型能解决的。

卡片长这样（一行）：

    name: fact-order-fulfillment
    kind: FactPipeline
    chain: eid > datamart-parquet > datamart-self-serve
    sources: cos, skapi
    targets: cos
    transforms: Select, FilterData, RenameColumns, JoinByExpression, Drop
    columns: ORDER_FULFILLMENT_KEY, ORDER_LINE_NUMBER, ORDER_ID, ...
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pdc.parser import PipelineParseError, parse_pipeline

MAX_COLUMNS = 12


@dataclass(frozen=True)
class Card:
    id: str
    text: str
    chain: str
    kind: str
    path: str
    ncols: int
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "chain": self.chain,
            "kind": self.kind,
            "path": self.path,
            "ncols": self.ncols,
        }


def card_from(path: Path, root: Path) -> Card | None:
    try:
        facts = parse_pipeline(path)
    except (PipelineParseError, OSError, UnicodeDecodeError):
        return None

    chain = " > ".join(j.job_type for j in facts.jobs)
    sources = sorted({e.connector for j in facts.jobs for e in j.extracts})
    targets = sorted({j.load.connector for j in facts.jobs if j.load and j.load.connector})
    transforms: list[str] = []
    for job in facts.jobs:
        for t in job.transforms:
            if t.transform_type not in transforms:
                transforms.append(t.transform_type)
    columns: list[str] = []
    for job in facts.jobs:
        if not job.load:
            continue
        for col in job.load.schema:
            if col.name not in columns:
                columns.append(col.name)

    text = "\n".join(
        [
            f"name: {facts.name}",
            f"kind: {facts.kind}",
            f"chain: {chain}",
            f"sources: {', '.join(sources) or '-'}",
            f"targets: {', '.join(targets) or '-'}",
            f"transforms: {', '.join(transforms) or '-'}",
            f"columns: {', '.join(columns[:MAX_COLUMNS]) or '-'}",
        ]
    )
    return Card(
        id=facts.name,
        text=text,
        chain=chain,
        kind=facts.kind,
        path=str(path.relative_to(root)),
        ncols=len(columns),
        meta={"chain": chain, "kind": facts.kind, "path": str(path.relative_to(root))},
    )


def build_cards(repo: str | Path, limit: int | None = None) -> list[Card]:
    root = Path(repo)
    cards: list[Card] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*.yaml")):
        card = card_from(path, root)
        if card is None:
            continue
        if card.id in seen:  # 同名文件取第一个，避免 id 冲突
            continue
        seen.add(card.id)
        cards.append(card)
        if limit and len(cards) >= limit:
            break
    return cards


def write_cards(cards: list[Card], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(c.to_dict(), ensure_ascii=False) for c in cards) + "\n",
        encoding="utf-8",
    )
    return out


def read_cards(path: str | Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
