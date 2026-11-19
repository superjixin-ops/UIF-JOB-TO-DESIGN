"""RBAC：三个角色，一条升级链，默认拒绝。

为什么不是"一个 token 走天下"：

    只读的人（看板的、审计的）不应该能触发跑批——
    跑一次就是真金白银，还可能写外部系统。
    企业里这一条通常写在合规要求里，不是"最好有"。

角色：

    viewer    只能看任务状态、trace、审计之外的东西
    operator  可以提交任务、取消任务
    admin     可以看审计日志、指标、所有运维接口

兼容：没有配 PDC_TOKENS 时——
    本机访问   -> admin（本地开发不折腾）
    外部访问   -> 401（默认安全，和原来一致）

为什么用"最小角色 + 层级比较"而不是逐接口列权限：
    接口会变，角色不会。接口只声明"最低需要什么角色"，
    加接口时不会漏配。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from pdc.config import parse_tokens

RANK = {"viewer": 1, "operator": 2, "admin": 3}
LEGACY_ENV = "PDC_API_TOKEN"


@dataclass(frozen=True)
class Principal:
    token_id: str  # token 的哈希前缀，永远不存明文
    role: str
    source: str  # "local" / "env"

    def can(self, need: str) -> bool:
        return RANK.get(self.role, 0) >= RANK[need]


def token_id(token: str) -> str:
    """审计日志里用它标识"谁"，不可逆。"""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _tokens() -> dict[str, str]:
    raw = os.getenv("PDC_TOKENS", "")
    tokens, _ = parse_tokens(raw)
    legacy = os.getenv(LEGACY_ENV, "").strip()
    if legacy:
        tokens.setdefault(legacy, "admin")
    return tokens


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def authenticate(request: Request, *, allow_query: bool = False) -> Principal:
    tokens = _tokens()
    header = request.headers.get("authorization", "")
    provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not provided and allow_query:
        # 浏览器 EventSource 不能自定义请求头，只能用 query 传 token。
        # 代价是 token 会进 access log——生产上应该改用短期签名 URL。
        provided = request.query_params.get("token", "").strip()

    if not tokens:
        if _is_loopback(request):
            return Principal(token_id="local", role="admin", source="local")
        raise HTTPException(status_code=401, detail="服务未配置 PDC_TOKENS，拒绝外部访问")

    for token, role in tokens.items():
        # compare_digest 防时序攻击：普通的 != 会在第一个不同字符处返回，
        # 攻击者能靠响应时间逐位猜出 token。
        if provided and secrets.compare_digest(provided, token):
            return Principal(token_id=token_id(token), role=role, source="env")

    raise HTTPException(status_code=401, detail="token 无效")


def require_role(need: str, *, allow_query: bool = False):
    """FastAPI 依赖工厂：声明这个接口最低要什么角色。"""

    async def dependency(request: Request) -> Principal:
        principal = authenticate(request, allow_query=allow_query)
        if not principal.can(need):
            raise HTTPException(
                status_code=403,
                detail=f"需要 {need} 角色，当前是 {principal.role}",
            )
        return principal

    return dependency
