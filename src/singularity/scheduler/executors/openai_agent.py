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
import subprocess
import time
from pathlib import Path

import httpx

from singularity.scheduler import config, witness
from singularity.scheduler._types import _pending_sse_events
from singularity.scheduler.executors.base import BaseExecutor, ExecutorResult, is_blocked_path, is_dangerous_command

# ── 重试策略（Temporal 五字段语义，见 _api_call）──
# 以前网络错误/超时一次就判任务失败 —— 一次抖动整轮白跑。
_RETRY_INITIAL = float(os.environ.get("QIDIAN_RETRY_INITIAL", "1"))        # 首次重试间隔
_RETRY_COEFF = float(os.environ.get("QIDIAN_RETRY_COEFF", "2.0"))          # 退避系数
_RETRY_MAX_INTERVAL = float(os.environ.get("QIDIAN_RETRY_MAX_INTERVAL", "60"))
_RETRY_MAX_ATTEMPTS = int(os.environ.get("QIDIAN_RETRY_MAX_ATTEMPTS", "3"))

# ── 子进程环境变量的脱敏 ────────────────────────────────────────
# 模型跑 `run_command` 时，`os.environ` 要脱敏后再给（`_tool_run`）。
_ENV_SENSITIVE_SUBSTR = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH",
                         "CREDENTIAL", "CERT")


def _is_sensitive_env(name: str) -> bool:
    """这个环境变量名算不算敏感（值不许给模型跑的命令看）。

    **按键名分段判，不按子串** —— 裸子串 `KEY` 会把 `MONKEY` / `KEYBOARD` 一起滤掉，
    那是**过度过滤**（会把模型跑的命令弄坏）。分段能抓住 `MY_KEY` / `SSH_KEY` /
    `AWS_SECRET_KEY` —— 它们才是原来漏掉的那类（原来只用上面那七个子串做子串匹配，
    `MY_KEY` 里没有 `API_KEY` ⇒ 直接放行）。
    ⚠️ **已知漏网**：`MYAPIKEY` 这种不靠下划线分词的名字抓不到 —— 这是本判据的天花板。
    """
    up = name.upper()
    if any(p in up for p in _ENV_SENSITIVE_SUBSTR):
        return True
    return "KEY" in up.split("_")


# ── XML 形式的工具调用 ─────────────────────────────────────────
# 有些模型不按 OpenAI 的 `tool_calls` 回，而是吐：
#   <tool_calls><invoke name="write_file"><parameter name="path">x.py</parameter>…
# 平台原来只认前者 → 这一整段被当成"模型的普通回答" → **文件一个字节都没落盘**，
# 而模型以为自己写成功了。2026-09-12 探路2 的 T3 就是这么"无文件改动"失败掉的
# （输出 9599 字，全是一块 <tool_calls><invoke>，changed_files 为空）。
# 讽刺的是 prompt 里早就写着"不要输出 <invoke> 块" —— **知道这个格式，却只有禁令、
# 没有解析器**。禁令挡不住换了模型/换了心情的那一次，所以这里把它接住。
_XML_INVOKE_RE = re.compile(r'<invoke\s+name=["\']([^"\']+)["\']\s*>(.*?)</invoke>', re.S)
_XML_PARAM_RE = re.compile(r'<parameter\s+name=["\']([^"\']+)["\']\s*>(.*?)</parameter>', re.S)

# ── DeepSeek 的 DSML ───────────────────────────────────────────
# **这不是"模型不听话"，是模型换了厂商的协议。** DeepSeek 系不回 OpenAI 的
# `tool_calls`，而是吐它自家的 DSML。分隔符是**两个全角竖线 U+FF5C**（不是 ASCII
# 的 `|`，肉眼几乎分不出来 —— 我先按 ASCII 写正则，拿真实输出一跑才发现对不上）。
# 探路2 的执行阵容里就有 deepseek-flash，所以只要轮到它，工具调用就可能整批蒸发。
_BAR = r"[|｜]{1,2}"          # 半角或全角、一根或两根，都认
_DSML_OPEN = "<" + _BAR + "DSML" + _BAR
_DSML_CLOSE = "</" + _BAR + "DSML" + _BAR
_DSML_HINT_RE = re.compile("<" + _BAR + "DSML", re.I)
_DSML_INVOKE_RE = re.compile(
    _DSML_OPEN + r"\s*invoke\s+name=\"([^\"]+)\"\s*>(.*?)"
    + _DSML_CLOSE + r"\s*invoke>", re.S)
_DSML_PARAM_RE = re.compile(
    _DSML_OPEN + r"\s*parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)"
    + _DSML_CLOSE + r"\s*parameter>", re.S)


def _looks_like_tool_markup(content: str) -> bool:
    """有没有"模型试图调工具"的痕迹（两种格式任一）。

    判据要**窄**：只认尖括号开头的标记。写成"文本里出现 invoke 这个词"的话，
    模型正常解释一句"I will invoke the tool"都会触发告警，把真信号淹了。
    """
    c = content or ""
    return ("<" + "invoke") in c or bool(_DSML_HINT_RE.search(c))


def _assistant_msg_for_history(msg: dict, tools: list) -> dict:
    """把 assistant 的这条回复放进历史时，**哪些字段该留**。

    ⚠️ **判据是 `tools` 空不空**，依据是 DeepSeek 官方 thinking-mode 文档：
      · **带 `tools`**：`reasoning_content` **必须在后续所有请求里原样回传**
        （含没有工具调用的轮次）——「must be fully passed back to the API in all
        subsequent requests」；**不回传就是 400**；
      · **不带 `tools`**：不必回传，传了也会被忽略 ⇒ **照旧剥掉**。

    原来这里对 `reasoning_content` 是**一刀切剥掉**，注释写着
    "推理模型(如Kimi/GLM)返回reasoning_content, API输入不接受此字段" —— **那是旧经验**。
    真机症状（2026-09-15）：`any 层所有 agent 均失败: deepseek-flash: 空输出
    [HTTP 400: The reasoning_content in the thinking mode must be passed back]`
    —— **换 agent 也没用**（同一个剥法）⇒ 整条任务挂掉。

    ⚠️ 不带 tools 的那条路（架构/规划的 `no_tools` 委员会）**保持剥掉** ——
    对 Kimi/GLM 维持原行为，**零回归**。
    """
    if tools:
        return dict(msg)
    return {k: v for k, v in msg.items() if k != "reasoning_content"}


def _parse_xml_tool_calls(content: str) -> list[dict] | None:
    """从 content 里捞出 XML 形式的工具调用 → OpenAI 那套结构。

    返回 None = 压根没这格式；返回 [] = **有**这格式但一条都没解析出来
    （调用方据此告警 —— "认出来了但没解析出来"和"没这格式"是两回事）。
    """
    if not _looks_like_tool_markup(content):
        return None
    out: list[dict] = []
    for regex, param_re in ((_DSML_INVOKE_RE, _DSML_PARAM_RE),
                            (_XML_INVOKE_RE, _XML_PARAM_RE)):
        for m in regex.finditer(content):
            name = (m.group(1) or "").strip()
            if not name:
                continue
            args = {k.strip(): v for k, v in param_re.findall(m.group(2) or "")}
            out.append({
                "id": f"xml_{len(out)}",
                "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)},
            })
    return out
