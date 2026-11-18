"""评测用例。每条都是真实文件，expectations 是人工核对过的。

原则：
  - 不断言"模型说得好不好"（无法自动判断）
  - 只断言结构性事实（列数、sheet、finding code），这些可以用程序判定
"""

from __future__ import annotations

CASES = [
    {
        "id": "order-fulfillment",
        "name": "fact-order-fulfillment.yaml",
        "expect_finding": "schema_mismatch",
        "note": "帧比 schema 多 7 列，用于验证漂移检出",
    },
    {
        "id": "customer-segment",
        "name": "dim-customer-segment.yaml",
        "expect_finding": None,
        "note": "维度流水线，含 AddSurrogateKey 与 FilterType2",
    },
    {
        "id": "order-fulfillment-again",
        "name": "fact-order-fulfillment.yaml",
        "expect_finding": "schema_mismatch",
        "note": "重复用例，验证幂等性（同一输入两次结果一致）",
        "idempotent_with": "order-fulfillment",
    },
]
