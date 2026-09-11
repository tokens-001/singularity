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
    is_blocked_path, is_dangerous_command)
from singularity.scheduler import witness
from singularity.scheduler import config
from singularity.scheduler._types import _pending_sse_events

# ── 重试策略（Temporal 五字段语义，见 _api_call）──
# 以前网络错误/超时一次就判任务失败 —— 一次抖动整轮白跑。
_RETRY_INITIAL = float(os.environ.get("QIDIAN_RETRY_INITIAL", "1"))        # 首次重试间隔
_RETRY_COEFF = float(os.environ.get("QIDIAN_RETRY_COEFF", "2.0"))          # 退避系数
_RETRY_MAX_INTERVAL = float(os.environ.get("QIDIAN_RETRY_MAX_INTERVAL", "60"))
_RETRY_MAX_ATTEMPTS = int(os.environ.get("QIDIAN_RETRY_MAX_ATTEMPTS", "3"))
# 整轮预算（schedule-to-close）：重试总耗时上限，防止 3×240s 撞穿 orchestrator 的 900s deadline
_RETRY_TOTAL_BUDGET = float(os.environ.get("QIDIAN_RETRY_TOTAL_BUDGET", "600"))
# 流式 + 停滞检测。read timeout = 多久没新 token 就断开（真中断，不用杀进程）。
# 非流式只能干等整体 240s 超时，且线程 join 不掉 —— 见 _dispatch_exec 顶部注释。
_STREAM = os.environ.get("QIDIAN_STREAM", "1") != "0"
_STALL_TIMEOUT = float(os.environ.get("QIDIAN_STALL_TIMEOUT", "90"))
# 进度上流节流：多久推一条 "生成中 N 字" 到前端（0 = 关）
_PROGRESS_INTERVAL = float(os.environ.get("QIDIAN_PROGRESS_INTERVAL", "1.0"))

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
- 对照需求完整实现：功能要全覆盖，需求里的软性要求（风格/响应式/空态/异常态/移动端适配）也要做到，别只写最小可运行版
- 跑命令一律用 `python3` 开头（本机没有 `python` 命令，敲 `python` 会 command not found）；跑测试固定 `python3 -m pytest -q`
- 分析/调研/总结类任务：第一轮直接输出答案，不调用工具
- 任务完成时，必须在输出末尾附加 [HANDOFF] 块，格式如下:
  [HANDOFF]
  deliverable: <产出文件路径或描述>
  conclusion: <关键结论，一句话>
  next: <建议下一个 Agent，如 Coding/QA/Review/None>
  human_confirm: <true/false，是否需要人工确认>"""

# no_tools 时用这份：上面那份写着"你有工具/直接写代码别输出方案"，
# 委员会这类"禁工具、只输出方案 JSON"的调用用它会被带偏。
SYSTEM_PROMPT_NO_TOOLS = """你是Singularity Dispatch的 AI Agent。

本次调用已禁用全部工具：你不能读写文件、不能执行命令、不能搜索代码。

