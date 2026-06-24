"""executors.openai_agent — 通用Agent Runtime。

不依赖 Claude Code CLI。任意 OpenAI 兼容 API 都能用。
给模型装上手脚: 读文件、写代码、跑命令、搜代码。

协议: OpenAI function calling (tools API)。
支持: Kimi / GLM / DeepSeek API / Qwen / 任何 /v1/chat/completions。
"""

from __future__ import annotations
import json
import os
import re
import shlex
import ssl
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from singularity.scheduler.executors.base import (BaseExecutor, ExecutorResult,
    _BLOCKED_PATTERNS, _BLOCKED_COMMANDS)
from singularity.scheduler import witness
from singularity.scheduler import config

# ── blocklist 已统一到 base.py ──

# ── Tool 定义 (OpenAI function calling 格式) ──

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容。path=单文件, paths=批量读(一次返回所有文件内容)。研究/调研时用paths批量读，减少往返。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "单文件路径"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "批量文件路径，一次读多个。调研/跨文件分析时批量传"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件内容。会覆盖已有文件或创建新文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文件内容"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "运行终端命令。用于运行测试、lint、安装依赖等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的shell命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在项目中搜索匹配的代码行。用于找到相关代码、理解调用关系。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索模式（支持正则）"},
                    "path": {"type": "string", "description": "搜索的子目录，为空则全项目搜索"}
                },
                "required": ["pattern"]
            }
        }
    },
]

# 系统提示：告诉模型怎么用工具
SYSTEM_PROMPT = """你是Singularity Dispatch的 AI Agent。你的唯一任务是产出可运行的代码。不要输出方案、计划或分析，直接写代码。

你有工具可以用：读文件、写代码、跑命令、搜代码。

工作方式:
- 如果是纯分析/调研任务（不需要改代码），直接输出分析结果，不要调用工具
- 如果需要改代码：先读相关文件→立即用write_file写代码→跑测试→修复→输出最终代码
- 如果工具返回"文件不存在"或空结果超过2次，基于已有知识直接写代码，不要反复尝试

铁则:
- 禁止输出"方案1/方案2"、"建议采取以下步骤"等分析内容，直接实施
- 改代码前必须 read_file 看原文件
- 不要在注释里留 TODO，要么实现要么删掉
- 参数 path 是相对于项目根目录的路径，不要用绝对路径
- 写文件时给完整可运行的代码，不只给 diff
- 分析/调研/总结类任务：第一轮直接输出答案，不调用工具
- 任务完成时，必须在输出末尾附加 [HANDOFF] 块，格式如下:
  [HANDOFF]
  deliverable: <产出文件路径或描述>
  conclusion: <关键结论，一句话>
  next: <建议下一个 Agent，如 Coding/QA/Review/None>
  human_confirm: <true/false，是否需要人工确认>"""


