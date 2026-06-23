"""_api_mcp.py — MCP server handlers."""
from __future__ import annotations

def mcp_server_list():
    from . import mcp as m; configs = m.load_mcp_configs(); reg = m.get_registry()
    servers = []
    for c in configs:
        connected = c.name in reg._clients
        tc = len(reg._clients[c.name]._tools) if connected else 0
        servers.append({"name": c.name, "transport": c.transport, "command": c.command,
            "url": c.url, "enabled": c.enabled, "timeout": c.timeout, "connected": connected, "tool_count": tc})
    return {"servers": servers}, 200

def mcp_server_add(data):
    from . import mcp as m
    if not data or not data.get("name"): return {"error": "缺少 name"}, 400
    configs = m.load_mcp_configs(); found = False
    for c in configs:
        if c.name == data["name"]:
            c.transport = data.get("transport", c.transport); c.command = data.get("command", c.command)
            c.url = data.get("url", c.url); c.enabled = data.get("enabled", c.enabled)
            c.timeout = data.get("timeout", c.timeout); c.env = data.get("env", c.env); found = True; break
    if not found:
        configs.append(m.MCPServerConfig(name=data["name"], transport=data.get("transport","stdio"),
            command=data.get("command",""), url=data.get("url",""), enabled=data.get("enabled",True),
            timeout=data.get("timeout",30.0), env=data.get("env",{})))
    m.save_mcp_configs(configs); return {"ok": True}, 200

def mcp_server_delete(name):
    from . import mcp as m; configs = m.load_mcp_configs()
    m.save_mcp_configs([c for c in configs if c.name != name]); return {"ok": True}, 200

def mcp_server_reconnect(name):
    from . import mcp as m; configs = m.load_mcp_configs(); reg = m.get_registry()
    for c in configs:
        if c.name == name:
            if name in reg._clients:
                reg._clients[name].disconnect(); del reg._clients[name]
                reg._tools = [t for t in reg._tools if t.server_name != name]
                reg._tool_index = {k:v for k,v in reg._tool_index.items() if v.cfg.name != name}
            reg.load_configs([c]); return {"ok": True, "tool_count": len(reg._tools)}, 200
    return {"error": f"服务器 {name} 不存在"}, 404

def mcp_tool_list():
    from . import mcp as m; reg = m.get_registry()
    return {"tools": [{"name": f"mcp__{t.server_name}__{t.name}", "server": t.server_name,
        "tool": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in reg.get_all_tools()]}, 200

def mcp_refresh():
    from . import mcp as m; configs = m.load_mcp_configs(); m.get_registry().load_configs(configs)
    return {"ok": True, "servers": m.get_registry().server_count, "tools": m.get_registry().tool_count}, 200
