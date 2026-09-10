"""统一 I/O 原语 — TOML 加载 + JSON 提取。

规则: 全仓 TOML/配置文件加载和 agent 输出 JSON 提取统一走此模块。
新增 TOML 读取或 LLM 输出解析不得裸调 tomllib/json.loads。
"""

from __future__ import annotations

import json
import os
import re as _re
import tomllib
from pathlib import Path


def atomic_write_json(path: Path, data) -> None:
    """原子写 JSON: 先写同目录 .tmp 再 os.replace, crash 不损坏正式文件。

    统一入口: api_store/tracker/_memory_core 共用 (原各自复制一份)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════
# TOML
# ═══════════════════════════════════════════════════════════════

def load_toml(path: Path) -> dict:
    """从 .toml 文件加载配置。文件不存在或解析失败返回空 dict。"""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def save_toml(path: Path, data: dict) -> None:
    """保存 dict 到 .toml 文件。支持嵌套表、数组、字符串、数字、布尔。

    ponytail: 手动序列化，不引入 toml 依赖。
    """
    import json as _json
    lines = []
    def _write_section(d: dict, prefix: str):
        for k, v in d.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and not any(isinstance(vv, (list, dict)) for vv in v.values()):
                # 简单字典 → [section]
                lines.append(f"\n[{full_key}]")
                for sk, sv in v.items():
                    lines.append(_format_kv(sk, sv))
            elif isinstance(v, dict):
                lines.append(f"\n[{full_key}]")
                for sk, sv in v.items():
                    if isinstance(sv, dict):
                        lines.append(f"\n[{full_key}.{sk}]")
                        for ssk, ssv in sv.items():
                            lines.append(_format_kv(ssk, ssv))
                    else:
                        lines.append(_format_kv(sk, sv))
            else:
                lines.append(_format_kv(k, v))
    # 顶级键直接处理
    sections = {}
    for k, v in data.items():
        if isinstance(v, dict):
            sections[k] = v
        else:
            lines.append(_format_kv(k, v))
    for k, v in sections.items():
        _write_section({k: v}, "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).lstrip() + "\n", encoding="utf-8")

def _format_kv(k: str, v) -> str:
    import json as _json
    if isinstance(v, list):
        return f"{k} = {_json.dumps(v, ensure_ascii=False)}"
    elif isinstance(v, bool):
        return f"{k} = {str(v).lower()}"
    elif isinstance(v, (int, float)):
        return f"{k} = {v}"
    elif isinstance(v, str):
        # 必须转义: 值里带 " 或换行会把 TOML 写坏，而读侧 `except: return {}`
        # 会把解析失败吞成空配置 —— 用户的设置静默消失且查不出原因。
        esc = (v.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        return f'{k} = "{esc}"'
    else:
        esc = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'{k} = "{esc}"'


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
        # 修复模型 JSON 瑕疵: 用 `?` 标注可选字段但位置错 (}? 和 ]? 非法)
        c = _re.sub(r'\}\?', '}', c)
        c = _re.sub(r'\]\?', ']', c)
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


# ═══════════════════════════════════════════════════════════════
# JSON Patch (RFC 6902) — 辩论轮增量修订
# ═══════════════════════════════════════════════════════════════


def apply_json_patch(original: str, patch_raw: str) -> str:
    """把模型产出的 RFC 6902 补丁应用到原方案 JSON，返回修订后 JSON 字符串。

    增量修订: 让模型只输出改动(约2k)而非全量重写(约20k)。
    任一步失败(原方案非 JSON / 补丁解析失败 / 路径无效)都返回原方案，
    宁可保留初稿也不引入坏数据。
    """
    doc = try_parse_json(original)
    if doc.get("parse_error"):
        return original
    ops = _parse_patch_ops(patch_raw)
    if ops is None:
        return original
    try:
        for op in ops:
            _apply_op(doc, op)
    except Exception:
        return original
    return json.dumps(doc, ensure_ascii=False)


def _parse_patch_ops(raw: str):
    """从模型输出提取补丁数组（容忍 ```json 块 / 前后杂文）。失败返回 None。"""
    if not raw:
        return None
    for m in _re.finditer(r"\[[\s\S]*\]", raw):
        try:
            ops = json.loads(m.group())
        except Exception:
            continue
        if isinstance(ops, list):
            return ops
    return None


def _apply_op(doc: dict, op: dict) -> None:
    kind = op.get("op", "replace")
    path = op.get("path", "")
    keys = [
        int(p) if p.isdigit() else p.replace("~1", "/").replace("~0", "~")
        for p in path.strip("/").split("/") if p
    ]
    if not keys:
        return
    if kind == "remove":
        _del_path(doc, keys)
    else:  # replace / add
        _set_path(doc, keys, op.get("value"), insert=(kind == "add"))


def _set_path(doc, keys, value, insert=False) -> None:
    cur = doc
    for k in keys[:-1]:
        cur = cur[k]
    last = keys[-1]
    if isinstance(cur, list):
        if not isinstance(last, int):
            raise ValueError(f"数组路径需数字索引: {last}")
        if insert:
            cur.insert(min(last, len(cur)), value)
        elif last < len(cur):
            cur[last] = value
        else:
            cur.append(value)  # 越界 replace 容忍为 append
    else:
        cur[last] = value


def _del_path(doc, keys) -> None:
    cur = doc
    for k in keys[:-1]:
        cur = cur[k]
    last = keys[-1]
    if isinstance(cur, list):
        cur.pop(last)
    else:
        cur.pop(last)