class OpenAIAgentExecutor(BaseExecutor):
    """通用 Agent Executor — 给任何 OpenAI 兼容模型装上工具。"""

    def __init__(self, cfg: dict, task: str, task_id: str,
                 baseline_ref: str = "", cwd: str = "",
                 agent_level: str = "",
                 # ── 依赖注入 (由 dispatcher 提供, 消除反向导入) ──
                 skills: dict = None,
                 skill_tools: list = None,
                 skill_prompt: str = "",
                 mcp_tools: list = None,
                 mcp_executor: callable = None,
                 permission_checker: callable = None):
        super().__init__(cfg, task, task_id, baseline_ref=baseline_ref, cwd=cwd)
        self._api_key = os.environ.get(cfg.get("api_key_env", ""), "")
        # ponytail: 存为实例属性, 不写全局 os.environ (防并发 Agent 竞态)
        self._agent_env = dict(cfg.get("env", {}))
        self._url = cfg.get("entry", "")
        self._is_responses_api = "/v1/responses" in self._url or "/responses" in self._url
        self._model = cfg.get("request_template", {}).get("model", cfg.get("model", ""))
        self._max_turns = cfg.get("max_turns", 15)  # ponytail: coding任务需要足够轮次(读→写→测→修)
        self._cwd = Path(cwd) if cwd else config.PROJECT_ROOT
        self._changed_files: list[str] = []
        self._tool_events: list[dict] = []
        self._agent_level = agent_level or cfg.get("_level", "")

        # ── 注入的依赖 ──
        self._skills = skills or {}
        self._skill_tools = skill_tools or []
        self._skill_prompt = skill_prompt or ""
        self._mcp_tools = mcp_tools or []
        self._mcp_executor = mcp_executor
        self._permission_checker = permission_checker

    def run(self) -> ExecutorResult:
        if not self._api_key:
            return ExecutorResult(success=False, error=f"API key 未设置: {self.cfg.get('api_key_env','')}", tool_events=list(self._tool_events))

        start = time.time()
        # ── 合并 skill tools 和 prompt ──
        tools = list(TOOLS)
        tools.extend(self._skill_tools)
        tools.extend(self._mcp_tools)
        system_prompt = SYSTEM_PROMPT
        if self._skill_prompt:
            system_prompt += "\n" + self._skill_prompt

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.task},
        ]
        total_tokens = 0
        tool_turns = 0          # 连续工具调用轮数
        last_tool_calls = ""     # 上一轮工具调用指纹 (去重)
        max_tool_turns = self.cfg.get("max_tool_turns", 3)

        for turn in range(1, self._max_turns + 1):
            # 从 request_template 读取参数，只传模型支持的
            tmpl = self.cfg.get("request_template", {})
            if self._is_responses_api:
                # responses API 格式: input 代 messages, tools 结构不同
                resp_tools = []
                for t in tools:
                    f = t.get("function", {})
                    resp_tools.append({
                        "type": "function",
                        "name": f.get("name", ""),
                        "description": f.get("description", ""),
                        "parameters": f.get("parameters", {}),
                    })
                body = {
                    "model": self._model,
                    "input": [{"role": m.get("role","user"), "content": m.get("content","")} for m in messages],
                    "tools": resp_tools,
                    "tool_choice": "auto",
                    "max_output_tokens": tmpl.get("max_output_tokens", tmpl.get("max_completion_tokens", tmpl.get("max_tokens", 8192))),
                }
                if "temperature" in tmpl:
                    body["temperature"] = tmpl["temperature"]
            else:
                body = {
                    "model": self._model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                }
                # GPT-5.5+ 用 max_completion_tokens, 旧模型用 max_tokens
                if "max_completion_tokens" in tmpl:
                    body["max_completion_tokens"] = tmpl["max_completion_tokens"]
                elif "max_tokens" in tmpl:
                    body["max_tokens"] = tmpl["max_tokens"]
                else:
                    body["max_tokens"] = 8192
                if "temperature" in tmpl:
                    body["temperature"] = tmpl["temperature"]

            try:
                resp_data = self._api_call(body)
            except _RateLimitError:
                time.sleep(2 ** turn)
                continue
            except (_NetworkError, _FormatError) as e:
                return ExecutorResult(success=False, error=str(e),
                                      error_kind="exec", elapsed=time.time() - start,
                                      tool_events=list(self._tool_events))

            if self._is_responses_api:
                # responses API → chat format
                output = resp_data.get("output", [])
                msg = {}
                tool_calls_list = []
                for item in output:
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                msg["content"] = (msg.get("content","") + c.get("text","")).strip()
                    elif item.get("type") == "function_call":
                        tool_calls_list.append({
                            "id": item.get("call_id", ""),
                            "type": "function",
                            "function": {"name": item.get("name",""), "arguments": item.get("arguments","")}
                        })
                if tool_calls_list:
                    msg["tool_calls"] = tool_calls_list
            else:
                choice = resp_data.get("choices", [{}])[0]
                msg = choice.get("message", {})
            total_tokens += resp_data.get("usage", {}).get("total_tokens", 0)
            # 推理模型(如Kimi/GLM)返回reasoning_content, API输入不接受此字段
            msg_clean = {k: v for k, v in msg.items() if k != "reasoning_content"}
            messages.append(msg_clean)

            # 有 tool_calls → 执行工具
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except Exception:
                        # 模型吐的 JSON 可能有单引号/中文标点 → 尝试修复
                        raw_args = func.get("arguments", "{}")
                        try:
                            fixed = raw_args.replace("'", '"')
                            args = json.loads(fixed)
                        except Exception:
                            args = {}
                    # ── 工具事件: 记录开始执行 ──
                    t_start = time.time()
                    self._tool_events.append({
                        "kind": "tool:start",
                        "tool": name,
                        "task_id": self.task_id,
                        "ts": t_start,
                        "msg": f"🔧 {name}",
                    })
                    # ── 执行工具 ──
                    result = self._execute_tool(name, args)
                    # ── 工具事件: 记录完成 ──
                    t_done = time.time()
                    result_preview = result[:120] if len(result) > 120 else result
                    self._tool_events.append({
                        "kind": "tool:done",
                        "tool": name,
                        "task_id": self.task_id,
                        "ts": t_done,
                        "elapsed": round(t_done - t_start, 3),
                        "result_preview": result_preview,
                        "result_len": len(result),
                        "msg": f"✅ {name} ({len(result)}字符, {round(t_done-t_start,2)}s)",
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                # 死循环检测
                tool_turns += 1
                call_fingerprint = str([(tc.get("function", {}).get("name", ""),
                                        tc.get("function", {}).get("arguments", "")[:80])
                                        for tc in tool_calls])
                if tool_turns >= max_tool_turns + 2:
                    # 强制输出: 撤掉工具，注入系统消息要求模型直接回答
                    tools = []
                    messages.append({
                        "role": "system",
                        "content": "[系统] 已达最大工具调用轮次。现在必须直接输出最终答案，禁止再调用工具。"
                    })
                elif tool_turns >= max_tool_turns or (call_fingerprint == last_tool_calls and tool_turns >= 2):
                    # 警告: 注入系统消息，建议停止工具
                    messages.append({
                        "role": "system",
                        "content": "[系统] 已收集足够信息。停止使用工具，直接输出最终答案。"
                    })
                last_tool_calls = call_fingerprint
                continue  # 继续下一轮，让模型看工具结果

            # 无 tool_calls → 任务完成
            tool_turns = 0  # 重置工具计数
            # 推理模型(如Kimi/GLM)可能 content="" 但 reasoning_content 有内容
            content = msg.get("content", "") or msg.get("reasoning_content", "")
            if content.strip():
                elapsed = time.time() - start
                # 用 git diff 追踪改动的文件
                self._track_changed_files()
                return ExecutorResult(
                    success=True, raw_output=content,
                    changed_files=list(self._changed_files),
                    elapsed=elapsed, token_count=total_tokens,
                    tool_events=list(self._tool_events),
                )

        return ExecutorResult(success=False,
                              error=f"达到最大轮次 {self._max_turns}，任务未完成",
                              error_kind="exec", elapsed=time.time() - start,
                              tool_events=list(self._tool_events))

    # ── 工具执行 ──

    def _check_permission(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """Permission 检查。如注入 checker 则调用，否则默认允许。"""
        if self._permission_checker:
            try:
                return self._permission_checker(tool_name, args, self._agent_level, self.cfg.get("model", ""), self.task_id)
            except Exception:
                pass
        return True, ""

    def _execute_tool(self, name: str, args: dict) -> str:
        try:
            # ── Permission 检查 ──
            allowed, reason = self._check_permission(name, args)
            if not allowed:
                return f"操作被拒绝: {reason}"

            if name == "read_file":
                paths = args.get("paths", []) or []
                if args.get("path"):
                    paths.append(args["path"])
                return self._tool_read_multi(paths) if paths else "请指定 path 或 paths"
            elif name == "write_file":
                return self._tool_write(args.get("path", ""), args.get("content", ""))
            elif name == "run_command":
                return self._tool_run(args.get("command", ""))
            elif name == "search_code":
                return self._tool_search(args.get("pattern", ""), args.get("path", ""))
            # ── Skill 工具调用 ──
            skill_name = name.replace("_", "-")  # function name 用 _ 连词，SKILL.md 用 - 连词
            if skill_name in self._skills:
                skill = self._skills[skill_name]
                expanded = skill.expand_body(**args)
                return f"[Skill: {skill.name}]\n\n{expanded}\n\n请按以上 Skill 指引继续完成任务。"
            # ── MCP 工具调用 (由调用方注入) ──
            if name.startswith("mcp__") and self._mcp_executor:
                try:
                    return self._mcp_executor(name, args)
                except Exception as e:
                    return f"MCP 工具执行错误: {e}"
            return f"未知工具: {name}"
        except Exception as e:
            return f"工具执行错误: {e}"

    def _safe_path(self, path: str) -> Path:
        """安全检查: 解析路径，禁止逃出项目目录。"""
        p = (self._cwd / path).resolve()
        root = self._cwd.resolve()
        # 加 os.sep 防止前缀绕过: /a/b 不匹配 /a/bb/foo
        if not (str(p) + os.sep).startswith(str(root) + os.sep) and str(p) != str(root):
            raise ValueError(f"路径逃逸被拒绝: {path} → {p}")
        return p

    def _is_blocked_path(self, path: str) -> tuple[bool, str]:
        """检查路径是否命中敏感文件 blocklist。返回 (blocked, reason)。"""
        import fnmatch
        normalized = path.replace("\\", "/")
        for pattern in _BLOCKED_PATTERNS:
            # 路径任意段匹配 or 文件名匹配
            if fnmatch.fnmatch(normalized, pattern):
                return True, f"敏感文件/目录: {pattern}"
            if fnmatch.fnmatch(normalized, f"*/{pattern}"):
                return True, f"敏感文件/目录: {pattern}"
            # 检查路径中是否包含被屏蔽的目录段
            parts = normalized.split("/")
            for part in parts:
                if fnmatch.fnmatch(part, pattern.rstrip("/*")):
                    return True, f"敏感文件/目录: {pattern}"
        return False, ""

    def _is_dangerous_command(self, command: str) -> tuple[bool, str]:
        """检查 shell 命令是否危险。返回 (dangerous, reason)。"""
        # ponytail: strip 防空格绕过, 白名单前缀 + 子串双保险
        cmd = command.strip()
        cmd_lower = cmd.lower()
        for blocked in _BLOCKED_COMMANDS:
            bl = blocked.lower()
            if cmd_lower.startswith(bl) or bl in cmd_lower:
                return True, f"危险命令被拦截: {blocked}"
        return False, ""

    def _tool_read(self, path: str) -> str:
        blocked, reason = self._is_blocked_path(path)
        if blocked:
            return f"访问被拒绝: {reason}"
        p = self._safe_path(path)
        if not p.exists():
            return f"文件不存在: {path}"
        content = p.read_text(encoding="utf-8")
        if len(content) > 8000:
            return content[:8000] + f"\n... (截断，共 {len(content)} 字符)"
        return content

    def _tool_read_multi(self, paths: list[str]) -> str:
        """批量读文件，一次返回所有内容。减少 API 往返次数。"""
        if not paths:
            return "未指定文件路径"
        results = []
        total_chars = 0
        max_total = 16000
        for path in paths:
            blocked, reason = self._is_blocked_path(path)
            if blocked:
                results.append(f"### {path}\n访问被拒绝: {reason}\n")
                continue
            p = self._safe_path(path)
            if not p.exists():
                results.append(f"### {path}\n(不存在)\n")
                continue
            try:
                content = p.read_text(encoding="utf-8")
                if total_chars + len(content) > max_total:
                    remain = max_total - total_chars
                    content = content[:remain] + f"\n... (截断，共 {len(content)} 字符)"
                results.append(f"### {path}\n{content}\n")
                total_chars += len(content)
            except Exception as e:
                results.append(f"### {path}\n读取错误: {e}\n")
        return "\n".join(results) if results else "未读取到任何文件"

    def _tool_write(self, path: str, content: str) -> str:
        blocked, reason = self._is_blocked_path(path)
        if blocked:
            return f"写入被拒绝: {reason}"
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self._changed_files.append(str(p.relative_to(self._cwd)))
        return f"已写入 {path} ({len(content)} 字符)"

    def _tool_run(self, command: str) -> str:
        dangerous, reason = self._is_dangerous_command(command)
        if dangerous:
            return f"命令被拦截: {reason}"
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"命令解析失败: {e}"
        if not argv:
            return "空命令"
        # ponytail: 合并 agent env 到局部环境, 不污染 os.environ
        merged = {**os.environ, **getattr(self, '_agent_env', {})}
        safe_env = {k:v for k,v in merged.items() if not any(p in k.upper() for p in ("API_KEY","TOKEN","SECRET","PASSWORD","AUTH","CREDENTIAL","CERT"))}
        try:
            r = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=30, cwd=str(self._cwd), env=safe_env)
            out = r.stdout[-4000:] if r.stdout else ""
            err = r.stderr[-2000:] if r.stderr else ""
            return f"exit={r.returncode}\nstdout:\n{out}\nstderr:\n{err}"
        except FileNotFoundError:
            return f"命令不存在: {argv[0]}"
        except subprocess.TimeoutExpired:
            return "命令超时 (30s)"

    def _tool_search(self, pattern: str, path: str = "") -> str:
        search_dir = self._safe_path(path) if path else self._cwd
        try:
            results = []
            for f in search_dir.rglob("*.py"):
                if ".qidian" in str(f) or "venv" in str(f) or "__pycache__" in str(f):
                    continue
                try:
                    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                        if re.search(pattern, line):
                            results.append(f"{f.relative_to(self._cwd)}:{i}: {line.strip()[:120]}")
                            if len(results) > 20:
                                return "\n".join(results) + "\n... (截断)"
                except Exception:
                    pass
            return "\n".join(results) if results else f"未找到匹配 '{pattern}' 的行"
        except Exception as e:
            return f"搜索错误: {e}"

    def _track_changed_files(self):
        """通过 git diff 追踪改动的文件。"""
        try:
            r = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, cwd=str(config.PROJECT_ROOT),
            )
            if r.returncode == 0:
                for f in r.stdout.strip().splitlines():
                    if f and f not in self._changed_files:
                        self._changed_files.append(f)
        except Exception:
            try: witness.heartbeat('oa_exec', 'warn')
            except Exception: pass

    # ── API 调用 ──

    def _api_call(self, body: dict) -> dict:
        """通过 httpx 连接池调用 API。复用 TCP 连接，自动重试。"""
        client = _get_http_client()
        try:
            resp = client.post(
                self._url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException:
            raise _NetworkError("超时")
        except httpx.ConnectError as e:
            raise _NetworkError(f"连接失败: {e}")
        except Exception as e:
            raise _NetworkError(f"网络错误: {e}")

        if resp.status_code == 429:
            raise _RateLimitError()
        if resp.status_code >= 400:
            err_text = resp.text[:500] if resp.text else ""
            raise _FormatError(f"HTTP {resp.status_code}: {err_text}")

        try:
            data = resp.json()
            if data.get("error"):
                raise _FormatError(f"API错误: {data['error']}")
            return data
        except json.JSONDecodeError as e:
            raise _FormatError(f"JSON解析失败: {e}")


# ── 全局 httpx 客户端 (连接池复用) ──

_HTTPX_CLIENT: "Optional[httpx.Client]" = None


def _get_http_client() -> httpx.Client:
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _HTTPX_CLIENT = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=15.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            follow_redirects=True,
            verify=False,  # ponytail: 国内API(DashScope/Moonshot) SSL兼容性
        )
    return _HTTPX_CLIENT


# ── 模块级工具函数 (给 AnthropicApiExecutor 复用, 避免代码重复) ──

def _read_file(args: dict, cwd) -> str:
    """模块级单文件读取。Anthropic executor 用。"""
    path = args.get("path", "")
    if not path:
        return "请指定 path"
    p = (cwd / path).resolve()
    if not p.exists():
        return f"文件不存在: {path}"
    content = p.read_text(encoding="utf-8")
    if len(content) > 8000:
        return content[:8000] + f"\n... (截断，共 {len(content)} 字符)"
    return content

def _read_files(args: dict, cwd) -> str:
    """模块级批量文件读取。支持 path(单文件) + paths(批量)。"""
    paths = list(args.get("paths", []) or [])
    if args.get("path"):
        paths.append(args["path"])
    if not paths:
        return "请指定 path 或 paths"
    results = []
    for path in paths:
        p = (cwd / path).resolve()
        if not p.exists():
            results.append(f"### {path}\n(不存在)\n")
            continue
        try:
            content = p.read_text(encoding="utf-8")
            if len(content) > 8000:
                content = content[:8000] + f"\n... (截断，共 {len(content)} 字符)"
            results.append(f"### {path}\n{content}\n")
        except Exception as e:
            results.append(f"### {path}\n错误: {e}\n")
    return "\n".join(results) if results else "未读取到任何文件"

def _write_file(args: dict, cwd, blocked_patterns) -> str:
    """模块级写文件。"""
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "请指定 path"
    p = (cwd / path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path} ({len(content)} 字符)"

def _run_command(args: dict, cwd) -> str:
    """模块级命令执行。"""
    import subprocess, shlex
    cmd = args.get("command", "")
    if not cmd:
        return "请指定 command"
    try:
        argv = shlex.split(cmd)
        r = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=30, cwd=str(cwd))
        out = r.stdout[-4000:] if r.stdout else ""
        err = r.stderr[-2000:] if r.stderr else ""
        return f"exit={r.returncode}\nstdout:\n{out}\nstderr:\n{err}"
    except Exception as e:
        return f"命令错误: {e}"

def _search_code(args: dict, cwd) -> str:
    """模块级代码搜索。"""
    import re
    pattern = args.get("pattern", "")
    path = args.get("path", "")
    if not pattern:
        return "请指定 pattern"
    search_dir = (cwd / path).resolve() if path else cwd
    try:
        results = []
        for f in search_dir.rglob("*.py"):
            if ".qidian" in str(f) or "venv" in str(f) or "__pycache__" in str(f):
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if re.search(pattern, line):
                        results.append(f"{f.relative_to(cwd)}:{i}: {line.strip()[:120]}")
                        if len(results) > 20:
                            return "\n".join(results) + "\n... (截断)"
            except Exception:
                pass
        return "\n".join(results) if results else f"未找到匹配 '{pattern}' 的行"
    except Exception as e:
        return f"搜索错误: {e}"

# ── 错误类型 ──

class _RateLimitError(Exception): pass
class _FormatError(Exception): pass
class _NetworkError(Exception): pass
