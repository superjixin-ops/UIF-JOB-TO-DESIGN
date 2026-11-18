"""评测打分：加权 + 校准。

两个容易被忽略的点：

  1. 指标不能等权。
     "结构对但内容编造" 比 "格式有点丑但事实全对" 危险得多。
     所以 grounding 权重必须高于 structural。

  2. 分数必须可校准。
     如果系统说 0.9，而人工抽检只有 0.7，那这个 0.9 就是假的。
     做法：留一批人工标注样本，算"系统分 vs 人工分"的差距。

这套东西在面试里叫 eval harness / scoring rubric，公司里是必备的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 默认权重。可在 CLI 用 --weights 覆盖。
DEFAULT_WEIGHTS = {
    "structural": 0.15,   # 结构完整：缺 sheet 就废了，但修起来便宜
    "contract": 0.25,     # 渲染与 IR 一致、无注入：错了下游会崩
    "detection": 0.20,    # 该发现的缺陷发现了吗
    "grounding": 0.30,    # 结论能不能被事实支持 —— 最高权重
    "stability": 0.10,    # 同输入同输出
}


@dataclass
class Metrics:
    structural: float = 0.0
    contract: float = 0.0
    detection: float = 0.0
    grounding: float = 1.0
    stability: float = 1.0
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "structural": self.structural,
            "contract": self.contract,
            "detection": self.detection,
            "grounding": self.grounding,
            "stability": self.stability,
            **self.extra,
        }


def parse_weights(raw: str | None) -> dict[str, float]:
    """解析 "structural=0.2,grounding=0.4" 这种字符串。未指定的沿用默认。"""
    weights = dict(DEFAULT_WEIGHTS)
    if not raw:
        return weights
    for part in raw.split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key not in weights:
            raise ValueError(f"未知指标: {key}，可选 {list(weights)}")
        weights[key] = float(value)
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("权重之和必须为正")
    # 归一化，避免用户写了 1,1,1 之后总分超过 1
    return {k: round(v / total, 4) for k, v in weights.items()}


def weighted_score(metrics: Metrics, weights: dict[str, float] | None = None) -> float:
    w = weights or DEFAULT_WEIGHTS
    values = metrics.as_dict()
    return round(sum(values.get(k, 0.0) * v for k, v in w.items()), 4)


def calibrate(system_scores: list[float], human_scores: list[float]) -> dict:
    """系统分 vs 人工分。

    gap 为正 = 系统过于自信（危险）
    gap 为负 = 系统过于保守（浪费人工）
    """
    if len(system_scores) != len(human_scores) or not system_scores:
        raise ValueError("两组分数长度必须一致且非空")
    n = len(system_scores)
    gaps = [s - h for s, h in zip(system_scores, human_scores, strict=True)]
    mae = sum(abs(g) for g in gaps) / n
    bias = sum(gaps) / n
    return {
        "n": n,
        "mae": round(mae, 4),
        "bias": round(bias, 4),
        "system_mean": round(sum(system_scores) / n, 4),
        "human_mean": round(sum(human_scores) / n, 4),
        "verdict": "过于自信" if bias > 0.1 else ("过于保守" if bias < -0.1 else "基本校准"),
    }
