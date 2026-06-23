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

    def run(self) -> ExecutorResult:
        import subprocess as _sp

        api_key_env = self.agent_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            return ExecutorResult(success=False,
                                  error=f"API key not set: {api_key_env}",
                                  error_kind="exec")

        base_url = self.agent_cfg.get("entry", ANTHROPIC_BASE_URL)
        model = self.agent_cfg.get("request_template", {}).get("model") or self.agent_cfg.get("model", "claude-sonnet-4-6")
        max_tokens = self.agent_cfg.get("request_template", {}).get("max_tokens", 4096)
        max_turns = self.agent_cfg.get("max_turns", 10)

        import httpx

        # ── Build tools (convert OpenAI format to Anthropic format) ──
        anthropic_tools = self._convert_tools(self._skill_tools + self._mcp_tools)

        system_prompt = _DEFAULT_SYSTEM
        if self._skill_prompt:
            system_prompt += "\n" + self._skill_prompt

        messages = [{"role": "user", "content": self.task}]
        tool_events = []

        start = time.time()
        total_tokens = 0

        for turn in range(1, max_turns + 1):
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": messages,
            }
            if anthropic_tools:
                body["tools"] = anthropic_tools

            try:
                resp = httpx.post(
                    base_url,
                    json=body,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                        "content-type": "application/json",
                    },
                    timeout=config.CLAUDE_CLI_TIMEOUT,
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
        """Get list of changed files via git diff."""
        import subprocess as _sp
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
