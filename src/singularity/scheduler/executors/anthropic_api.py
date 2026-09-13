"""Anthropic Messages API executor — 直连 Anthropic API，去 claude-cli 硬编码路径依赖。

API format: POST https://api.anthropic.com/v1/messages
Docs: https://docs.anthropic.com/en/api/messages
"""

from __future__ import annotations

import json, os, time
from pathlib import Path

from singularity.scheduler.executors.base import BaseExecutor, ExecutorResult
from singularity.scheduler import config


# ── Constants ──────────────────────────────────────────────────────────
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"


class AnthropicApiExecutor(BaseExecutor):
    """Anthropic Messages API executor with tool use support."""

    honors_no_tools = True
    has_tool_surface = True      # `_execute_tool` 是本进程分发的 ⇒ 权限闸门在这儿

    def run(self) -> ExecutorResult:
        import subprocess as _sp

        api_key_env = self.cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            return ExecutorResult(success=False,
                                  error=f"API key not set: {api_key_env}",
                                  error_kind="exec")

        base_url = self.cfg.get("entry", ANTHROPIC_BASE_URL)
        tmpl = self.cfg.get("request_template", {})
        model = tmpl.get("model") or self.cfg.get("model", "claude-sonnet-4-6")
        max_tokens = tmpl.get("max_tokens", 4096)
        max_turns = self.cfg.get("max_turns", 10)

        import httpx

        # ── Build tools (convert OpenAI format to Anthropic format) ──
        # 架构/规划类调用(no_tools)禁工具：和 openai_agent 对齐，否则委员会"禁工具"
        # 的承诺对 anthropic-api 类型的成员不成立（工具照注入，模型可能直接改磁盘）
        no_tools = bool(self.cfg.get("no_tools"))
        if no_tools:
            anthropic_tools = []
            system_prompt = _DEFAULT_SYSTEM_NO_TOOLS
        else:
            anthropic_tools = self._convert_tools(self._skill_tools + self._mcp_tools)
            system_prompt = _DEFAULT_SYSTEM
        if self._skill_prompt:
            system_prompt += "\n" + self._skill_prompt
            if no_tools:
                # 技能提示词可能要求跑命令（如 archify 的 node 渲染器）—— 禁工具时
                # 这会诱导模型吐假 tool_call。把禁令放最后压住它。
                system_prompt += (
                    "\n\n[重要] 本次调用已禁用所有工具：不要调用工具、不要执行命令、"
                    "不要输出 tool_calls，直接输出最终文本。"
                )

        messages = [{"role": "user", "content": self.task}]
        tool_events = []

        start = time.time()
        total_tokens = 0
        # ⚠️ **消费调用方给的预算**（2026-09-14 核外派「改动审阅」）：`budget_s` 是
        # "这次 dispatch 还能花多少秒"（按任务死线倒推）。不理会它，单次请求的硬上限
        # 会越过任务死线 —— 外面那把 900s 的刀照样无声收割（同 §67）。取 min 保住原上限；
        # 下界 1.0s（预算跑光时不该再发请求）。
        _tmo_base = config.CLAUDE_CLI_TIMEOUT
        _deadline_at = (start + self.budget_s) if self.budget_s is not None else None

        for turn in range(1, max_turns + 1):
            # 🔴 **必须逐轮算，不能在循环外算一次**（2026-09-14，外派 K 条4 / E'③）：
            # 原来 `_tmo` 在循环外定死、每轮共用 ⇒ 最坏 `max_turns`（默认 10）倍预算，
            # 照样撞穿任务死线被 900s 无声收割。zhipu（`zhipu_api.py:61` 每次尝试前看
            # `_deadline`）和 openai_agent（`openai_agent.py:406/496` 每轮看表、
            # `:960-966` 连单次调用都按剩余封顶）都防了这一手，只有这份没防。
            _tmo = _tmo_base
            if _deadline_at is not None:
                left = _deadline_at - time.time()
                if left <= 0:
                    # 预算跑光就别再发请求了 —— 发了也是被外面那把刀砍，且时间已算在别人头上。
                    # ⚠️ **`error_kind` 必须是 `"deadline"`，不是 `"timeout"`**（2026-09-14，
                    # 逆向审抓到、我核过）：`_exec.py:589` 只对 `=="deadline"` 置
                    # `deadline_wrapup`（"别换模型，直接收，把已经拿到手的账留下"）；
                    # 返回 `"timeout"` 会落进"换 agent 容灾 + 重试满轮 → FAILED"，
                    # 而换一个模型只是把剩下的时间再烧一遍。
                    # `openai_agent.py:664` 的预算收尾用的就是 `"deadline"`（同一个信号）。
                    # **httpx 真超时那条仍然保持 `"timeout"`**（见下面 `except httpx.TimeoutException`）。
                    return ExecutorResult(success=False,
                                          error="到达执行预算，主动收尾（budget exhausted）",
                                          error_kind="deadline")
                _tmo = max(1.0, min(_tmo_base, left))

            body = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": messages,
            }
            if anthropic_tools:
                body["tools"] = anthropic_tools
            if "thinking" in tmpl:
                # 只透传不判语义：Claude 是 {type:"enabled", budget_tokens:N}，
                # 与 OpenAI 系的 {type:"disabled"} 形状不同，由配置自己写对
                body["thinking"] = tmpl["thinking"]

            try:
                resp = httpx.post(
                    base_url,
                    json=body,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                        "content-type": "application/json",
                    },
                    timeout=_tmo,
                )
                if resp.status_code == 429:
                    wait = 2 ** turn
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    return ExecutorResult(success=False,
                                          error=f"Anthropic API {resp.status_code}: {resp.text[:300]}",
                                          error_kind="exec")
                data = resp.json()
            except httpx.TimeoutException:
                return ExecutorResult(success=False, error="timeout", error_kind="timeout")
            except Exception as e:
                return ExecutorResult(success=False, error=str(e), error_kind="exec")

            # ── Token counting ──
            usage = data.get("usage", {})
            total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            # ── Process response ──
            content = data.get("content", [])
            text_parts = []
            tool_use_blocks = []

            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_use_blocks.append(block)
                elif block.get("type") == "thinking":
                    pass  # skip thinking blocks

            assistant_text = "\n".join(text_parts)

            # ── No tool calls → done ──
            if not tool_use_blocks:
                changed = self._get_changed_files()
                return ExecutorResult(
                    success=True,
                    raw_output=assistant_text,
                    changed_files=changed,
                    elapsed=time.time() - start,
                    token_count=total_tokens,
                    tool_events=tool_events,
                )

            # ── Execute tool calls ──
            messages.append({"role": "assistant", "content": content})
            tool_results = []

            for tb in tool_use_blocks:
                tool_name = tb.get("name", "")
                tool_input = tb.get("input", {})
                result = self._execute_tool(tool_name, tool_input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb.get("id", ""),
                    "content": result,
                })
                tool_events.append({
                    "tool": tool_name,
                    "status": "done" if not result.startswith("Error:") else "error",
                    "time": int(time.time()),
                })

            messages.append({"role": "user", "content": tool_results})

        # ── Max turns exhausted ──
        changed = self._get_changed_files()
        return ExecutorResult(
            success=True,
            raw_output=assistant_text if 'assistant_text' in dir() else "(max turns)",
            changed_files=changed,
            elapsed=time.time() - start,
            token_count=total_tokens,
            tool_events=tool_events,
        )

    def _convert_tools(self, openai_tools: list[dict]) -> list[dict]:
        """Convert OpenAI function-calling tool defs to Anthropic tool format."""
        result = []
        for t in openai_tools or []:
            func = t.get("function", {})
            result.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    def _execute_tool(self, name: str, args: dict) -> str:
        """Execute a tool call and return result text."""
        from singularity.scheduler.executors.base import _BLOCKED_PATTERNS
        from singularity.scheduler.executors.openai_agent import (
            _read_file, _read_files, _write_file, _run_command, _search_code,
        )
        try:
            # ── 权限闸门（2026-09-14 补）──
            # 这里原来是**自己重写的分发**、整个文件一个 `permission` 都没有：
            # 绑了 read-only / sandboxed 的 agent，只要 type 写成 anthropic-api，
            # 白名单 + 审批通道 + profile 拦截**整层静默消失**（界面照样显示"已绑定"）。
            # openai_agent 的分发早就有这一步，两套方法独立撞上同一处 → 实锤。
            allowed, reason = self._check_permission(name, args)
            if not allowed:
                return f"操作被拒绝: {reason}"
            if name == "read_file":
                # 支持批量读 (paths参数)
                if args.get("paths"):
                    return _read_files(args, self.cwd)
                return _read_file(args, self.cwd)
            elif name == "write_file":
                return _write_file(args, self.cwd, _BLOCKED_PATTERNS)
            elif name == "run_command":
                return _run_command(args, self.cwd)
            elif name == "search_code":
                return _search_code(args, self.cwd)
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            return f"Error: {e}"

    def _get_changed_files(self) -> list[str]:
        """Get list of changed files via git diff.

        ⚠️ 原来这里是 `self.baseline_ref or "HEAD"` —— 拿不到基线就**静默**退化成
        跟 HEAD 比。而 agent 自己 `git commit` 之后跟 HEAD 比是**恒空**的
        （`git commit` 不在 `base.py` 的 `_BLOCKED_COMMANDS` 里）⇒ changed_files 空
        ⇒ 下游审查/QA/安全审计整条被跳过。防御模式 §55 的同形状，第三次。
        现在：拿不到基线就**出声**（降级仍然发生，但不再静默）。
        """
        import subprocess as _sp
        if not self.baseline_ref:
            try:
                from singularity.scheduler import witness as _w
                _w.warn('anthropic_exec', 'collect_changes:no_baseline_ref（退化成跟 HEAD 比，已提交的改动看不见）'[:200])
            except Exception:
                pass
        try:
            r = _sp.run(
                ["git", "diff", "--name-only", self.baseline_ref or "HEAD"],
                cwd=str(self.cwd or config.PROJECT_ROOT),
                capture_output=True, text=True, timeout=30,
            )
            files = [f for f in r.stdout.strip().splitlines() if f and not f.startswith(".qidian/")]
            r2 = _sp.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=str(self.cwd or config.PROJECT_ROOT),
                capture_output=True, text=True, timeout=30,
            )
            new_files = [f for f in r2.stdout.strip().splitlines() if f and not f.startswith(".qidian/")]
            return list(set(files + new_files))
        except Exception:
            return []


_DEFAULT_SYSTEM = """You are a code engineering agent. You have access to tools for reading/writing files and running commands.
Always use the tools to make concrete changes. Do not leave TODO comments or placeholder implementations.
Output complete, working code. When done, just output the final result without further tool calls."""

# no_tools 时用这份：上面那份明确要求"必须用工具改文件"，禁了工具还留着等于误导模型
_DEFAULT_SYSTEM_NO_TOOLS = """You are a code engineering agent. Tools are disabled for this call —
output your answer directly as text. Do not leave TODO comments or placeholder implementations."""
