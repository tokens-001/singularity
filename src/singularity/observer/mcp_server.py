"""mcp_server.py — MCP (Model Context Protocol) stdio server.

让 Claude Code 等 MCP 客户端能直接调用奇点工具。
ponytail: JSON-RPC 2.0 over stdio, 复用 observer_agent 的工具注册表。
"""

from __future__ import annotations
import json
import sys
import traceback
from typing import Any


def _build_mcp_tools() -> list[dict]:
    """从 observer_agent 的工具注册表生成 MCP 格式的工具列表。"""
    try:
        from singularity.scheduler.observer_agent import _TOOL_REGISTRY
    except Exception:
        return []
    tools = []
    for t in _TOOL_REGISTRY:
        props = {}
        required = []
        for pname, pinfo in t.get("params", {}).items():
            props[pname] = {"type": pinfo.get("type", "string"),
                          "description": pinfo.get("description", "")}
            if pinfo.get("required"):
                required.append(pname)
        tools.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
            } if props else {"type": "object", "properties": {}},
        })
    return tools


def _call_tool(name: str, arguments: dict) -> list[dict]:
    """调用 observer 工具，返回 MCP content 列表。"""
    try:
        from singularity.scheduler.observer_agent import _execute_observer_tool
        result = _execute_observer_tool(name, arguments)
        return [{"type": "text", "text": result}]
    except Exception as e:
        return [{"type": "text", "text": json.dumps({"error": str(e)})}]


def _handle_request(req: dict) -> dict | None:
    """处理单个 JSON-RPC 请求。通知 (无 id) 返回 None。"""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "singularity-observer", "version": "0.1"},
            "capabilities": {"tools": {}},
        }}
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _build_mcp_tools()}}
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        content = _call_tool(tool_name, arguments)
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": content}}
    elif method == "notifications/initialized":
        return None
    else:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def run_stdio() -> None:
    """stdio 传输: 从 stdin 读 JSON-RPC 请求, 写回 stdout。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = _handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception:
            tb = traceback.format_exc()
            err = {"jsonrpc": "2.0", "id": req.get("id"),
                   "error": {"code": -32603, "message": tb[-200:]}}
            sys.stdout.write(json.dumps(err, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
