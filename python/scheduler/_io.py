"""统一 I/O 原语 — TOML 加载 + JSON 提取。

规则: 全仓 TOML/配置文件加载和 agent 输出 JSON 提取统一走此模块。
新增 TOML 读取或 LLM 输出解析不得裸调 tomllib/json.loads。
"""

from __future__ import annotations

import json
import re as _re
import tomllib
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# TOML
# ═══════════════════════════════════════════════════════════════

def load_toml(path: Path) -> dict:
    """从 .toml 文件加载配置。文件不存在或解析失败返回空 dict。"""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


# ═══════════════════════════════════════════════════════════════
# JSON 提取 (从 agent 原始输出)
# ═══════════════════════════════════════════════════════════════

def try_parse_json(raw: str, try_repair: bool = False) -> dict:
    """从 agent 原始输出中提取 JSON。统一处理 ```json 块/裸{}/截断修复。

    返回 dict，解析失败时含 parse_error 标记。
    原位于 workflow._try_parse_json，下沉到 _io 消除 workflow 被各处反向依赖。
    """
    if not raw:
        return {"raw_output": "", "parse_error": True}
    candidates = []
    # 方式1: ```json ... ``` 代码块
    for m in _re.finditer(r"```(?:json)?\s*\n(.*?)```", raw, _re.DOTALL):
        candidates.append(m.group(1).strip())
    # 方式2: 裸 {...} 块
    if not candidates:
        m = _re.search(r"\{[\s\S]*\}", raw)
        if m:
            candidates.append(m.group().strip())
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            # 修复常见 JSON 错误 (尾逗号)
            try:
                fixed = _re.sub(r',\s*}', '}', c)
                fixed = _re.sub(r',\s*]', ']', fixed)
                return json.loads(fixed)
            except Exception:
                continue
    # 尝试截断修复
    if try_repair:
        repaired = _repair_truncated_json(raw)
        if repaired is not None:
            return repaired
    return {"raw_output": raw[:5000], "parse_error": True}


def _repair_truncated_json(raw: str) -> dict | None:
    """修复被截断的 JSON — 补全未闭合的括号和引号。"""
    if not raw:
        return None
    # 提取 JSON 块
    body = raw
    m = _re.search(r"```(?:json)?\s*\n(.*)", raw, _re.DOTALL)
    if m:
        body = m.group(1).strip()
    # 找到第一个 {
    start = body.find("{")
    if start == -1:
        return None
    body = body[start:]
    # 数括号，补充未闭合的
    depth = 0
    in_string = False
    escaped = False
    for ch in body:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    # 补全
    if in_string:
        body += '"'
    while depth > 0:
        stripped = body.rstrip()
        if stripped.endswith("]"):
            body += "}"
            depth -= 1
        elif stripped.endswith("}") or stripped.endswith('"'):
            body += "}"
            depth -= 1
        else:
            body += "]}"
            depth -= 2
    try:
        return json.loads(body)
    except (json.JSONDecodeError, Exception):
        return None


# try_parse_json_list 已移除 — 无调用方, 保留空壳。
