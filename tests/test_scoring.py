"""评测打分模块的测试。

这个模块之前覆盖率是 0%——CLI 里能 `--weights` 覆盖权重，
但没有任何用例验证过"权重会不会被正确归一化""校准会不会把过于自信判反"。

打分是评测的地基：如果 weighted_score 算错，后面所有"通过率"都不可信。
所以这里测的不是实现细节，是**打分规则本身**。
"""

from __future__ import annotations

import pytest

from pdc.scoring import (
    DEFAULT_WEIGHTS,
    Metrics,
    calibrate,
    parse_weights,
    weighted_score,
)


class TestParseWeights:
    def test_none_returns_defaults(self) -> None:
        assert parse_weights(None) == DEFAULT_WEIGHTS

    def test_defaults_sum_to_one(self) -> None:
        # 权重不归一化，总分就会超过 1，"0.93 分"这种说法就没意义
        assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9

    def test_override_is_normalized(self) -> None:
        w = parse_weights("grounding=0.6")
        # 0.6 加上其余默认值 (0.15+0.25+0.20+0.10) = 1.30
        assert w["grounding"] == pytest.approx(0.6 / 1.3, abs=1e-4)
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)
        # 覆盖后 grounding 必须仍是最高的
        assert w["grounding"] == max(w.values())

    def test_multiple_overrides(self) -> None:
        w = parse_weights("structural=0.4,detection=0.4")
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)
        assert w["structural"] > w["contract"]

    def test_blank_segments_ignored(self) -> None:
        # grounding 默认就是 0.30，这里改成 0.40 才能真正改变总量
        assert parse_weights(" , grounding=0.4 , ")["grounding"] == pytest.approx(
            0.4 / 1.1, abs=1e-4
        )

    def test_unknown_metric_raises(self) -> None:
        with pytest.raises(ValueError, match="未知指标"):
            parse_weights("accuracy=0.5")

    def test_zero_total_raises(self) -> None:
        with pytest.raises(ValueError, match="权重之和必须为正"):
            parse_weights(
                "structural=0,contract=0,detection=0,grounding=0,stability=0"
            )


class TestWeightedScore:
    def test_all_zero(self) -> None:
        m = Metrics(structural=0, contract=0, detection=0, grounding=0, stability=0)
        assert weighted_score(m) == 0.0

    def test_all_one(self) -> None:
        m = Metrics(structural=1, contract=1, detection=1, grounding=1, stability=1)
        assert weighted_score(m) == 1.0

    def test_single_metric_uses_its_weight(self) -> None:
        m = Metrics(structural=1, contract=0, detection=0, grounding=0, stability=0)
        assert weighted_score(m) == DEFAULT_WEIGHTS["structural"]

    def test_grounding_outweighs_structural(self) -> None:
        """这条是设计原则的守卫：编造事实比格式难看危险得多。

        如果哪天有人把 grounding 的权重调到低于 structural，这个用例会失败，
        逼他解释为什么。
        """
        only_grounding = Metrics(
            structural=0, contract=0, detection=0, grounding=1, stability=0
        )
        only_structural = Metrics(
            structural=1, contract=0, detection=0, grounding=0, stability=0
        )
        assert weighted_score(only_grounding) > weighted_score(only_structural)

    def test_custom_weights_override_defaults(self) -> None:
        m = Metrics(structural=1, contract=0, detection=0, grounding=0, stability=0)
        assert weighted_score(m, {"structural": 0.5}) == 0.5

    def test_unknown_metric_in_metrics_ignored(self) -> None:
        # Metrics 允许塞 extra，但打分只认权重表里有的键
        m = Metrics(
            structural=1, contract=0, detection=0, grounding=0, stability=0,
            extra={"whatever": 999},
        )
        assert weighted_score(m) == DEFAULT_WEIGHTS["structural"]

    def test_missing_key_in_custom_weights_treated_as_zero(self) -> None:
        m = Metrics(structural=1, contract=1, detection=0, grounding=0, stability=0)
        assert weighted_score(m, {"structural": 1.0}) == 1.0


class TestCalibrate:
    def test_overconfident(self) -> None:
        r = calibrate([0.9, 0.9], [0.7, 0.7])
        assert r["verdict"] == "过于自信"
        assert r["bias"] == pytest.approx(0.2)
        assert r["mae"] == pytest.approx(0.2)
        assert r["n"] == 2

    def test_underconfident(self) -> None:
        r = calibrate([0.5, 0.5], [0.7, 0.7])
        assert r["verdict"] == "过于保守"
        assert r["bias"] == pytest.approx(-0.2)

    def test_well_calibrated(self) -> None:
        r = calibrate([0.7, 0.8], [0.7, 0.8])
        assert r["verdict"] == "基本校准"
        assert r["mae"] == 0.0
        assert r["bias"] == 0.0

    def test_mae_does_not_cancel_out(self) -> None:
        """偏差一正一负时 bias 会抵消，但 mae 不会——这正是要同时报两个数的原因。"""
        r = calibrate([0.9, 0.5], [0.7, 0.7])
        assert r["bias"] == pytest.approx(0.0)
        assert r["mae"] == pytest.approx(0.2)
        assert r["verdict"] == "基本校准"

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="长度必须一致"):
            calibrate([0.9], [0.7, 0.6])

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            calibrate([], [])

    def test_means_are_reported(self) -> None:
        r = calibrate([0.8, 0.6], [0.9, 0.7])
        assert r["system_mean"] == pytest.approx(0.7)
        assert r["human_mean"] == pytest.approx(0.8)


class TestMetricsAsDict:
    def test_defaults_present(self) -> None:
        d = Metrics().as_dict()
        assert set(DEFAULT_WEIGHTS) <= set(d)

    def test_extra_merged(self) -> None:
        d = Metrics(extra={"note": "x"}).as_dict()
        assert d["note"] == "x"