# 整轮预算（schedule-to-close）：重试总耗时上限，防止 3×240s 撞穿 orchestrator 的 900s deadline
_RETRY_TOTAL_BUDGET = float(os.environ.get("QIDIAN_RETRY_TOTAL_BUDGET", "600"))


# 多慢算"慢"（秒）。**可配是为了测试**：写死 20 的话，测这条就得真跑 20 秒
# —— 而测试跑得慢的代价是整个仓的测试没人愿意跑（这不划算）。
_SLOW_CALL_LOG_S = float(os.environ.get("QIDIAN_SLOW_CALL_LOG_S", "20"))


def _slow_calls_path() -> Path:
    """慢调用的分诊账（`.qidian/llm_calls_slow.jsonl`）。

    ⚠️ 放在 `.qidian/` 下是**有意**的：那里**不在版本控制里**，是运行期产物，
    跟 `alerts.jsonl` / `partial_usage/` 一个性质。只记 ≥20s 的调用 ——
    全记的话这台账自己就是噪声，而"快的那些"直连已经量过了（0.1s 首字节）。
    """
    return config.QIDIAN_DIR / "llm_calls_slow.jsonl"
# 流式 + 停滞检测。**"停滞" = 多久没有可用进展**（content / reasoning / tool_calls），
# 不是"多久没有字节"—— 2026-09-16 把实现改成和这句注释一致（`_stream_call` 里有一长段）。
# read timeout = 多久读不到东西就断开（真中断，不用杀进程）。
# 非流式只能干等整体 240s 超时，且线程 join 不掉 —— 见 _dispatch_exec 顶部注释。
_STREAM = os.environ.get("QIDIAN_STREAM", "1") != "0"
_STALL_TIMEOUT = float(os.environ.get("QIDIAN_STALL_TIMEOUT", "90"))
# 执行器自查的总预算 = orchestrator 的收割上限 − 收尾余量（单一来源在 config）。
# **为什么执行器要自己看表**：以前它只转 max_turns 轮、一圈表都不看，唯一的上限
# 就是 orchestrator 到 900s 的**无声收割** —— 被杀就什么都留不下（token/文件/轮次全丢，
# 2026-09-13 复查 8003/8384/8386 定的案）。现在提前 TASK_WRAPUP_MARGIN_S 自己收尾，
# 把已知事实交回去。
# 环境变量可调：真机验证时设个小值就能几十秒内撞上这条路径。
_EXEC_BUDGET = float(os.environ.get(
    "QIDIAN_EXEC_BUDGET", str(config.TASK_DEADLINE_S - config.TASK_WRAPUP_MARGIN_S)))
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
# ── 「一次多动作」实验开关（2026-09-12）─────────────────────────
#
# 背景：执行器**本来就**把一轮响应里的所有 function_call 全部执行
# （`:469 for tc in tool_calls`）。所以"一次多动作"卡的不是代码，是**模型不这么吐** ——
# 上面那份 SYSTEM_PROMPT 描述的是"读→写→测→修"的**串行**走法，等于在暗示一轮一个动作。
#
# ⚠️ 这**不是**稳赚的改动。SPACE 那篇（arXiv 2609.02042）的消融显示：
# 光允许一次多动作会掉分（多动作 GRPO 轮数砍到 5.5，成功率从 83.6 掉到 65.6），
# 得学会"在哪切"才好。我们这边的收益/代价**一点数据都没有**。
# 所以做成**环境变量开关、默认关**，A/B 就是翻这个变量跑两次，不用改代码。
#
# 打开：QIDIAN_BATCH_TOOLS=1
_BATCH_TOOLS_HINT = """

批量调用（重要）:
- 一轮回复里**可以同时发起多个工具调用**，它们会一起执行完再回到你这。
- 互不依赖的动作请**一次发完**：要写 3 个文件就一轮发 3 个 write_file；要查几处代码就一轮发多个 search_code。
- 有依赖的（写完要跑测试才知道对不对）**不要**硬凑一轮。
- 判断标准只有一条：**后一个调用用不上前一个的结果** → 就该同一轮发。"""


def batch_tools_enabled() -> bool:
    """环境变量开关。默认关 = 行为跟改动前逐字一致。"""
    return os.environ.get("QIDIAN_BATCH_TOOLS", "") == "1"


