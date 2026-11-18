"""API 安全层：鉴权、限流、成本护栏。

三个都是"公开部署"的必需品，缺一个就等于把服务送人：

    鉴权   没有它，任何人拿到链接就能用你的模型额度
    限流   没有它，一个人能把并发槽占满，其他人全部 429
    预算   没有它，一个跑飞的循环能烧掉一天的钱

设计取向：**默认安全**。
没配 token 时只允许本机访问，而不是"没配就放行"。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

TOKEN_ENV = "PDC_API_TOKEN"
RATE_ENV = "PDC_RATE_PER_MIN"
BUDGET_ENV = "PDC_RUN_TOKEN_BUDGET"


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


async def require_token(request: Request) -> None:
    """FastAPI 依赖：校验 Bearer token。

    三种情况：
        配了 PDC_API_TOKEN     → 必须带对的 token
        没配 + 本机访问        → 放行（本地开发）
        没配 + 外部访问        → 401，并说明原因
    """
    expected = os.getenv(TOKEN_ENV, "").strip()
    header = request.headers.get("authorization", "")
    provided = header[7:].strip() if header.lower().startswith("bearer ") else ""

    if not expected:
        if _is_loopback(request):
            return
        raise HTTPException(
            status_code=401,
            detail=f"服务未配置 {TOKEN_ENV}，拒绝外部访问",
        )

    if provided != expected:
        raise HTTPException(status_code=401, detail="token 无效")


# ---------------------------------------------------------------------------
# 按客户端限流：令牌桶
# ---------------------------------------------------------------------------
@dataclass
class Bucket:
    capacity: int
    refill_per_sec: float
    tokens: float
    updated: float

    def take(self, now: float) -> bool:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@dataclass
class RateLimiter:
    """按 IP 的令牌桶。

    为什么用令牌桶而不是固定窗口：
        固定窗口在边界会双倍放行（59 秒 10 个 + 61 秒 10 个）。
        令牌桶平滑，且能限制突发。
    """

    per_min: int = 6
    burst: int = 2
    buckets: dict[str, Bucket] = field(default_factory=dict)

    def check(self, client: str) -> tuple[bool, float]:
        now = time.monotonic()
        bucket = self.buckets.get(client)
        if bucket is None:
            bucket = Bucket(
                capacity=self.burst,
                refill_per_sec=self.per_min / 60.0,
                tokens=float(self.burst),
                updated=now,
            )
            self.buckets[client] = bucket
        if bucket.take(now):
            return True, 0.0
        wait = (1 - bucket.tokens) / bucket.refill_per_sec if bucket.refill_per_sec else 60.0
        return False, round(wait, 1)

    def reset(self) -> None:
        self.buckets.clear()


def rate_limit_dependency(limiter: RateLimiter):
    async def dependency(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        ok, wait = limiter.check(client)
        if not ok:
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，请 {wait}s 后重试",
                headers={"Retry-After": str(int(wait) + 1)},
            )

    return dependency


# ---------------------------------------------------------------------------
# 成本护栏
# ---------------------------------------------------------------------------
def token_budget() -> int:
    """单次运行的 token 上限。0 表示不限制。"""
    try:
        return int(os.getenv(BUDGET_ENV, "120000"))
    except ValueError:
        return 120_000
