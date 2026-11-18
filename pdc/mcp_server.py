"""一个最小可用的 MCP Server（stdio 传输）。

为什么自己写一个：
    1. 本机没有 node，官方的 filesystem server 跑不起来
    2. MCP 的协议本身很小，写一遍比读十篇文档更懂
    3. 需要它来验证 client —— 同一个仓库里两头都有，才能测端到端

协议要点（MCP 2024-11-05）：
    - JSON-RPC 2.0，一行一个 JSON 对象，走 stdin/stdout
    - 握手：initialize → notifications/initialized
    - 发现：tools/list 返回 {name, description, inputSchema}
    - 调用：tools/call 返回 {content: [{type: "text", text}], isError}

跑法：
    python -m pdc.mcp_server
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "pdc-filesystem", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def find_pipelines(directory: str = ".") -> str:
    base = Path(directory).expanduser()
    if not base.exists():
        raise FileNotFoundError(f"目录不存在: {directory}")
    files = sorted(p.name for p in base.glob("*.yaml"))
    return json.dumps({"count": len(files), "files": files}, ensure_ascii=False)


def pipeline_stats(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    jobs = (data.get("spec") or {}).get("jobs") or []
    summary = {
        "name": (data.get("metadata") or {}).get("name", p.stem),
        "kind": data.get("kind", ""),
        "jobs": [
            {
                "job_type": j.get("jobType"),
                "extracts": len(j.get("extract") or []),
                "transforms": len(j.get("transform") or []),
                "load_columns": len((j.get("load") or {}).get("schema") or []),
            }
            for j in jobs
        ],
    }
    return json.dumps(summary, ensure_ascii=False)


TOOLS: dict[str, dict[str, Any]] = {
    "find_pipelines": {
        "fn": find_pipelines,
        "description": "列出目录下的 pipeline YAML 文件。",
        "inputSchema": {
            "type": "object",
            "properties": {"directory": {"type": "string", "description": "目录路径"}},
            "required": [],
        },
    },
    "pipeline_stats": {
        "fn": pipeline_stats,
        "description": "读取一个 pipeline YAML，返回各 job 的 extract/transform/load 列数统计。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "YAML 文件路径"}},
            "required": ["path"],
        },
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC 分发
# ---------------------------------------------------------------------------
def handle(request: dict) -> dict | None:
    """处理一条请求。通知类请求返回 None。"""
    method = request.get("method")
    rid = request.get("id")

    if rid is None:
        return None  # 通知，不需要回复

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method == "tools/list":
        return ok({
            "tools": [
                {"name": name, "description": t["description"], "inputSchema": t["inputSchema"]}
                for name, t in TOOLS.items()
            ]
        })

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return err(-32602, f"未知工具: {name}")
        try:
            text = TOOLS[name]["fn"](**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001
            return ok({
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })

    return err(-32601, f"不支持的方法: {method}")


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
