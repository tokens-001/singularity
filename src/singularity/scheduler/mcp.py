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
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field

import httpx

from singularity.scheduler import config, witness
from singularity.scheduler.log import info as _log_info
from singularity.scheduler.log import warn as _log_warn

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
        self._proc: subprocess.Popen | None = None
        self._http_client: httpx.Client | None = None
        self._initialized = False
        self._tools: list[MCPTool] = []
        # stdio 是**一条字节流**，没有多路复用：两个线程同时收发会互相读到对方的响应
        # （各自按 id 判不是自己的就丢掉 ⇒ 两边一起超时）。锁的粒度 = 一次请求/响应往返，
        # 跟协议本身对齐。见 `_rpc_stdio` / `_recv_stdio`。
        self._stdio_lock = threading.RLock()
        # stdout 由**一个专用线程**独占读，读到的行进这个队列（见 `_ensure_reader`）。
        self._stdout_q: queue.Queue | None = None
        self._reader_proc = None

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
                    witness.warn('mcp', f'{e}')
            self._proc = None
        if self._http_client:
            try:
                self._http_client.close()
            except Exception as e:
                witness.warn('mcp', f'{e}')
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

    def _rpc(self, method: str, params: dict) -> dict | None:
        """发送 JSON-RPC 请求, 返回 result 或 None。"""
        if self.cfg.transport == "stdio":
            return self._rpc_stdio(method, params)
        elif self.cfg.transport == "http":
            return self._rpc_http(method, params)
        return None

    def _rpc_stdio(self, method: str, params: dict) -> dict | None:
        if not self._proc or self._proc.poll() is not None:
            return None
        req_id = _next_id()
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        try:
            # **发+收必须在同一把锁里**：只有一个 stdio 管道，两个线程同时往返会串。
            with self._stdio_lock:
                self._send_stdio(json.dumps(req) + "\n")
                return self._recv_stdio(req_id)
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

    def _ensure_reader(self) -> None:
        """起一个线程**独占**读 `stdout`，读到的行塞进队列（每个子进程一个）。

        ⚠️ **不能 `select` + `readline` 混用**（2026-09-14，我第一版就是这么写的，
        被自己写的测试当场打回）：`select` 看的是**底层 fd**，而 `readline` 是**带缓冲**的 ——
        子进程一次写三行时，第一次 `readline` 会把三行全吸进 Python 的缓冲区；
        之后 fd 上没数据、`select` 再也不 ready ⇒ **数据明明已经在手里，却一直等到超时**。
        让一个线程从头上独占读取、别人只从队列取，这个坑就不存在。

        哨兵 `None` 表示 **EOF**（子进程退出/管道关闭），消费侧据此立刻返回而不是空等。
        """
        if self._stdout_q is not None and self._reader_proc is self._proc:
            return
        q: queue.Queue = queue.Queue()
        proc = self._proc
        self._stdout_q, self._reader_proc = q, proc

        def _pump():
            try:
                for line in proc.stdout:      # 迭代到 EOF 自然结束
                    q.put(line)
            except Exception as e:            # noqa: BLE001
                # 读线程炸了 = 这个客户端的通道死了。**必须出声**，
                # 否则调用方只会看到"一个个请求超时"，查不出是读线程没了。
                _log_warn(_TAG, f"MCP[{self.cfg.name}]: stdout 读线程异常 "
                                f"({type(e).__name__})")
            finally:
                q.put(None)                   # EOF / 异常，都要叫醒还在等的消费侧

        threading.Thread(target=_pump, daemon=True,
                         name=f"mcp-reader-{self.cfg.name}").start()

    def _recv_stdio(self, want_id: int | None = None,
                    timeout: float = 30.0) -> dict | None:
        """读到**这一次请求**的响应为止 —— 按 `id` 认领，不认的丢掉继续等。

        ⚠️ 原来是"`readline` 一行就返回、`json.loads` 完就交差"，**从不看 `id`**
        （2026-09-14，外派 扫bug-02 ③ 核出，我回当前树确认过）。两个后果：
          · 服务端发**通知**（`notifications/*`，按协议就是没有 `id` 的）或往 stdout
            打日志时，返回的是那一条 —— **不是这次请求的响应**；
          · 更坏的是**超时之后**：30s 没等到就 `return None`，而响应还躺在管道里
            ⇒ **下一次调用读到的是上一次的响应**，从此整体串位。
            而且**全程不报错** —— 拿到的是一份形状合法、内容错位的结果，
            上层只会看到"工具返回了看不懂的东西"。

        现在：非 JSON 行跳过、`id` 对不上的丢掉，都**继续等**到这次的响应或超时。
        `want_id=None` 时退回旧行为（收到第一条 JSON 就返回）。
        """
        if not self._proc or not self._proc.stdout:
            return None
        self._ensure_reader()
        q = self._stdout_q
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                line = q.get(timeout=left)     # 超时是**剩余**时间，不是每次重新 30s
            except queue.Empty:
                return None
            if line is None:                   # EOF 哨兵：子进程没了
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 不少 MCP 服务器会往 stdout 打日志。**必须跳过继续等** ——
                # `return None` 就等于把这次的响应丢了（正是上面那个串位的起点）。
                _log_info(_TAG, f"MCP[{self.cfg.name}]: 跳过非 JSON 行 {line[:120]!r}")
                continue
            if not isinstance(msg, dict):
                continue
            if want_id is not None and msg.get("id") != want_id:
                continue          # 通知 / 上一次的迟到响应 —— 丢掉，继续等这一次的
            return msg

    def _rpc_http(self, method: str, params: dict) -> dict | None:
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
            witness.warn('mcp', f'{e}')


