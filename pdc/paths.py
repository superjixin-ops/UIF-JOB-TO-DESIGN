"""路径解析：本地开发和容器部署用的是两套路径。

优先级：
    环境变量 > 仓库内 samples/ > 本机 CRD 仓库

为什么需要这一层：
    仓库自带 samples/ 里的示例数据，开箱即用。
    想指向自己的数据时设 PDC_REPO / PDC_RULES_DIR / PDC_SKILLS_DIR。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

def _pick(env_key: str, *candidates: Path) -> Path:
    """取第一个存在的路径；都不存在就返回第一个候选（让上层报错更清楚）。"""
    raw = os.getenv(env_key)
    if raw:
        return Path(raw).expanduser()
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def repo_dir() -> Path:
    """pipeline yaml 所在目录。"""
    return _pick("PDC_REPO", SAMPLES / "pipelines")


def rules_dir() -> Path:
    """规则文档目录。"""
    return _pick("PDC_RULES_DIR", SAMPLES / "rules")


def skills_dir() -> Path:
    """skill 文档目录。"""
    return _pick("PDC_SKILLS_DIR", SAMPLES / "skills")


def out_dir() -> Path:
    """生成产物的目录。容器里是临时的，本地是 out/。"""
    path = Path(os.getenv("PDC_OUT_DIR", str(ROOT / "out"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def describe() -> dict:
    """启动时打印一次，方便排查"为什么找不到数据"。"""
    return {
        "repo": str(repo_dir()),
        "rules": str(rules_dir()),
        "skills": str(skills_dir()),
        "out": str(out_dir()),
        "rag_mode": os.getenv("PDC_RAG_MODE", "hybrid"),
    }
