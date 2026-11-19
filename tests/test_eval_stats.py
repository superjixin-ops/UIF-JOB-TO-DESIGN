"""评测聚合的测试：不联网，纯数字。

为什么单独测这个：均值和标准差算错，比模型跑得差更隐蔽——
报告看起来有小数位，像模像样，但结论是错的。
"""

from __future__ import annotations

from eval.build_eval import aggregate


def _report(rounds: list[int], tokens: list[int]) -> dict:
    cases = [
        {"case": f"c{i}", "passed": True, "rounds": r, "tokens": t, "grounding": 1.0}
        for i, (r, t) in enumerate(zip(rounds, tokens, strict=True))
    ]
    return {
        "summary": {
            "pass_rate": 1.0,
            "avg_rounds": sum(rounds) / len(rounds),
            "avg_tokens": sum(tokens) / len(tokens),
            "exact_match_rate": 0.0,
            "stuck_events": 0,
        },
        "cases": cases,
    }


def test_aggregate_reports_mean_and_std() -> None:
    reports = [_report([1, 2], [100, 200]), _report([2, 2], [200, 200]), _report([2, 2], [300, 200])]
    out = aggregate(reports)

    assert out["n"] == 3
    assert out["pass_rate"]["mean"] == 1.0
    assert out["pass_rate"]["std"] == 0.0
    # 轮数均值：1.5, 2.0, 2.0 -> 1.8333
    assert out["avg_rounds"]["mean"] == 1.8333
    assert out["avg_rounds"]["std"] > 0
    # 逐用例保留原始序列，方便人看波动
    assert out["per_case"]["c0"]["rounds"] == [1, 2, 2]


def test_aggregate_single_run_has_zero_std() -> None:
    """只跑一次时不能报"标准差 0"，那会假装稳定——n=1 的 std 无定义，这里取 0 但必须带 n。"""
    out = aggregate([_report([2, 2], [100, 100])])
    assert out["n"] == 1
    assert out["avg_rounds"]["std"] == 0.0
    assert out["avg_rounds"]["n"] == 1


def test_aggregate_empty_is_safe() -> None:
    assert aggregate([]) == {}