# ── MCP 注册表 ─────────────────────────────────────────────────────

class MCPRegistry:
    """管理所有 MCP 服务器连接和工具。

    单例模式, 在 orchestrator 启动时初始化。
    """

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}
        self._tools: list[MCPTool] = []
        self._tool_index: dict[str, MCPClient] = {}  # tool_name → client
        self._lock = threading.RLock()

    def load_configs(self, configs: list[MCPServerConfig]):
        """加载服务器配置, 连接并发现工具。

        原实现是"先 disconnect_all 清空、再逐个填回"，全在锁外 —— 执行中的任务
        正好撞在中间就会读到半截注册表（`未知 MCP 工具`，或迭代到重建中的列表），
        而并发连接还会被 disconnect_all 掐断。

        改为：先建到**局部**结构（不在锁内做网络/子进程发现），再整体原子换入。
        读方要么看到旧的完整集合、要么看到新的完整集合。
        """
        clients: dict[str, MCPClient] = {}
        tools: list[MCPTool] = []
        index: dict[str, MCPClient] = {}
        for cfg in configs:
            if not cfg.enabled:
                continue
            client = MCPClient(cfg)
            clients[cfg.name] = client
            found = client.discover_tools()
            for t in found:
                tools.append(t)
                index[t.name] = client
            if found:
                _log_info(_TAG, f"MCP[{cfg.name}]: 发现 {len(found)} 个工具: "
                          f"{', '.join(t.name for t in found)}")
            else:
                _log_warn(_TAG, f"MCP[{cfg.name}]: 未发现工具或连接失败")

        with self._lock:
            old = list(self._clients.values())
            self._clients, self._tools, self._tool_index = clients, tools, index
        for c in old:      # 锁外断开: disconnect 可能阻塞
            try:
                c.disconnect()
            except Exception as e:  # noqa: BLE001
                witness.warn("mcp", f"old_client_disconnect:{type(e).__name__}"[:80])

    def drop_server(self, name: str) -> bool:
        """把一个服务器**从注册表里摘掉**（客户端 + 它的全部工具），并断开连接。

        为什么要有这个方法（2026-09-14）：删配置 ≠ 删掉已经连上的那一个。
        `get_registry()` 是**全局单例**，`/api/mcp/servers/<name>` DELETE 过去只
        `save_mcp_configs` + 清 `dispatcher._MCP_CACHE`，而注册表里那个服务器的
        `_clients` / `_tools` / `_tool_index` **原样还在** ⇒ 下一次装配 agent 时旧工具
        照旧回来：界面上"删了"，模型还能调。
        同一段摘除逻辑原先只写在 `_api_admin.mcp_server_reconnect` 里（那里是
        "先摘再重连"）—— 收在这里，免得下次再有人新增一个"动配置"的入口又漏掉。
        """
        with self._lock:
            client = self._clients.pop(name, None)
            if client is None:
                return False
            self._tools = [t for t in self._tools if t.server_name != name]
            self._tool_index = {k: v for k, v in self._tool_index.items()
                                if v.cfg.name != name}
        try:                       # 锁外断开: disconnect 可能阻塞
            client.disconnect()
        except Exception as e:     # noqa: BLE001
            witness.warn("mcp", f"drop_server_disconnect:{type(e).__name__}"[:80])
        return True

    def get_all_tools(self) -> list[MCPTool]:
        """获取所有已发现的工具。"""
        with self._lock:
            return list(self._tools)

    def get_openai_tools(self) -> list[dict]:
        """将所有 MCP 工具转换为 OpenAI function calling 格式。"""
        result = []
        with self._lock:
            tools = list(self._tools)   # 快照: 别在重建期间迭代半截列表
        for t in tools:
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
        with self._lock:
            client = self._tool_index.get(full_name)
        if client is None:
            return f"未知 MCP 工具: {full_name}"
        # 注意: call_tool 在锁外调 —— 它可能阻塞很久，持锁会把刷新饿死。
        # 代价是"调用进行中被并发刷新断开"仍可能发生（刷新是人工低频操作，可接受）。
        # 从 full_name 提取原始工具名
        # 格式: mcp__<server_name>__<tool_name>
        parts = full_name.split("__", 2)
        if len(parts) < 3:
            return f"无效 MCP 工具名: {full_name}"
        tool_name = parts[2]
        return client.call_tool(tool_name, arguments)

    def disconnect_all(self):
        """断开所有服务器连接。"""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._tools.clear()
            self._tool_index.clear()
        for client in clients:
            try:
                client.disconnect()
            except Exception as e:  # noqa: BLE001
                witness.warn("mcp", f"disconnect:{type(e).__name__}"[:80])

    @property
    def server_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def tool_count(self) -> int:
        return len(self._tools)


