"""配置校验：启动时把"错的配置"变成"启动失败"。

企业里最常见的线上事故不是代码 bug，是配置错：

    端口写成字符串      -> 服务起不来，但报错在三十行之后
    并发设成 0          -> 所有请求 429，看起来像限流
    输出目录不可写      -> 跑完才失败，钱已经花了
    token 格式写错      -> 鉴权全部 401，排查半天

这些错误有一个共同点：**它们都能在启动时发现**。
所以这里的原则是：**fail fast，一次报全部问题，而不是逐个撞。**

为什么不直接用 pydantic Settings：
    多一个依赖，而且 pydantic 的错误信息对运维不友好
    （"value is not a valid integer" 不告诉你该改成什么）。
    这里手写 60 行，错误信息可以直接写"应该改成什么"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROLES = {"viewer", "operator", "admin"}


class ConfigError(RuntimeError):
    """配置有问题。消息里必须包含"怎么改"。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("配置有问题：\n" + "\n".join(f"  - {p}" for p in problems))


def _int(name: str, default: int, problems: list[str], *, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name}={raw!r} 不是整数，应该写成 {default} 这样的数字")
        return default
    if not (low <= value <= high):
        problems.append(f"{name}={value} 超出范围，应该在 {low}~{high} 之间")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip() not in {"0", "false", "False", ""}


@dataclass(frozen=True)
class Settings:
    port: int = 8000
    max_concurrency: int = 4
    rate_per_min: int = 6
    token_budget: int = 120_000
    out_dir: Path = field(default_factory=lambda: Path("out"))
    rag_mode: str = "hybrid"
    rerank: bool = True
    tokens: dict[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> Settings:
        problems: list[str] = []

        port = _int("PDC_PORT", 8000, problems, low=1, high=65535)
        concurrency = _int("PDC_MAX_CONCURRENCY", 4, problems, low=1, high=64)
        rate = _int("PDC_RATE_PER_MIN", 6, problems, low=1, high=10_000)
        budget = _int("PDC_RUN_TOKEN_BUDGET", 120_000, problems, low=0, high=10_000_000)

        out_raw = os.getenv("PDC_OUT_DIR", "out")
        out = Path(out_raw)
        try:
            out.mkdir(parents=True, exist_ok=True)
            probe = out / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"PDC_OUT_DIR={out_raw!r} 不可写（{exc}），换一个目录或修权限")

        rag = os.getenv("PDC_RAG_MODE", "hybrid").strip().lower()
        if rag not in {"bm25", "hybrid"}:
            problems.append(f"PDC_RAG_MODE={rag!r} 不认识，只能是 bm25 或 hybrid")

        tokens, token_problems = parse_tokens(os.getenv("PDC_TOKENS", ""))
        problems.extend(token_problems)

        return cls(
            port=port,
            max_concurrency=concurrency,
            rate_per_min=rate,
            token_budget=budget,
            out_dir=out,
            rag_mode=rag,
            rerank=_bool("PDC_RERANK", True),
            tokens=tokens,
            problems=tuple(problems),
        )

    def validate(self) -> Settings:
        """有问题就抛，一次报全部。"""
        if self.problems:
            raise ConfigError(list(self.problems))
        return self

    def redacted(self) -> dict:
        """给日志和 /health 看的版本：token 只显示数量。"""
        return {
            "port": self.port,
            "max_concurrency": self.max_concurrency,
            "rate_per_min": self.rate_per_min,
            "token_budget": self.token_budget,
            "out_dir": str(self.out_dir),
            "rag_mode": self.rag_mode,
            "rerank": self.rerank,
            "tokens": {f"***{i}": role for i, role in enumerate(self.tokens.values())},
        }


def parse_tokens(raw: str) -> tuple[dict[str, str], list[str]]:
    """解析 PDC_TOKENS="token1:admin,token2:operator"。

    规则：
      - 每个 token 至少 16 位（太短容易被撞）
      - role 必须是 viewer / operator / admin
      - 重复 token 直接报错（否则你以为是两个身份，实际是一个）
    """
    tokens: dict[str, str] = {}
    problems: list[str] = []
    for item in (x.strip() for x in raw.split(",") if x.strip()):
        if ":" not in item:
            problems.append(f"PDC_TOKENS 里的 {item!r} 缺角色，应该写成 token:role")
            continue
        token, role = item.rsplit(":", 1)
        token, role = token.strip(), role.strip().lower()
        if role not in ROLES:
            problems.append(f"{item!r} 的角色 {role!r} 不认识，只能是 {'/'.join(sorted(ROLES))}")
            continue
        if len(token) < 16:
            problems.append(f"token {token[:4]}... 太短（{len(token)} 位），至少 16 位")
            continue
        if token in tokens:
            problems.append(f"token {token[:4]}... 重复了，每个身份一个 token")
            continue
        tokens[token] = role
    return tokens, problems
