"""few-shot 选择逻辑的测试（不联网、不碰语料）。

这里测的是"怎么选示例"，不是"选了有没有用"——
后者要跑真实模型，属于 eval/fewshot_ab.py 的事。
两者必须分开：选错了是代码 bug，效果不好是实验结论。
"""

from __future__ import annotations

import pytest

from eval.fewshot_ab import _jac, similarity, structural_neighbors


def card(name: str, chain=("eid",), ex=("cos",), tx=("Select",), ncols=10) -> dict:
    return {
        "name": name,
        "chain": tuple(chain),
        "ex": tuple(ex),
        "tx": tuple(tx),
        "ncols": ncols,
        "path": None,
    }


def test_similarity_of_identical_cards_is_one() -> None:
    a = card("a")
    # 浮点累加会有 1e-16 的误差，不能直接 == 1.0
    assert similarity(a, card("b")) == pytest.approx(1.0)


def test_chain_difference_dominates() -> None:
    """链不同但变换相同，相似度必须明显低于同链。

    0.40 的权重就是为此设的：链是结构里最硬的信号。
    """
    q = card("q")
    same_chain = card("s", tx=("Join",))
    diff_chain = card("d", chain=("datamart-epmm",))
    assert similarity(q, same_chain) > similarity(q, diff_chain)


def test_jaccard_handles_empty_sets() -> None:
    assert _jac((), ()) == 1.0
    assert _jac(("a",), ()) == 0.0
    assert _jac(("a", "b"), ("b", "c")) == 1 / 3


def test_neighbors_prefer_exact_structure() -> None:
    q = card("q")
    exact = card("exact")
    same_chain_only = card("loose", tx=("Join", "Drop"))
    other = card("other", chain=("eid", "datamart-epmm"))
    got = structural_neighbors(q, [other, same_chain_only, exact], k=1)
    assert got == [exact]


def test_neighbors_relax_when_exact_key_absent() -> None:
    """没有完全同构的，就退到"同链 + 同连接器"。"""
    q = card("q", tx=("Select", "Join"))
    peer = card("peer", tx=("Select",))
    other = card("other", chain=("landing",))
    got = structural_neighbors(q, [peer, other], k=1)
    assert got == [peer]


def test_neighbors_fall_back_to_chain() -> None:
    """连连接器都不同，最后只按链找。"""
    q = card("q", ex=("cos",), tx=("Select",))
    peer = card("peer", ex=("jdbc",), tx=("Drop",))
    other = card("other", chain=("landing",))
    got = structural_neighbors(q, [peer, other], k=1)
    assert got == [peer]


def test_neighbors_never_include_self() -> None:
    q = card("q")
    pool = [q, card("a"), card("b")]
    got = structural_neighbors(q, pool, k=3)
    assert q not in got
    assert len(got) == 2


def test_neighbors_return_at_most_k() -> None:
    q = card("q")
    pool = [q] + [card(f"c{i}") for i in range(10)]
    assert len(structural_neighbors(q, pool, k=3)) == 3