# ── 全局单例 ───────────────────────────────────────────────────────

_registry: MCPRegistry | None = None


def get_registry() -> MCPRegistry:
    """获取全局 MCP 注册表单例。"""
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry


# ── 配置加载 ───────────────────────────────────────────────────────

MCP_CONFIG_PATH = config.SCHEDULER_DIR / "mcp_servers.toml"


def load_mcp_configs() -> list[MCPServerConfig]:
    """从 TOML 配置文件加载 MCP 服务器配置。

    ⚠️ **解析失败时不许回落 `_default_configs()`**（2026-09-14，C 的 S1 草案 §3.12，我核过）：
    原来读坏了就返回默认配置，而 `mcp_server_add` / `mcp_server_delete` / `refresh`
    都会拿手里的这份去 `save_mcp_configs` ⇒ **默认配置覆盖掉用户配的服务器**
    （命令、env、headers 全没），而 `mcp_servers.toml` 看起来完好。
    ⇒ 改成：读坏了 → **空列表**（带告警 + `.corrupt` 备份 + 写侧拒写），
    **宁可这一轮一个服务器都没有**，也不拿默认值去冒充用户的配置。
    """
    from ._io import load_toml_or_quarantine
    if not MCP_CONFIG_PATH.exists():
        return _default_configs()          # 真的没有文件：默认配置是对的
    data = load_toml_or_quarantine(MCP_CONFIG_PATH)
    if data is None:
        return []

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
    """保存 MCP 配置到 TOML 文件。

    ⚠️ 这一轮读到过损坏的配置（已隔离到 `.corrupt`）就**拒写** —— 见 `load_mcp_configs`。
    """
    # ⚠️ **判据是"我这次读出来的是什么"，不是模块级标记**（2026-09-14 修）：
    # 原来问的是 `_MCP_CONFIG_CORRUPT`，而那个全局只有 `load_mcp_configs` 被调过才为真
    # ⇒ **第一次触碰**（进程刚起、坏文件还没人读过）时闸门形同虚设，整份重建照写。
    # 这正是 §68 那条「写侧护栏查在读之前」，`load_for_rewrite` 那一族只修了 3 处、
    # 这条漏了（外派⑬ 报、我核过：生产入口是 `mcp_server_add` / `mcp_server_delete`）。
    from ._io import load_toml_for_rewrite
    _existing, writable = load_toml_for_rewrite(MCP_CONFIG_PATH)
    if not writable:
        _log_warn(_TAG, "保存被拒：mcp_servers.toml 损坏已隔离(.corrupt)，"
                        "拒绝拿手里的这份整份重建；人工恢复备份后重启即可")
        try:
            from singularity.scheduler import witness
            witness.warn("mcp", "save_skipped: mcp_servers.toml 损坏已隔离，拒绝整份重建",
                         key="mcp_config_corrupt")
        except Exception as e:      # noqa: BLE001
            _log_warn(_TAG, f"损坏告警的第二通道没发出去: {type(e).__name__}")
        return False
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
        lines.append("[[servers]]")
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
