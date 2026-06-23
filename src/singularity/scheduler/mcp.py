"""mcp — Model Context Protocol 集成

MCP 允许 Agent 发现和调用外部工具服务器提供的工具。
支持两种传输方式:
  - stdio: 启动本地进程, 通过 stdin/stdout 通信
  - http: 连接远程 MCP 服务器

协议: JSON-RPC 2.0
"""

from __future__ import annotations
import json
import os
import select
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from singularity.scheduler import config
from singularity.scheduler import witness
from singularity.scheduler.log import info as _log_info, warn as _log_warn

_TAG = "mcp"


# ── 数据类 ─────────────────────────────────────────────────────────

@dataclass
class MCPServerConfig:
    """MCP 服务器配置"""
    name: str                          # 唯一名称, 如 "filesystem", "web-search"
    transport: str = "stdio"           # stdio | http
    # stdio 传输
    command: str = ""                  # 启动命令, 如 "npx @anthropic/mcp-server-filesystem /tmp"
    # http 传输
    url: str = ""                      # HTTP endpoint
    headers: dict = field(default_factory=dict)
    # 通用
    enabled: bool = True
    timeout: float = 30.0              # 工具调用超时 (秒)
    env: dict = field(default_factory=dict)  # 额外环境变量


@dataclass
class MCPTool:
    """MCP 工具定义 (从 tools/list 返回)"""
    name: str
    description: str = ""
    inputSchema: dict = field(default_factory=dict)
    server_name: str = ""              # 所属服务器


# ── MCP 客户端 ─────────────────────────────────────────────────────

