"""MCP 客户端：把远端 server 的工具接进 Agent。

为什么要一个客户端而不是"直接调函数"：
    直接调函数是硬编码。MCP 的价值是**协议**——
    同一个客户端可以接任何遵守协议的 server，不需要改 Agent 代码。

工程上的三个细节（面试会被问）：
    1. 握手有时序：initialize 必须先回，才能发 tools/list
    2. 读取要有超时，否则 server 挂起会把 Agent 卡死
    3. 工具名要加前缀，避免和本地工具重名
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Self

PREFIX = "mcp_"
PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    pass


class MCPClient:
    """stdio 传输的 MCP 客户端。

    用后台线程读 stdout、投进队列：
        readline 阻塞在 BSD/macOS 上不好设超时，线程 + 队列最省事也最稳。
    """

    def __init__(self, command: list[str], *, timeout: float = 10.0) -> None:
        self.command = command
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 0
        self.server_info: dict = {}
        self.tools: list[dict] = []

    # ------------------------------------------------------------------
    def start(self) -> Self:
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pipeline-copilot", "version": "1.0.0"},
        })
        self.server_info = init.get("serverInfo") or {}
        self._notify("notifications/initialized")

        listed = self._request("tools/list", {})
        self.tools = [
            {
                "name": PREFIX + t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in (listed.get("tools") or [])
        ]
        return self

    # ------------------------------------------------------------------
    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            self._queue.put(line)
        self._queue.put(None)  # 结束标记

    def _send(self, payload: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise MCPError("客户端未启动")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(f"{method} 超时（{self.timeout}s）")
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise MCPError(f"{method} 超时（{self.timeout}s）") from exc

            if line is None:
                raise MCPError("server 已退出")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != rid:
                continue  # 忽略通知和其他响应
            if "error" in msg:
                raise MCPError(f"{method} 失败: {msg['error'].get('message')}")
            return msg.get("result") or {}

    # ------------------------------------------------------------------
    def call(self, tool_name: str, arguments: dict | None = None) -> dict:
        """调用远端工具。返回 {ok, text}，不抛业务异常。"""
        name = tool_name.removeprefix(PREFIX)
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return {"ok": not result.get("isError", False), "text": text}

    def tool_schemas(self) -> list[dict]:
        """转成 OpenAI function calling 的格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                },
            }
            for t in self.tools
        ]

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