SYSTEM_PROMPT = """你是Singularity Dispatch的 AI Agent。你的唯一任务是产出可运行的代码。不要输出方案、计划或分析，直接写代码。

你有工具可以用：读文件、写代码、跑命令、搜代码。

工作方式:
- 如果是纯分析/调研任务（不需要改代码），直接输出分析结果，不要调用工具
- 如果需要改代码：先读相关文件→立即用write_file写代码→跑测试→修复→输出最终代码
- 如果工具返回"文件不存在"或空结果超过2次，基于已有知识直接写代码，不要反复尝试

铁则:
- 禁止输出"方案1/方案2"、"建议采取以下步骤"等分析内容，直接实施
- 改代码前必须 read_file 看原文件
- **别替别的任务干活**：任务描述点名了产出文件时，就只写它（及它必需的配套文件）。
  ⚠️ **特别是别顺手创建测试文件** —— 测试是**另一个任务**的产出，你替它写了，
  它那一轮就会"零文件改动"、被质量门禁判失败（2026-09-13 实测：实现任务顺手写了
  `test_*.py`，写测试的那个任务空手，整个项目因此卡住）。要验证就**跑一次性命令**
  （`python3 -c …` / 临时脚本），或直接说你没法验证 —— **不要为此新建文件**。
- 不要在注释里留 TODO，要么实现要么删掉
- 参数 path 是相对于项目根目录的路径，不要用绝对路径
- 写文件时给完整可运行的代码，不只给 diff
- 对照需求完整实现：功能要全覆盖，需求里的软性要求（风格/响应式/空态/异常态/移动端适配）也要做到，别只写最小可运行版
- 跑命令一律用 `python3` 开头（本机没有 `python` 命令，敲 `python` 会 command not found）；跑测试固定 `python3 -m pytest -q`（**只在测试文件已经存在时才跑** —— 没有就参照上面那条，别新建）
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
    has_tool_surface = True      # 每次工具调用都经 `_execute_tool` ⇒ 权限闸门在这儿

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
        # ⚠️ `agent_level` 必须传给父类：权限闸门收进 `BaseExecutor._check_permission`
        # 之后，它读的是 `self.agent_level`（原来这份自己存 `self._agent_level`，
        # 父类那份是空的 ⇒ 不传的话 profile 查表会按 "" 去查、永远查成 full-access）。
        super().__init__(cfg, task, task_id, baseline_ref=baseline_ref, cwd=cwd,
                         agent_level=agent_level or cfg.get("_level", ""))
        self._api_key = os.environ.get(cfg.get("api_key_env", ""), "")
        # `_agent_env` 改由 BaseExecutor 存（原来只有这份存，anthropic 那份因此看不到它）。
        # ponytail: 仍然只存实例属性, 不写全局 os.environ (防并发 Agent 竞态)
        self._url = cfg.get("entry", "")
        self._is_responses_api = "/v1/responses" in self._url or "/responses" in self._url
        self._model = cfg.get("request_template", {}).get("model", cfg.get("model", ""))
        self._max_turns = cfg.get("max_turns", 15)  # ponytail: coding任务需要足够轮次(读→写→测→修)
        self._cwd = (Path(cwd) if cwd else config.PROJECT_ROOT).resolve()  # resolve 掉 /tmp→/private/tmp 等符号链接, 否则 write_file 的 relative_to 会炸
        self._changed_files: list[str] = []
        self._tool_events: list[dict] = []
        # body 每轮重建，被 API 拒过的思考参数要记住，否则下一轮又加回来、又撞一次 400
        self._rejected_think_keys: set[str] = set()
        # 同一件事的**另一个面**：thinking 模式不接受 `tool_choice="required"`
        # （DeepSeek 原文 `400 Thinking mode does not support this tool_choice`）。
        # 下面的降级分支原来**只改当次的 body**，而 body 每轮重建 ⇒ **每个带工具的
        # 轮次都重撞一次 400**。2026-09-19 夜真机复现的量：3 个工具轮 = 6 次 HTTP，
        # 其中 3 次是白撞的（探针 `/tmp/probe_exec_400.py`）。记在实例上，一次就够。
        self._no_required_tool_choice = False
        # `_agent_level` 删了（2026-09-14）：闸门收进基类后它一个读者都没有，
        # 而同一件事存两份正是本仓反复吃亏的形状（改一处漏一处）。现在只有
        # `self.agent_level` 一份，由上面的 super() 赋值。

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
            # 实验开关：默认关（行为与改动前逐字一致）。见 _BATCH_TOOLS_HINT 的说明。
            if batch_tools_enabled():
                system_prompt += _BATCH_TOOLS_HINT
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
        # 自查总预算（轮间看表 + 单次调用封顶都用它，见 _EXEC_BUDGET）。
        # **从调用方给的"还剩多久"倒推**，不是从自己这个 `start` 算 ——
        # 执行器是 `_run_executor` 每次 dispatch 新建的，用 `start` 等于每 dispatch
        # 把预算清零 ⇒ 跑过 ≥2 次 dispatch 就永远不收尾（2026-09-13 查明的真根因，
        # 见 `docs/防御模式.md` §67）。
        # `budget_s is None` = 调用方不管（阶段级那条路）⇒ 退回老行为；
        # 取 `min` 是为了**保住 `QIDIAN_EXEC_BUDGET` 这个真机验证开关**（小值优先）。
        # ≤0 时 `_deadline_at` 落在过去 ⇒ 第 1 轮就看表、立刻收尾，正是想要的。
        _budget = _EXEC_BUDGET if self.budget_s is None else min(_EXEC_BUDGET, self.budget_s)
        self._deadline_at = start + _budget
        _wrapped = False

        for turn in range(1, self._max_turns + 1):
            # 轮间看表：到点不再开新轮，直接收尾（否则下一轮一跑就是几分钟，
            # 冲过 orchestrator 的 900s 一样被无声砍 —— 那就白改了）。
            if time.time() >= self._deadline_at:
                _wrapped = True
                break
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
                    # `_no_required_tool_choice` = 这个模型已经拒过一次，别再每轮重撞（见 __init__）
                    "tool_choice": ("required"
                                    if tools and not self._no_required_tool_choice
                                    else "auto"),
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
                    # ⚠️ **这个降级原来只活在这一次调用里** —— 记住它，否则下一轮的 body
                    # 又把 required 拼回来、又撞一次（2026-09-19 夜复现：每个工具轮都白打一发）。
                    # 只在**第一次**学到时出声，不然告警会被自己的重试刷屏。
                    if not self._no_required_tool_choice:
                        self._no_required_tool_choice = True
                        witness.warn("oa_exec",
                                     f"tool_choice_required_rejected:{self._model}:{str(e)[:60]}"[:150])
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
                        return self._fail_result(str(e2), start, exc=e2)
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
                        return self._fail_result(str(e2), start, exc=e2)
                else:
                    # 认不出来的 400 —— **先把现场留下再失败**，否则又是无头案（见该方法 docstring）
                    self._dump_unknown_400(body, str(e))
                    return self._fail_result(str(e), start, exc=e)
            except _NetworkError as e:
                # 预算已到 → 这多半是上面封顶超时导致的断流，**不是**该换模型重来的
                # 瞬时网络故障。报成网络错会被上层 failover 掉，白烧剩下的时间。
                if time.time() >= self._deadline_at:
                    _wrapped = True
                    break
                return self._fail_result(str(e), start, exc=e)

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

            # ── 没有 tool_calls 时，看看是不是吐了 XML 形式（见 _parse_xml_tool_calls）──
            # **必须在 messages.append 之前**：否则回给模型的历史里没有这次调用，
            # 下一轮 tool 结果就对不上 id 了。接住了要告警 —— 这是"模型没按协议来"，
            # 不是正常路径，得能查得到。
            if not msg.get("tool_calls"):
                _xml_calls = _parse_xml_tool_calls(msg.get("content", ""))
                _scope = "recovered" if _xml_calls else "unparsed"
                if _xml_calls:
                    msg["tool_calls"] = _xml_calls
                if _xml_calls is not None:
                    # ⚠️ **这里原来有一句函数内的 `from singularity.scheduler import witness`，
                    # 2026-09-19 删掉了 —— 那不是省略，是个真 bug。**
                    # Python 把函数内任何赋值/import 都当作**整个函数的局部绑定**，
                    # 于是本函数里**所有** `witness.*` 都变成引用局部变量 `witness`，
                    # 而它只在走到这一行时才被绑上 ⇒ 它**上面**那些 `witness.warn`
                    # 跑到就抛 `UnboundLocalError`。
                    # 挨的那一处正是「模型不吃思考参数 → 摘掉重试」的告警
                    # （本文件 `:519`），也就是**最该留痕的那条路径**反而炸掉。
                    # 本模块 `:21` 早就有模块级 `from singularity.scheduler import config, witness`，
                    # 这句局部 import 一直是多余且有害的。
                    # 逮住它的是 `ruff --select=E,F` 的 **F823** —— 它在这仓的 757 条里
                    # 躺了很久没被看见，因为 CI 的门从来没绿过（见 `docs/CI与发布审计-20260919.md`）。
                    try:
                        witness.warn("oa_exec", (f"xml_tool_calls_{_scope}:"
                                                 f"{len(_xml_calls)}:"
                                                 f"{(_xml_calls[0]['function']['name'] if _xml_calls else '-')}"
                                                 )[:120])
                    except Exception:
                        pass

            # ⚠️ `reasoning_content` **不能无条件剥掉**（2026-09-15 真机 + DeepSeek 官方文档）。
            #
            # 原来这行是一刀切，注释写着"推理模型(如Kimi/GLM)返回reasoning_content,
            # API输入不接受此字段" —— **那是旧经验**。现在 DeepSeek 的规则
            # （官方 thinking-mode 文档原文）是：
            #   · **带 `tools` 参数**：`reasoning_content` **必须在后续所有请求里原样回传**
            #     （含没有工具调用的轮次）——「must be fully passed back to the API in all
            #     subsequent requests」；**不回传就是 400**；
            #   · **不带 `tools`**：不必回传，传了也会被忽略。
            # 真机症状：`any 层所有 agent 均失败: deepseek-flash: 空输出
            #   [HTTP 400: The reasoning_content in the thinking mode must be passed back]`
            # —— **换 agent 也没用**（同一个剥法）⇒ 整条任务挂掉。
            # 注意上面**流式那条特意把 reasoning_content 拼了回来**
            # （`if reasoning: msg["reasoning_content"] = …`）就是给这里用的 ——
            # 原来下一行就扔了，**两条路自相矛盾**。
            #
            # 按官方规则**只在该回传时回传**：带 tools 才留。
            # 不带 tools 的那条路（架构/规划的 `no_tools` 委员会）**照旧剥掉** ——
            # 对 Kimi/GLM 维持原行为，**零回归**。
            messages.append(_assistant_msg_for_history(msg, tools))

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
                        # ── 「一次多动作」实验的埋点（2026-09-12）──
                        # 光有"调了哪些工具"算不出"省不省轮" —— 得知道**第几轮、那轮几个**。
                        # 修 trace 之前这些数据拿不到（trace 只存最终产物），
                        # 所以量不了就加埋点，别靠印象。
                        "turn": turn,
                        "batch": len(tool_calls),
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
                        "turn": turn,
                        "batch": len(tool_calls),
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
                    #
                    # ⚠️ **必须同时说清"交付物得落盘"**（2026-09-15 真机坐实）：
                    # 原来只有「停止使用工具，直接输出最终答案」这一句 —— 而它是全仓
                    # **唯一一条"只劝、不硬撤工具"**的收尾指令（上一条 661 与下面 675
                    # 都是 `tools = []`）。真机上模型听了劝、把 311 行测试文件**贴在回答
                    # 正文里**收尾，还写着"以下代码块即为权威交付物" —— 而平台只收文件，
                    # 正文里的代码**永远不会被交付**，任务照样判 `done`。
                    # 同文件里还有一条方向相反的（`tool_choice` 兜底那条：
                    # 「必须调用工具产出文件，禁止只输出文字描述」）—— 两条对着干，
                    # **谁后出现谁赢**，而这条更靠后。补上这句，让它不再互相抵消。
                    messages.append({
                        "role": "system",
                        "content": "[系统] 已收集足够信息。停止使用工具，直接输出最终答案。"
                                   "但**交付物必须以文件形式存在**：还没写进文件的代码/文档，"
                                   "先用 write_file 落盘再收尾 —— "
                                   "**只写在回答正文里的内容不会被交付**。"
                    })
                last_tool_calls = call_fingerprint
                continue  # 继续下一轮，让模型看工具结果

            # 无 tool_calls → 任务完成
            tool_turns = 0  # 重置工具计数
            content = msg.get("content", "")
            if not content and resp_data.get("_cut"):
                # ── 被我们掐断、正文一个字没拿到 ⇒ **这次调用没有可用产出** ──
                # 两条理由都必须在这里收尾，而不是让循环再转一圈：
                #   ① 剩下的"产出"只有**半截思考** —— 拿它当正文就是 2026-09-18 那条链的起点
                #      （真机 15 个失败任务**没一个败在"做错"，全败在"没产出"**，
                #       而 QA 是扫着模型**自己的思考**判它「偷懒」的）；
                #   ② 原样重试 = 同一个模型 + 同一个上限 ⇒ **再烧一个 240 秒**，
                #      正是 `TestStreamOverBudgetNoOutput` 那条 F1 死法（三轮烧穿 810s）。
                # `error_kind="deadline"`（我方上限）是**刻意的**：`_dispatch_exec.py:236`
                # 已经认这一档是"我方造成、别赖模型" —— 落进 `exec` 会被记成
                # "这个模型空输出"并 `record_failure`，三次就把好模型熔断 300 秒。
                self._track()
                return ExecutorResult(
                    success=False,
                    error=(f"单次调用撞上限被掐断，正文零产出"
                           f"（工具轮 {len(self._tool_events)} 次，"
                           f"改动 {len(self._changed_files)} 个文件）"),
                    error_kind="deadline",
                    changed_files=list(self._changed_files),
                    elapsed=time.time() - start, token_count=total_tokens,
                    tool_events=list(self._tool_events))
            # 推理模型(如Kimi/GLM)可能 content="" 但 reasoning_content 有内容 ——
            # 那种"答完了、只是答案写在思考里"要兜底。
            # ⚠️ **被掐断时不许兜底**：那时 reasoning 是半截思考，不是产出的替身。
            if not content:
                content = msg.get("reasoning_content", "")
            if content.strip():
                elapsed = time.time() - start
                # 用 git diff 追踪改动的文件
                self._track()
                return ExecutorResult(
                    success=True, raw_output=content,
                    changed_files=list(self._changed_files),
                    elapsed=elapsed, token_count=total_tokens,
                    tool_events=list(self._tool_events),
                )

        if _wrapped:
            # 撞总预算: 主动收尾，把**已知事实**交回去（烧掉的 token、改过的文件、跑过的轮次）。
            # success=False 是实话（活确实没干完）—— 别为了好走流程谎报成功。
            # error_kind="deadline" 是给 _exec 的信号：**别换模型重来**。换一个只会把
            # 剩下的时间再烧一遍，换完照样被 900s 无声收割，而这份账同样保不住。
            self._track()
            return ExecutorResult(
                success=False,
                error=(f"到达执行预算 {_EXEC_BUDGET:.0f}s，主动收尾"
                       f"（工具轮 {len(self._tool_events)} 次，改动 {len(self._changed_files)} 个文件）"),
                error_kind="deadline",
                changed_files=list(self._changed_files),
                elapsed=time.time() - start, token_count=total_tokens,
                tool_events=list(self._tool_events))

        # 达到最大轮次: 模型可能已写文件但没输出终答 → 追踪 changed_files, 有文件就算产出
        self._track()
        if self._changed_files:
            return ExecutorResult(
                success=True, raw_output="(达到最大工具轮次, 已产出文件)",
                truncated_by="max_turns",   # ← 「成功但其实被截断」的结构化出口
                changed_files=list(self._changed_files),
                elapsed=time.time() - start, token_count=total_tokens,
                tool_events=list(self._tool_events))
        return ExecutorResult(success=False,
                              error=f"达到最大轮次 {self._max_turns}，任务未完成",
                              error_kind="exec", elapsed=time.time() - start,
                              tool_events=list(self._tool_events))

    # ── 工具执行 ──

    # `_check_permission` 已收进 `BaseExecutor`（2026-09-14）—— 原来只有这一份，
    # 另外三个执行器一个 permission 引用都没有，等于"换个 type 就绕过闸门"。
    # 语义（注入则调、异常则拒、未注入则放行但出声）都在基类那份的 docstring 里。

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
        """**派给模块级 `_run_command`**（2026-09-14）—— 这个工具的语义只留一份。

        原来这里和模块级那份（anthropic 在用）**是两套实现**：`shell=True` vs
        `shell=False`、带不带 agent env 全都不同 ⇒ 同一个工具名换个执行器两种行为。
        现在两边走同一个函数；这里只负责把 `self._agent_env` 递过去。
        """
        return _run_command({"command": command}, self._cwd, getattr(self, "_agent_env", None))

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

    def _track(self):
        """`no_tools` 时**不追改动** —— 这不是省事，是**别报假警**。

        禁工具的那条路（评审 / QA / 架构 / 调研）**没有写文件的工具**，所以它
        **不可能**改过文件；而 `_track_changed_files` 会给出一条
        `collect_changes:no_baseline_ref` 告警 —— 那条告警在真机上**一周 338 次**，
        常年霸占告警页第一名（同族的 `claude_cli` 那条一起算，占全部告警的 47%），
        把真事故盖住。**假告警比没有告警更坏**：它训练人忽略这个页面。

        ⚠️ 能这么写的前提是 `honors_no_tools` 那条已经落实（`_dispatch_exec._honors_no_tools`）：
        **拦不住禁工具的执行的器根本不会跑**，所以"跑到这里 = 确实没碰过磁盘"成立。
        """
        if bool(self.cfg.get("no_tools")):
            return
        self._track_changed_files()

    def _track_changed_files(self):
        """追踪改动的文件。

        ⚠️ **原来这里只跑裸的 `git status --porcelain`（跟 HEAD 比），会被"改动已提交"
        骗到**：agent 自己 `git commit` 之后工作区是干净的 ⇒ changed_files 空 ⇒
        `_exec` 那句 `if changed:` 为假 ⇒ **审查 / QA / 安全审计整条被跳过**，
        而 `git commit` 并不在 `base.py` 的 `_BLOCKED_COMMANDS` 里（拦不住）。

        同一个形状在 `validator._diff_base` 和 `_salvage_timed_out` 已各踩过一次
        （防御模式 §55）—— 那两处都改成了"跟执行前的快照 ref 比"，这里漏了。
        改法直接复用 `claude_cli._git_changed_files`（同一招，别各写一份）：
        `git diff <baseline_ref>` 看得见**已提交 + 未提交**，再并上 untracked。
        """
        try:
            # 降级路径要**出声**：拿不到基线时 `_git_changed_files` 会退回裸 diff，
            # 那就又会漏掉已提交的部分 —— 静默降级正是这个 bug 的一半。
            if not self.baseline_ref:
                try:
                    witness.warn('oa_exec', 'collect_changes:no_baseline_ref（判据不完整）'[:120])
                except Exception:
                    pass
            from singularity.scheduler.executors.claude_cli import _git_changed_files
            for f in _git_changed_files(self.baseline_ref, str(self._cwd)):
                # 过滤构建产物 (__pycache__/.pyc), 不算交付文件
                if not f or f in self._changed_files:
                    continue
                if "__pycache__" in f or f.endswith((".pyc", ".pyo")):
                    continue
                self._changed_files.append(f)
        except Exception as e:
            try:
                witness.warn('oa_exec', f'collect_changes:{e}'[:80])
            except Exception:
                pass

    def _fail_result(self, error: str, started: float,
                     exc: BaseException | None = None) -> ExecutorResult:
        """失败返回 —— **已经改过的文件必须跟着交回去**。

        ⚠️ 2026-09-18：这几条 `return` 原来**都不带 `changed_files=`**（只有成功路径和
        撞预算收尾调 `self._track()`）⇒ 一个任务**前几轮写好了 5 个文件、第 4 轮网络
        抖一下就失败**，交回去的却是"改动 0 个" ⇒ QA 拿这个 0 判它
        「无文件改动 + 偷懒」。**干过活的和没干活的，在账上长得一模一样** ——
        而 2026-09-18 那 15 个失败任务，正是靠这个 0 一条条判死的。

        `_track()` 在这里是**幂等追加**（`_track_changed_files` 只 `append`，不覆盖），
        所以先前轮次记下的文件不会丢。

        ⚠️ `error_kind` 仍然是 `"exec"`（调用真的失败了）—— **不改成 `deadline`**：
        那档是给"我方上限主动收尾"的，改了会让 `_dispatch_exec` 不再换模型重试，
        而这里的失败（网络/格式）本来就该换。
        """
        self._track()
        return ExecutorResult(
            success=False, error=error,
            # `exc` 只用来**给种类起个名**，不改任何判定：`stalled` 和 `exec` 在
            # 下游走**完全同一条路**（换模型重试 + 记 breaker，见 `_dispatch_exec`）——
            # 全仓**没有任何分支认 `exec`**（2026-09-19 核过）。差别只在日志/终态/
            # `unverified` 里写的是 `stalled 未产出` 还是 `exec 未产出`。
            error_kind=("stalled" if isinstance(exc, _StalledError) else "exec"),
            changed_files=list(self._changed_files),
            elapsed=time.time() - started, tool_events=list(self._tool_events))

    def _dump_unknown_400(self, body: dict, err: str) -> None:
        """**认不出来的** 400：把当次请求体原样落一份，供事后复现。

        来历（2026-09-19 夜，一整轮真机死在这上面）：任务报
        `400 The reasoning_content in the thinking mode must be passed back`，
        等回头看时**现场什么都没有** ——
          · trace 只留了"超时那次"（`_save_trace` 幂等，先写者胜）；
          · `alerts.jsonl` 只有告警键，没有请求体。
        只能事后拿探针去重撞，四个形状**全是 200**，触发条件至今没找到。
        ⇒ 认不出来的 400 一次落一条，别让下一次再变成无头案。

        **只落认不出来的**：`tool_choice` / 思考参数那两类代码自己会处理，
        每轮都记只会把真信号淹掉（同 `_SLOW_CALL_LOG_S` 只记 ≥20s 的理由）。

        ⚠️ **不写摘要、原样落 `messages`** —— 触发条件很可能就藏在"某条消息有没有
        某个字段"上，摘要过一道正好把要找的东西摘掉（本仓栽过：拿摘要当证据）。

        ⚠️ 落盘失败**不许静默**：这条通道是"现场"的唯一来源，它哑了和
        "没发生过 400"长得一模一样（同 `witness` 那第二条通道的理由）。
        """
        try:
            rec = {
                "ts": time.time(),
                "model": self._model,
                "url": self._url,
                "error": str(err)[:500],
                "task_id": self.task_id,
                "tool_choice": body.get("tool_choice"),
                "tool_names": [(t.get("function") or {}).get("name", "")
                               for t in (body.get("tools") or [])],
                # messages / tools 之外的那些参数（temperature、max_tokens、思考参数……）
                "params": {k: v for k, v in body.items()
                           if k not in ("messages", "tools")},
                "messages": body.get("messages"),
            }
            with (config.QIDIAN_DIR / "llm_400_unknown.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:                    # noqa: BLE001
            witness.warn("oa_exec",
                         f"unknown_400_dump_failed:{type(e).__name__}:{e}"[:150],
                         key="unknown_400_dump_failed")

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
        bad_frames = 0
        # 单次调用也要封在**剩余预算**内：只在轮间看表是不够的 —— 一轮本身可能跑
        # 几分钟，看完表再开一轮照样冲过 900s，又变成被无声收割。
        _left = getattr(self, "_deadline_at", 0.0)
        _cap = min(240.0, max(1.0, _left - time.time())) if _left else 240.0
        # ⚠️ **总时长**另算一把尺（2026-09-15 真机坐实）：
        # `httpx.Timeout(read=)` 封的是**两次读之间**的时间，**不是总时长** ——
        # 服务端只要持续吐 token，一次调用就能跑任意久。
        # 实测：一次 dispatch `elapsed=1615.7` 秒（27 分钟）、**`tokens=0`**
        # —— usage 只在流结束时才到，说明流压根没结束。
        # 而轮间那句 `if time.time() >= self._deadline_at` **只在两轮之间**，
        # 单次调用里根本轮不到 ⇒ **执行器全程"没看见表"**，最后被外层 900s 的刀
        # 无声砍掉（`task_killed_no_wrapup`）—— 这正是"900s 自收尾没生效"的新根因，
        # 与 09-13 那条「每 dispatch 归零」是**两个病**，修了一条不等于修了另一条。
        _call_started = time.time()
        _call_deadline = _call_started + _cap
        _over_budget = False
        # 慢调用分诊账要的三样（见文件末尾那段注释）：发出去的 payload 多大、
        # **第一个字节什么时候到**、一共转了几圈。
        _prompt_chars = len(json.dumps(payload, ensure_ascii=False))
        _first_byte = None
        try:
            with client.stream("POST", self._url, json=payload, headers=headers,
                               timeout=httpx.Timeout(_cap, connect=15.0,
                                                     read=min(_STALL_TIMEOUT, _cap))) as resp:
                if resp.status_code >= 400:
                    resp.read()                      # 先取回 body 才能读 .text
                    self._raise_for_status(resp)
                emitted, last_emit = 0, time.time()
                # ⚠️ **"停滞"要量的是「多久没有**可用进展**」，不是「多久没有字节」**
                # （2026-09-16 真机坐实 —— 而且 `_STALL_TIMEOUT` 那句注释本来就写着
                #   「多久**没新 token**」：是**实现**没跟上它自己的语义）。
                #
                # 病：`read=` 那把尺量的是 **socket 读** —— 服务端只要一直在发字节
                # （保活 / 凑不满一行的半截分片）它就**永不触发**，而这一轮可能
                # **一个字都没拿到**。实测同一天三次：`cap=240s, chars=0`、
                # `696s, chars=0`、`900s, chars=0` —— 每次调用烧满上限、零产出，
                # 几轮就把任务的 **810s 预算**烧光 → `deadline_wrapup` → 判死，
                # 而**前几轮已经干出来的活全丢了**（真机：185 行测试留在悬空提交里）。
                # ⇒ 90 秒拿不到 content/reasoning/tool_calls 就当**停滞**，抛错换 agent，
                #   别把预算烧在同一个卡住的模型上。
                # ⚠️ **三样都算进展**：思考模型的 `reasoning_content` 也是进展，
                # 只认 content 的话会把"正在想"误判成"卡住"。
                # ⚠️ **阈值就是 `_STALL_TIMEOUT`（默认 90s，`QIDIAN_STALL_TIMEOUT` 可调）**，
                # 而它有个**我没实测过**的边界：会不会有厂商"静默 90 秒、然后一次性吐"？
                # 真撞上就是**把一次本来能成的调用换给下一个 agent** —— 代价可控
                # （failover 拿到了活），但要知道这是拿 90 秒换的。
                # 反过来（保持原样）的代价已经量过了：810s 预算烧光、任务判死、产物丢。
                _last_progress = time.time()
                # 循环体转了几圈 —— 2026-09-17 加，专门为了分清那个**解释不通的 900 秒**：
                # 告警打出 `stream_over_budget:913s:cap=125s`，而判据就挂在循环体里、
                # 125 秒时**必然**该触发 ⇒ 那段时间循环体多半**根本没进过**。
                # `loops` 小 + `idle≈elapsed` = 循环**饿着**（流那头不出货）；
                # `loops` 大 + `idle` 小 = 循环在转、是**判据没掐住**。两种修法完全不同。
                _loops = 0

                def _lines():
                    """按行吐，但**每收到一批字节**就先看一眼表。

                    ⚠️ 2026-09-16 真机**两轮复现**：原来直接 `for line in
                    resp.iter_lines()`。而 `iter_lines()` 是**按行**吐的 ——
                    服务端只要一直在发字节、却凑不满一行（httpx 的 `LineDecoder`
                    会把不满一行的片段**一直缓冲**），这个循环**一行都收不到**，
                    于是循环体里那句总时长判据**永远轮不到**。
                    两轮真机各卡死 30+ 分钟，`stream_over_budget` 全库 **0 次**；
                    线程栈停在 `_ssl__SSLSocket_read → PySSL_select → poll`。

                    ⇒ 改成 `iter_text()`：它**每收到一批字节就 yield 一次**
                    （行切分挪到下面自己做）**只要网络还在动，判据就有机会执行**。

                    三种"服务端不吐东西"的长相，实测（read=3s 的独立探针）：
                      · **纯静默**（一个字节不回）→ `read=` 超时 3.0s 就抛，✅ 兜得住；
                      · **保活整行**（每秒 `: ping\\n`）→ read 超时被重置，但循环体在跑，
                        ⇒ 总时长判据兜得住；
                      · **吐字节但凑不满一行** → **两条都兜不住** ← 真机死的就是这种。
                    """
                    nonlocal _over_budget, _loops, _first_byte
                    buf = ""
                    for text in resp.iter_text():
                        _loops += 1
                        if _first_byte is None:
                            _first_byte = time.time() - _call_started
                        if time.time() >= _call_deadline:
                            _over_budget = True
                            return
                        _idle = time.time() - _last_progress
                        if _idle > _STALL_TIMEOUT:
                            # **抛**，不是"出声后返回空" —— 返回空会被上层当成
                            # "这次调用成功了、只是模型没说话"，于是同一个卡住的模型
                            # 继续被派下一轮；抛错才走 failover，换一个 agent。
                            raise _StalledError(
                                f"流停滞 {_idle:.0f}s 无新 token"
                                f"（一直在收数据，但 content/reasoning/tool_calls 一样都没有）")
                        buf += text
                        while "\n" in buf:
                            ln, buf = buf.split("\n", 1)
                            yield ln.rstrip("\r")
                    # 收尾：最后一行不带换行符时 `iter_lines()` 会 flush 出来，
                    # 这里保持同样行为 —— 否则最后一条 `data:` 会被无声吞掉。
                    if buf:
                        yield buf.rstrip("\r")

                for line in _lines():
                    # **总时长**封顶：见 `_call_deadline` 那段注释。
                    # 断流要**出声**（下面），否则"输出莫名其妙变短"永远找不到原因 ——
                    # 同 `bad_frames` 那条的规矩：认不出/提前断都要明报，不能静默。
                    # ⚠️ 生成器里已经判过一次（那才是关键那道）；这里再判一次是兜
                    # "一批字节里一次切出几十行"——那种情况下上头只看了一次表。
                    if time.time() >= _call_deadline:
                        _over_budget = True
                        break
                    # 这一行的**处理前后**比一比长度，才判得出它有没有带来可用进展
                    _n_before = (len(content), len(reasoning), len(tool_calls))
                    if not line.startswith("data:"):
                        continue
                    chunk_str = line[5:].strip()     # 容忍 "data:{...}" 无空格
                    if chunk_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(chunk_str)
                    except json.JSONDecodeError:
                        # 单个 SSE 分片坏掉（截断 / 厂商噪声）**不能杀掉整轮**。
                        # 原来这里是裸解析：一个坏帧抛穿 `_stream_call`，上层当成
                        # "agent 失败" → 整条 fallback 链全灭 → 任务 0 产物。
                        # 实测 2026-09-12：glm-5.3-flash 报
                        # `JSONDecodeError: Unterminated string ... (char 187)`，
                        # `any` 层两个 agent 一起废掉。
                        # 按 §58 的规矩：**认不出要明报**，不能静默吞（下面计数 + 告警）。
                        bad_frames += 1
                        continue
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
                    # 这一行带来了可用进展吗（content / reasoning / tool_calls 有任何一个变长）
                    # —— 变了就把"停滞计时"归零。**只有变长才算**：光"又收到一包字节"
                    # 不算进展，那正是今晚烧光预算的那种。
                    if (len(content), len(reasoning), len(tool_calls)) != _n_before:
                        _last_progress = time.time()
        except httpx.TimeoutException:
            # read timeout = 流停滞（不是整体超时）—— 报清楚，方便区分
            raise _StalledError(f"流停滞 {_STALL_TIMEOUT:.0f}s 无新 token")
        except httpx.HTTPError as e:
            raise _NetworkError(f"网络错误: {e}")

        if bad_frames:
            # 坏帧被跳过了 ⇒ 这轮内容可能**少了一截**，必须可查 ——
            # 否则"模型输出莫名其妙变短"永远找不到原因。
            try:
                witness.warn('oa_exec', f'sse_chunk_unparsed:{bad_frames}'[:80])
            except Exception:
                pass

        if _over_budget:
            # 同上，而且更该说：这是**我们主动断的**，不是模型答完了。
            # 不说的话，下游只看到"这轮输出特别短"，会去怀疑模型而不是看这里。
            # ⚠️ **不套 `try/except: pass`**（棘轮抓过）：那形状等于"出声失败就静默"，
            # 而出声本身就不该失败 —— `witness.warn` 就是本仓的出声通道。
            # ⚠️ `loops` / `idle` 是 2026-09-17 加的**分诊用**两个数（见上面 `_loops` 那段）：
            #    没有它俩，"循环饿着"和"判据没掐住"在盘上长得一模一样，
            #    只能靠读代码猜 —— 而今晚我已经猜错一次了（拿 `raw_truncated` 当"被截断"）。
            witness.warn('oa_exec',
                         f'stream_over_budget:{int(time.time() - _call_started)}s'
                         f':cap={int(_cap)}s:chars={sum(len(c) for c in content)}'
                         f':loops={_loops}:idle={int(time.time() - _last_progress)}s'[:200],
                         key='stream_over_budget')

            # ── F1（保险）：**撞了上限、又一个字没拿到** ⇒ 按调用失败抛出去 ──
            # 不抛的话这里返回的是一个**看起来正常的空回答**，而：
            #   · 执行器的 turn 循环**不看 raw_output**（它只看 `msg`），直接进下一轮；
            #   · `_dispatch_exec` 那条 failover 判据（`result.raw_output`）要等这个
            #     执行器**先返回**才轮得到。
            # ⇒ 同一个卡住的模型继续烧，一轮 240s，三轮就把 810s 预算耗尽 ——
            # 2026-09-16 真机正是这么死的（`stream_over_budget:329s/696s/900s, chars=0`
            # → 执行器撞自己的 810s → `deadline_wrapup` → 任务判死、产物不合并）。
            # `_STALL_TIMEOUT`（F2，90s 无新 token）已覆盖大部分；这条只管**兜底那一格**：
            # 只在"撞了上限"**且"零产出"**时抛 —— 内容还在长（长回答被截断）不在此列。
            if not content and not reasoning and not tool_calls:
                raise _NetworkError(
                    f"流式撞上限 {int(_cap)}s 且零产出 —— 按调用失败处理"
                    f"（不是「模型答完了、只是没说话」）")

        # ── 慢调用的分诊账（2026-09-17 加）────────────────────────────
        # 背景：真机上模型调用动辄 90~240 秒，而**直连同一个接口**（同 key、同模型、
        # 8 并发）实测首字节 0.1~0.2s、整批 8 秒 ⇒ **外部原因全排除了**
        # （代理没开 · 服务端不慢 · prompt 4.8 万字符也一样快 · 并发不是瓶颈）。
        # 那慢就一定在**奇点自己这一跳**，但要先分出是哪一种：
        #   ① 首字节就慢          ⇒ 发出去的 payload / 连接建立有问题
        #   ② 首字节快、总久、产出大 ⇒ **模型真在写大东西**（不是 bug，是"看起来像卡住"）
        #   ③ 首字节快、总久、产出小 ⇒ 才是真的空转
        # 三种修法完全不同 ⇒ 只记**慢的那些**（≥20s），不然这台账自己就成噪声。
        _elapsed = time.time() - _call_started
        if _elapsed >= _SLOW_CALL_LOG_S:
            try:
                with open(_slow_calls_path(), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "ts": time.time(),
                        # ⚠️ **model 必须有**：架构/执行都是**多家**跑同一个 prompt，
                        # 不记是哪一家的话，"这家特别慢"和"这家卡住了"分不出来
                        # —— 而当晚真机上就是同一 prompt 两家差了 4.7 倍（738 vs 156 字/秒）。
                        "model": str(payload.get("model") or self._model or "?"),
                        "elapsed": round(_elapsed, 1),
                        "first_byte": round(_first_byte, 1) if _first_byte is not None else None,
                        "prompt_chars": _prompt_chars,
                        # ⚠️ **正文和思考必须分开记**（2026-09-18）：合在一起时
                        # "产出 67385 字符"根本看不出那是思考还是正文 —— 而
                        # **"240 秒是不够、还是它在原地打转"**这个判断题，答案就在这两者之比上。
                        # 分开之前，只能靠"另跑一次拿真 prompt 直打"去猜（见 OPEN.md 那条未答）。
                        "content_chars": sum(len(c) for c in content),
                        "reasoning_chars": sum(len(r) for r in reasoning),
                        "tool_calls": len(tool_calls),
                        "loops": _loops,
                        "cut": bool(_over_budget),
                    }, ensure_ascii=False) + "\n")
            except Exception as _e:      # noqa: BLE001 —— 记账不许连累调用本身
                # ⚠️ **不吞**（本仓的静默异常棘轮抓过两次这个形状）：写不进去也要吭一声,
                # 否则"这台账没数据"和"没有慢调用"长得一模一样 —— 正是它要防的。
                witness.warn("oa_exec", f"slow_call_log:{type(_e).__name__}:{_e}"[:120],
                             key="slow_call_log_failed")

        msg = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if tool_calls:
            msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
        # ⚠️ `_cut` 是**我们自己的**字段（不是 provider 的 schema），下划线标出来。
        # 它回答的是"这次是模型答完了，还是**被我们掐断的**" —— 这两件事
        # 以前在返回值里**长得一模一样**，只能靠猜。2026-09-18：往下游要这个答案。
        return {"choices": [{"message": msg, "finish_reason": finish}],
                "usage": usage, "_cut": bool(_over_budget)}


# ── 全局 httpx 客户端 (连接池复用) ──

_HTTPX_CLIENT: httpx.Client | None = None


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
    # ⚠️ **空内容不许覆盖一个非空文件**（2026-09-15 真机坐实）：
    # 原来这里无条件 `p.write_text(content)`，而 `content` 缺省是 `""` ——
    # 一次**没带上内容**的调用（参数没拼出来 / 被 max_tokens 截掉）就能把交付物
    # 清成 0 字节，**返回的还是一条"已写入 X (0 字符)"，长得像成功**。
    # 真机现场：一个任务的 311 行测试文件被自己清空，而 `changed_files` 因此非空
    # ⇒ 「零改动 = 没产出」那条判据被绕过去了 ⇒ 一路判 `通过` 到 `done`。
    # 新建空文件（`__init__.py` 之类）仍然放行 —— 拦的只是"把已有内容抹掉"这一种。
    if content == "" and p.is_file() and p.stat().st_size > 0:
        return (f"写入被拒绝：content 为空，而 {path} 已有 {p.stat().st_size} 字节内容，"
                "这会把它清空。请把**完整内容**放进 content 再调一次；"
                "确实要清空的话，用 run_command 显式做。")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path} ({len(content)} 字符)"

def _run_command(args: dict, cwd, extra_env: dict | None = None) -> str:
    """命令执行的**唯一实现** —— 类方法 `_tool_run` 现在也派到这儿。

    ⚠️ 2026-09-14 收敛（外派 ⑩ 抓到、我核过）：同一个工具名 `run_command`
    在两个执行器上**行为不一样**，而模型看不见这个区别 ——
      · `openai_agent._tool_run`：`shell=True`（支持 `&&` / `|` / `source`）
        + 合并 agent 自己的 env（PATH/代理/endpoint）；
      · 这个模块级函数（anthropic 在用）：`shell=False` + `shlex.split`
        （`&&` 会被当成普通参数、`source` 直接不存在）+ **完全不带 agent env**。
    ⇒ 同一份任务提示词，换个 `type` 就是两种行为；模型写 `a && b` 在一边能跑、
    在另一边静默变成一条把 `&&` 当文件名的命令。

    现在统一成 `shell=True`（类方法那条注释解释过为什么必须支持 shell 语法：
    `shell=False` 会把 `&&` / `source` 当参数、生成垃圾目录），
    安全性仍靠 `_is_dangerous_command` 黑名单**前置**拦截。
    `extra_env` = agent 配置里的 `env`（`BaseExecutor._agent_env`），
    合并进子进程环境后**再统一脱敏**（`_is_sensitive_env`）。
    """
    cmd = args.get("command", "")
    if not cmd:
        return "请指定 command"
    dangerous, reason = _is_dangerous_command_at(cmd)
    if dangerous:
        return f"命令被拦截: {reason}"
    if not str(cmd).strip():
        return "空命令"
    # ponytail: 合并 agent env 到局部环境, 不污染 os.environ（并发 Agent 会打架）
    merged = {**os.environ, **(extra_env or {})}
    safe_env = {k: v for k, v in merged.items() if not _is_sensitive_env(k)}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30,
                           cwd=str(cwd), env=safe_env)
        out = r.stdout[-4000:] if r.stdout else ""
        err = r.stderr[-2000:] if r.stderr else ""
        return f"exit={r.returncode}\nstdout:\n{out}\nstderr:\n{err}"
    except subprocess.TimeoutExpired:
        return "命令超时 (30s)"
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

class _RateLimitError(Exception):
    pass
class _FormatError(Exception):
    pass
class _NetworkError(Exception):
    pass


class _StalledError(_NetworkError):
    """流停滞（`_STALL_TIMEOUT` 秒没有任何新 token）。

    ⚠️ **它是 `_NetworkError` 的子类，不是新的一类失败** —— 走 failover 的判定
    一个字没变（`_fail_result` 只多写一个种类名 `stalled`，而**没有任何下游分支认它**）。
    存在的理由（2026-09-19 复核审计 A6）：它和"模型吐了个空"原来在 `error_kind` 上
    **长得一样**（都是 `exec`），只能靠 error 文本区分 —— 又是"只活在文本里"。
    ⚠️ **别把它算进"我方掐断"**（`supervisor.our_side_stop_of` 只认 `deadline`）：
    停滞是服务端/网络的事，不是我们的刀。
    """
    pass
class _TransientError(Exception):
    pass     # 5xx —— 可重试（429 由 _RateLimitError 单独走）