class MCPClient:
    """单个 MCP 服务器的客户端。

    支持 stdio (子进程) 和 HTTP 两种传输。
    """

    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None
        self._http_client: Optional[httpx.Client] = None
        self._initialized = False
        self._tools: list[MCPTool] = []

    # ── 连接管理 ──────────────────────────────────────────────────

    def connect(self) -> bool:
        """连接到 MCP 服务器并完成初始化握手。"""
        try:
            if self.cfg.transport == "stdio":
                return self._connect_stdio()
            elif self.cfg.transport == "http":
                return self._connect_http()
        except Exception as e:
            _log_warn(_TAG, f"MCP[{self.cfg.name}]: 连接失败: {e}")
            return False
        return False

    def _connect_stdio(self) -> bool:
        env = os.environ.copy()
        env.update(self.cfg.env)
        try:
            argv = shlex.split(self.cfg.command)
            self._proc = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError:
            _log_warn(_TAG, f"MCP[{self.cfg.name}]: 命令未找到: {self.cfg.command.split()[0]}")
            return False
        except Exception as e:
            _log_warn(_TAG, f"MCP[{self.cfg.name}]: 启动失败: {e}")
            return False

        # 初始化握手
        result = self._rpc_stdio("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "qidian-scheduler", "version": "1.0"},
        })
        if result is None:
            return False
        # 发送 initialized 通知
        self._send_stdio(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self._initialized = True
        return True

    def _connect_http(self) -> bool:
        try:
            self._http_client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
            result = self._rpc_http("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "qidian-scheduler", "version": "1.0"},
            })
            if result is None:
                return False
            # initialized 通知
            self._rpc_http_noreply("notifications/initialized", {})
            self._initialized = True
            return True
        except Exception as e:
            _log_warn(_TAG, f"MCP[{self.cfg.name}]: HTTP 连接失败: {e}")
            return False

    def disconnect(self):
        """断开连接。"""
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.stdout.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception as e:
                    witness.heartbeat('mcp', f'warn:{e}')
            self._proc = None
        if self._http_client:
            try:
                self._http_client.close()
            except Exception as e:
                witness.heartbeat('mcp', f'warn:{e}')
            self._http_client = None
        self._initialized = False

    # ── 工具发现 ──────────────────────────────────────────────────

    def discover_tools(self) -> list[MCPTool]:
        """从服务器获取工具列表。"""
        if not self._initialized:
            if not self.connect():
                return []
        result = self._rpc("tools/list", {})
        if result is None:
            return []
        tools_raw = result.get("tools", [])
        self._tools = []
        for t in tools_raw:
            tool = MCPTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                inputSchema=t.get("inputSchema", {}),
                server_name=self.cfg.name,
            )
            self._tools.append(tool)
        return self._tools

    # ── 工具调用 ──────────────────────────────────────────────────

    def call_tool(self, name: str, arguments: dict) -> str:
        """调用 MCP 工具并返回文本结果。"""
        if not self._initialized:
            if not self.connect():
                return f"MCP 服务器 [{self.cfg.name}] 未连接"
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result is None:
            return f"MCP 工具 [{name}] 调用失败"
        # 提取内容
        content = result.get("content", [])
        if isinstance(content, list):
            texts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        texts.append(c.get("text", ""))
                    elif c.get("type") == "resource":
                        texts.append(f"[Resource: {c.get('resource', {})}]")
                    else:
                        texts.append(json.dumps(c, ensure_ascii=False))
                elif isinstance(c, str):
                    texts.append(c)
            return "\n".join(texts)
        return json.dumps(content, ensure_ascii=False)

    # ── JSON-RPC 核心 ─────────────────────────────────────────────

    def _rpc(self, method: str, params: dict) -> Optional[dict]:
        """发送 JSON-RPC 请求, 返回 result 或 None。"""
        if self.cfg.transport == "stdio":
            return self._rpc_stdio(method, params)
        elif self.cfg.transport == "http":
            return self._rpc_http(method, params)
        return None

    def _rpc_stdio(self, method: str, params: dict) -> Optional[dict]:
        if not self._proc or self._proc.poll() is not None:
            return None
        req = {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params}
        try:
            self._send_stdio(json.dumps(req) + "\n")
            return self._recv_stdio()
        except Exception as e:
            _log_warn(_TAG, f"MCP[{self.cfg.name}]: stdio RPC 错误 ({method}): {e}")
            return None

    def _send_stdio(self, data: str):
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except BrokenPipeError:
            pass

    def _recv_stdio(self, timeout: float = 30.0) -> Optional[dict]:
        if not self._proc or not self._proc.stdout:
            return None
        try:
            # ponytail: select 防永久阻塞, 30s 超时保护
            ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
            if not ready:
                return None
            line = self._proc.stdout.readline()
            if not line:
                return None
            return json.loads(line)
        except (json.JSONDecodeError, Exception):
            return None

    def _rpc_http(self, method: str, params: dict) -> Optional[dict]:
        if not self._http_client:
            return None
        req = {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params}
        try:
            resp = self._http_client.post(
                self.cfg.url,
                json=req,
                headers={**self.cfg.headers, "Content-Type": "application/json"},
                timeout=self.cfg.timeout,
            )
            if resp.status_code >= 400:
                _log_warn(_TAG, f"MCP[{self.cfg.name}]: HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            return resp.json()
        except Exception as e:
            _log_warn(_TAG, f"MCP[{self.cfg.name}]: HTTP RPC 错误 ({method}): {e}")
            return None

    def _rpc_http_noreply(self, method: str, params: dict):
        """发送通知 (无需响应)。"""
        if not self._http_client:
            return
        req = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self._http_client.post(
                self.cfg.url,
                json=req,
                headers={**self.cfg.headers, "Content-Type": "application/json"},
                timeout=10.0,
            )
        except Exception as e:
            witness.heartbeat('mcp', f'warn:{e}')


# ── MCP 注册表 ─────────────────────────────────────────────────────

class MCPRegistry:
    """管理所有 MCP 服务器连接和工具。

    单例模式, 在 orchestrator 启动时初始化。
    """

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}
        self._tools: list[MCPTool] = []
        self._tool_index: dict[str, MCPClient] = {}  # tool_name → client

    def load_configs(self, configs: list[MCPServerConfig]):
        """加载服务器配置, 连接并发现工具。"""
        self.disconnect_all()
        for cfg in configs:
            if not cfg.enabled:
                continue
            client = MCPClient(cfg)
            self._clients[cfg.name] = client
            tools = client.discover_tools()
            for t in tools:
                self._tools.append(t)
                self._tool_index[t.name] = client
            if tools:
                _log_info(_TAG, f"MCP[{cfg.name}]: 发现 {len(tools)} 个工具: "
                          f"{', '.join(t.name for t in tools)}")
            else:
                _log_warn(_TAG, f"MCP[{cfg.name}]: 未发现工具或连接失败")

    def get_all_tools(self) -> list[MCPTool]:
        """获取所有已发现的工具。"""
        return list(self._tools)

    def get_openai_tools(self) -> list[dict]:
        """将所有 MCP 工具转换为 OpenAI function calling 格式。"""
        result = []
        for t in self._tools:
            params = dict(t.inputSchema)
            # 确保 required 字段存在
            if "required" not in params:
                # 从 properties 中推断 required
                props = params.get("properties", {})
                if props:
                    params["required"] = list(props.keys())
            result.append({
                "type": "function",
                "function": {
                    "name": f"mcp__{t.server_name}__{t.name}",
                    "description": f"[MCP:{t.server_name}] {t.description}",
                    "parameters": params,
                }
            })
        return result

    def execute_tool(self, full_name: str, arguments: dict) -> str:
        """执行 MCP 工具。full_name 格式: mcp__<server>__<tool>"""
        if full_name not in self._tool_index:
            return f"未知 MCP 工具: {full_name}"
        client = self._tool_index[full_name]
        # 从 full_name 提取原始工具名
        # 格式: mcp__<server_name>__<tool_name>
        parts = full_name.split("__", 2)
        if len(parts) < 3:
            return f"无效 MCP 工具名: {full_name}"
        tool_name = parts[2]
        return client.call_tool(tool_name, arguments)

    def disconnect_all(self):
        """断开所有服务器连接。"""
        for client in self._clients.values():
            client.disconnect()
        self._clients.clear()
        self._tools.clear()
        self._tool_index.clear()

    @property
    def server_count(self) -> int:
        return len(self._clients)

    @property
    def tool_count(self) -> int:
        return len(self._tools)