规则:
- 不要调用工具，不要输出 tool_calls 或 <invoke> 块
- 直接按用户要求输出最终文本（如架构方案 JSON），不要输出"我先做X再做Y"的过程叙述
- 不要以 [HANDOFF] 块结尾（那是执行类任务的格式）"""


def force_output_at(max_turns: int, max_tool_turns: int) -> int:
    """第几轮该撤掉工具、逼模型直接出终答。**必须早于最后一轮**。

    注入完那条"必须直接输出最终答案"的系统消息就 `continue` —— 若它正好是最后一轮，
    循环当场结束，**那次模型调用从未发生**，raw_output 只剩占位串
    `"(达到最大工具轮次, 已产出文件)"`。实测（2026-09-11 探路轮）：max_turns=5 /
    max_tool_turns=3 → 原判据 `3+2=5` 恰好等于 max_turns，于是文件全写出来了，
    交付报告里却一个字总结都没有。
    """
    return max(2, min(max_tool_turns + 2, max_turns - 1))


# 思考相关参数白名单。各家键名/取值都不同（DeepSeek/Kimi/智谱用 thinking，
# GLM-5.3 与 DeepSeek 用 reasoning_effort，Qwen/Kimi 兼容写法用 enable_thinking），
# 且支持面会变（GLM-5.2 能关、5.3 强制开；k2.6 能关、k2.7 强制开）。
# 所以只做透传，**不维护"谁支持什么"的能力表** —— 那表一定会过期。
_THINK_KEYS = ("thinking", "reasoning_effort", "enable_thinking")


def _apply_think_params(body: dict, tmpl: dict, skip: set | None = None) -> None:
    """把 request_template 里的思考参数原样透传进 body。配了就传，不判定支持与否。

    skip 放已被 API 拒过的键（body 每轮重建，不记就每轮重撞一次 400）。
    """
    for k in _THINK_KEYS:
        if k in tmpl and k not in (skip or ()):
            body[k] = tmpl[k]


def _drop_rejected_think_param(body: dict, err: str) -> str:
    """400 里提到某个思考参数 → 从 body 摘掉并返回键名（没有则 ""）。

    一次只摘一个：错误通常只报第一个不认识的参数，剩下的下一轮再摘。
    """
    low = err.lower()
    for k in _THINK_KEYS:
        if k in body and k in low:
            del body[k]
            return k
    return ""


class OpenAIAgentExecutor(BaseExecutor):
    """通用 Agent Executor — 给任何 OpenAI 兼容模型装上工具。"""

    honors_no_tools = True

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
        self._cwd = (Path(cwd) if cwd else config.PROJECT_ROOT).resolve()  # resolve 掉 /tmp→/private/tmp 等符号链接, 否则 write_file 的 relative_to 会炸
        self._changed_files: list[str] = []
        self._tool_events: list[dict] = []
        # body 每轮重建，被 API 拒过的思考参数要记住，否则下一轮又加回来、又撞一次 400
        self._rejected_think_keys: set[str] = set()
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
        # 架构/规划类任务(no_tools)禁工具: 模型直接输出文本, 不被 write_file/run_command 带偏
        no_tools = bool(self.cfg.get("no_tools"))
        if no_tools:
            tools = []
            # 只清空 tools 不够: 通用 SYSTEM_PROMPT 说"你有工具/直接写代码别输出方案"，
            # 与"输出架构 JSON"直接冲突 → 模型去够工具、吐出假 tool_call 就结束
            # （实测委员会初稿只剩 320 字）。
            system_prompt = SYSTEM_PROMPT_NO_TOOLS
        else:
            tools = list(TOOLS)
            tools.extend(self._skill_tools)
            tools.extend(self._mcp_tools)
            system_prompt = SYSTEM_PROMPT
        if self._skill_prompt:
            system_prompt += "\n" + self._skill_prompt
            if no_tools:
                # 技能提示词可能要求跑命令（如 archify 的 node 渲染器）—— 禁工具时
                # 这会诱导模型吐假 tool_call。把禁令放最后压住它。
                system_prompt += (
                    "\n\n[重要] 本次调用已禁用所有工具：不要调用工具、不要执行命令、"
                    "不要输出 tool_calls，直接输出最终文本。"
                )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.task},
        ]
        total_tokens = 0
        tool_turns = 0          # 连续工具调用轮数
        last_tool_calls = ""     # 上一轮工具调用指纹 (去重)
        max_tool_turns = self.cfg.get("max_tool_turns", 3)
        _force_at = force_output_at(self._max_turns, max_tool_turns)

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
                _apply_think_params(body, tmpl, self._rejected_think_keys)
            else:
                body = {
                    "model": self._model,
                    "messages": messages,
                    "tools": tools,
                    # tools 清空(测试通过即停/死循环强制输出)后别再 required, 否则 API 拒收空 tools + required
                    "tool_choice": "required" if tools else "auto",
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
                _apply_think_params(body, tmpl, self._rejected_think_keys)

            try:
                resp_data = self._api_call(body)
            except _RateLimitError:
                time.sleep(min(2 ** turn, 60))
                continue
            except _FormatError as e:
                # thinking 模型(DeepSeek V4 等)不接受 tool_choice=required → 降级 auto 重试一次
                if body.get("tool_choice") == "required" and "tool_choice" in str(e):
                    body["tool_choice"] = "auto"
                    # 光说不做防护: auto 模式 thinking 模型可能只回文字不调工具 → 注入强制工具指令
                    messages.append({
                        "role": "system",
                        "content": "[系统] 本任务必须调用工具完成：写代码用 write_file，跑命令用 run_command。禁止只输出文字描述或计划，必须实际调用工具产出文件。",
                    })
                    try:
                        resp_data = self._api_call(body)
                    except _RateLimitError:
                        time.sleep(min(2 ** turn, 60))
                        continue
                    except (_NetworkError, _FormatError) as e2:
                        return ExecutorResult(success=False, error=str(e2),
                                              error_kind="exec", elapsed=time.time() - start,
                                              tool_events=list(self._tool_events))
                elif (bad := _drop_rejected_think_param(body, str(e))):
                    # 该模型不吃这个思考参数（各家支持面不同且会变）→ 摘掉重试一次，
                    # 并记住键名（body 每轮重建，不记就每轮再撞一次 400）
                    self._rejected_think_keys.add(bad)
                    witness.warn("oa_exec",
                                 f"think_param_rejected:{self._model}:{bad}:{str(e)[:60]}"[:150])
                    try:
                        resp_data = self._api_call(body)
                    except _RateLimitError:
                        time.sleep(min(2 ** turn, 60))
                        continue
                    except (_NetworkError, _FormatError) as e2:
                        return ExecutorResult(success=False, error=str(e2),
                                              error_kind="exec", elapsed=time.time() - start,
                                              tool_events=list(self._tool_events))
                else:
                    return ExecutorResult(success=False, error=str(e),
                                          error_kind="exec", elapsed=time.time() - start,
                                          tool_events=list(self._tool_events))
            except _NetworkError as e:
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
                tests_passed = False
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
                    evt_start = {
                        "kind": "tool:start",
                        "tool": name,
                        "task_id": self.task_id,
                        "ts": t_start,
                        "msg": f"🔧 {name}",
                    }
                    self._tool_events.append(evt_start)
                    _pending_sse_events.append(evt_start)  # 实时上流: 前端任务卡滚动日志
                    # ── 执行工具 ──
                    result = self._execute_tool(name, args)
                    if name == "run_command" and self._tests_green(args.get("command", ""), result):
                        tests_passed = True
                    # ── 工具事件: 记录完成 ──
                    t_done = time.time()
                    result_preview = result[:120] if len(result) > 120 else result
                    evt_done = {
                        "kind": "tool:done",
                        "tool": name,
                        "task_id": self.task_id,
                        "ts": t_done,
                        "elapsed": round(t_done - t_start, 3),
                        "result_preview": result_preview,
                        "result_len": len(result),
                        "msg": f"✅ {name} ({len(result)}字符, {round(t_done-t_start,2)}s)",
                    }
                    self._tool_events.append(evt_done)
                    _pending_sse_events.append(evt_done)  # 实时上流
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                # 测试通过即停: 跑测试全绿 → 强制输出, 治 thinking 模型反复测不收敛(超900s)
                if tests_passed:
                    tools = []
                    messages.append({
                        "role": "system",
                        "content": "[系统] 测试已全部通过，代码已完成。停止调用工具，直接输出最终答案。",
                    })
                    continue

                # 死循环检测
                tool_turns += 1
                call_fingerprint = str([(tc.get("function", {}).get("name", ""),
                                        tc.get("function", {}).get("arguments", "")[:80])
                                        for tc in tool_calls])
                if tool_turns >= _force_at:
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

        # 达到最大轮次: 模型可能已写文件但没输出终答 → 追踪 changed_files, 有文件就算产出
        self._track_changed_files()
        if self._changed_files:
            return ExecutorResult(
                success=True, raw_output="(达到最大工具轮次, 已产出文件)",
                changed_files=list(self._changed_files),
                elapsed=time.time() - start, token_count=total_tokens,
                tool_events=list(self._tool_events))
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
        return is_blocked_path(path)

    def _is_dangerous_command(self, command: str) -> tuple[bool, str]:
        """检查 shell 命令是否危险。返回 (dangerous, reason)。"""
        return is_dangerous_command(command)

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
        if not command.strip():
            return "空命令"
        # ponytail: 合并 agent env 到局部环境, 不污染 os.environ
        merged = {**os.environ, **getattr(self, '_agent_env', {})}
        safe_env = {k:v for k,v in merged.items() if not any(p in k.upper() for p in ("API_KEY","TOKEN","SECRET","PASSWORD","AUTH","CREDENTIAL","CERT"))}
        try:
            # shell=True: 支持 && | source 等 shell 语法 (shell=False 会把 &&/source 当参数生成垃圾目录)。
            # 安全性靠 _is_dangerous_command 黑名单前置拦截 (rm -rf/curl/python -c/bash -c 等)
            r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30, cwd=str(self._cwd), env=safe_env)
            out = r.stdout[-4000:] if r.stdout else ""
            err = r.stderr[-2000:] if r.stderr else ""
            return f"exit={r.returncode}\nstdout:\n{out}\nstderr:\n{err}"
        except subprocess.TimeoutExpired:
            return "命令超时 (30s)"

    def _tests_green(self, command: str, result: str) -> bool:
        """run_command 跑了测试且 exit=0 → 测试通过。治 thinking 模型反复测不收敛撞 900s。"""
        cmd = command.lower()
        # 只认真正的测试调用。原先的子串匹配会把 `grep -r test src` / `ls test_data`
        # / `cat test.log` 当成"测试通过" → 误清工具列表, 任务被提前截断
        if not re.search(r"\b(?:pytest|unittest)\b|\bnpm\s+(?:run\s+)?test\b", cmd):
            return False
        return "exit=0" in result

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
        """通过 git status 追踪改动的文件 (含 untracked 新文件, 修复 #1)。

        git diff --name-only 漏掉 untracked 新文件 (模型用 run_command heredoc 写的新文件),
        改用 git status --porcelain 全覆盖。
        """
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(self._cwd), timeout=15,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    # 格式: "XY path" (X=index, Y=worktree), 重命名 "R  old -> new"
                    f = line[3:].split(" -> ")[-1].strip()
                    # 过滤构建产物 (__pycache__/.pyc), 不算交付文件
                    if not f or f in self._changed_files:
                        continue
                    if "__pycache__" in f or f.endswith((".pyc", ".pyo")):
                        continue
                    self._changed_files.append(f)
        except Exception as e:
            try: witness.warn('oa_exec', f'collect_changes:{e}'[:80])
            except Exception: pass

    # ── API 调用 ──

    def _api_call(self, body: dict) -> dict:
        """带分层重试的 API 调用（Temporal 五字段语义）。

        - 单次尝试上限 240s（start-to-close，见 _get_http_client 的 httpx.Timeout）
        - 整轮预算 600s（schedule-to-close）：超预算不再重试，避免 3×240s 撞穿 900s deadline
        - 重试间隔 = initial × coeff^(n-1)，封顶 maximum_interval
        - 只重试**瞬时**错误（网络中断 / 超时 / 5xx）；4xx 不重试 —— 重试也不会好
        - 429 交给外层循环（它按对话轮次退避），这里不吞
        """
        deadline = time.time() + _RETRY_TOTAL_BUDGET
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                return self._api_call_once(body)
            except (_NetworkError, _TransientError):
                if attempt >= _RETRY_MAX_ATTEMPTS or time.time() >= deadline:
                    raise
                time.sleep(min(_RETRY_INITIAL * _RETRY_COEFF ** (attempt - 1), _RETRY_MAX_INTERVAL))
        raise AssertionError("unreachable")

    def _api_call_once(self, body: dict) -> dict:
        """单次 API 调用。异常分类见 _api_call 的文档。

        默认走流式（QIDIAN_STREAM=0 关）：只有流式才能做**停滞检测** ——
        read timeout 就是"多久没有新 token"的上限，超时即断开连接，生成真的停。
        非流式只能干等 240s 整体超时，而且线程 join 不掉（见 _dispatch_exec 顶部注释）。
        """
        if _STREAM:
            return self._stream_call(body)
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

        self._raise_for_status(resp)

        try:
            data = resp.json()
            if data.get("error"):
                raise _FormatError(f"API错误: {data['error']}")
            # 别名对账：请求名和返回的 model 不一致，说明厂商把旧名路由到新模型了
            # （DeepSeek 2026-09-14 起 deepseek-v4-pro 全部路由到 V4.1-Flash）。
            # 记下来供委员会去重 —— 否则两个别名会占两个席位、自己跟自己碰。
            try:
                from .. import api_store
                api_store.record_alias(self._model, data.get("model") or "")
            except Exception:
                pass   # 记账失败不能影响这次调用
            return data
        except json.JSONDecodeError as e:
            raise _FormatError(f"JSON解析失败: {e}")

    def _raise_for_status(self, resp) -> None:
        """状态码 → 异常分类（流式/非流式共用）。欠费顺手标记 provider。"""
        if resp.status_code >= 400:
            try:
                from .. import api_store
                api_store.note_api_error(self._model, resp.status_code, resp.text or "")
            except Exception:
                pass  # 标记失败不能盖掉真正的 HTTP 错误
        if resp.status_code == 429:
            raise _RateLimitError()
        if resp.status_code >= 500:
            raise _TransientError(f"HTTP {resp.status_code}: {resp.text[:200] if resp.text else ''}")
        if resp.status_code >= 400:
            raise _FormatError(f"HTTP {resp.status_code}: {resp.text[:500] if resp.text else ''}")

    def _stream_call(self, body: dict) -> dict:
        """流式调用，把 delta 拼回与非流式同形状的响应。

        read timeout = _STALL_TIMEOUT：超过这么久没有新 token 就抛 ReadTimeout，
        `with client.stream(...)` 退出即关闭连接 —— 这是不靠杀进程的"真中断"。
        """
        client = _get_http_client()
        payload = dict(body, stream=True, stream_options={"include_usage": True})
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        content, reasoning = [], []
        tool_calls, finish, usage = {}, "", {}
        try:
            with client.stream("POST", self._url, json=payload, headers=headers,
                               timeout=httpx.Timeout(240.0, connect=15.0, read=_STALL_TIMEOUT)) as resp:
                if resp.status_code >= 400:
                    resp.read()                      # 先取回 body 才能读 .text
                    self._raise_for_status(resp)
                emitted, last_emit = 0, time.time()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk_str = line[5:].strip()     # 容忍 "data:{...}" 无空格
                    if chunk_str == "[DONE]":
                        break
                    chunk = json.loads(chunk_str)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices", []) or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"])
                            emitted += len(delta["content"])
                            now = time.time()
                            if now - last_emit >= _PROGRESS_INTERVAL:
                                # 进度上流：节流后再推，否则每个 token 一条会淹掉 SSE
                                last_emit = now
                                _pending_sse_events.append({
                                    "kind": "gen", "task_id": self.task_id,
                                    "msg": f"✍️ 生成中 {emitted} 字…", "ts": now,
                                })
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = tool_calls.setdefault(tc.get("index", 0), {
                                "id": "", "type": "function",
                                "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["function"]["arguments"] += fn["arguments"]
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
        except httpx.TimeoutException:
            # read timeout = 流停滞（不是整体超时）—— 报清楚，方便区分
            raise _NetworkError(f"流停滞 {_STALL_TIMEOUT:.0f}s 无新 token")
        except httpx.HTTPError as e:
            raise _NetworkError(f"网络错误: {e}")

        msg = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if tool_calls:
            msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}


# ── 全局 httpx 客户端 (连接池复用) ──

_HTTPX_CLIENT: "Optional[httpx.Client]" = None


def _get_http_client() -> httpx.Client:
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _HTTPX_CLIENT = httpx.Client(
            timeout=httpx.Timeout(240.0, connect=15.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            follow_redirects=True,
            verify=False,  # ponytail: 国内API(DashScope/Moonshot) SSL兼容性
        )
    return _HTTPX_CLIENT


# ── 模块级工具函数 (给 AnthropicApiExecutor 复用, 避免代码重复) ──

def _safe_path_at(cwd: Path, path: str) -> Path:
    """模块级路径安全：解析路径，禁止逃出 cwd（与类方法 _safe_path 等价）。"""
    p = (cwd / path).resolve()
    root = cwd.resolve()
    if not (str(p) + os.sep).startswith(str(root) + os.sep) and str(p) != str(root):
        raise ValueError(f"路径逃逸被拒绝: {path}")
    return p

def _is_blocked_path_at(path: str) -> tuple[bool, str]:
    """模块级 blocklist 检查（与类方法 _is_blocked_path 等价）。返回 (blocked, reason)。"""
    return is_blocked_path(path)

def _is_dangerous_command_at(command: str) -> tuple[bool, str]:
    """模块级危险命令检查（与类方法 _is_dangerous_command 等价）。返回 (dangerous, reason)。"""
    return is_dangerous_command(command)

def _read_file(args: dict, cwd) -> str:
    """模块级单文件读取。Anthropic executor 用。"""
    path = args.get("path", "")
    if not path:
        return "请指定 path"
    blocked, reason = _is_blocked_path_at(path)
    if blocked:
        return f"访问被拒绝: {reason}"
    p = _safe_path_at(cwd, path)
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
        blocked, reason = _is_blocked_path_at(path)
        if blocked:
            results.append(f"### {path}\n访问被拒绝: {reason}\n")
            continue
        try:
            p = _safe_path_at(cwd, path)
        except ValueError as e:
            results.append(f"### {path}\n{e}\n")
            continue
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
    blocked, reason = _is_blocked_path_at(path)
    if blocked:
        return f"写入被拒绝: {reason}"
    p = _safe_path_at(cwd, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path} ({len(content)} 字符)"

def _run_command(args: dict, cwd) -> str:
    """模块级命令执行。"""
    import subprocess, shlex
    cmd = args.get("command", "")
    if not cmd:
        return "请指定 command"
    dangerous, reason = _is_dangerous_command_at(cmd)
    if dangerous:
        return f"命令被拦截: {reason}"
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
    try:
        search_dir = _safe_path_at(cwd, path) if path else cwd
    except ValueError as e:
        return f"搜索错误: {e}"
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
class _TransientError(Exception): pass     # 5xx —— 可重试（429 由 _RateLimitError 单独走）