# ── 全局单例 ───────────────────────────────────────────────────────

_registry: Optional[MCPRegistry] = None


def get_registry() -> MCPRegistry:
    """获取全局 MCP 注册表单例。"""
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry


# ── 配置加载 ───────────────────────────────────────────────────────

MCP_CONFIG_PATH = config.SCHEDULER_DIR / "mcp_servers.toml"


def load_mcp_configs() -> list[MCPServerConfig]:
    """从 TOML 配置文件加载 MCP 服务器配置。"""
    from ._io import load_toml
    if not MCP_CONFIG_PATH.exists():
        return _default_configs()
    try:
        data = load_toml(MCP_CONFIG_PATH)
    except Exception as e:
        _log_warn(_TAG, f"读取 mcp_servers.toml 失败: {e}, 使用默认配置")
        return _default_configs()

    configs = []
    servers = data.get("servers", [])
    for s in servers:
        cfg = MCPServerConfig(
            name=s.get("name", ""),
            transport=s.get("transport", "stdio"),
            command=s.get("command", ""),
            url=s.get("url", ""),
            headers=s.get("headers", {}),
            enabled=s.get("enabled", True),
            timeout=s.get("timeout", 30.0),
            env=s.get("env", {}),
        )
        configs.append(cfg)
    return configs


def save_mcp_configs(configs: list[MCPServerConfig]) -> bool:
    """保存 MCP 配置到 TOML 文件。"""
    servers = []
    for c in configs:
        s = {
            "name": c.name,
            "transport": c.transport,
            "enabled": c.enabled,
            "timeout": c.timeout,
        }
        if c.command:
            s["command"] = c.command
        if c.url:
            s["url"] = c.url
        if c.headers:
            s["headers"] = c.headers
        if c.env:
            s["env"] = c.env
        servers.append(s)
    content = _toml_dumps({"servers": servers})
    try:
        MCP_CONFIG_PATH.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        _log_warn(_TAG, f"保存 mcp_servers.toml 失败: {e}")
        return False


def _default_configs() -> list[MCPServerConfig]:
    """返回内置默认 MCP 服务器配置 (例: 文件系统服务器)。"""
    return []


# ── TOML 序列化辅助 (无需第三方库) ──────────────────────────────────

def _toml_dumps(data: dict) -> str:
    """将嵌套 dict 序列化为 TOML 格式 (仅支持简单结构)。"""
    lines = []
    servers = data.get("servers", [])
    for i, s in enumerate(servers):
        lines.append(f"[[servers]]")
        for k, v in s.items():
            lines.append(_toml_kv(k, v))
        if i < len(servers) - 1:
            lines.append("")
    return "\n".join(lines) + "\n"


def _toml_str(s: str) -> str:
    """转义 TOML 基本字符串中的特殊字符。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')

def _toml_kv(key: str, value) -> str:
    """序列化单个 key-value。"""
    if isinstance(value, bool):
        return f"{key} = {str(value).lower()}"
    elif isinstance(value, (int, float)):
        return f"{key} = {value}"
    elif isinstance(value, dict):
        if not value:
            return f"{key} = {{}}"
        inner = ", ".join(f'{k} = "{_toml_str(str(v))}"' for k, v in value.items())
        return f"{key} = {{ {inner} }}"
    else:
        return f'{key} = "{_toml_str(str(value))}"'


# ── RPC ID 生成 ────────────────────────────────────────────────────

_counter = 0


def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter
