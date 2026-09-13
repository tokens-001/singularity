"""统一 I/O 原语 — TOML 加载 + JSON 提取。

规则: 全仓 TOML/配置文件加载和 agent 输出 JSON 提取统一走此模块。
新增 TOML 读取或 LLM 输出解析不得裸调 tomllib/json.loads。
"""

from __future__ import annotations

import json
import os
import re as _re
import threading
import tomllib
from pathlib import Path


# 🔴 **tmp 名要带 pid、写的时候要拿锁 —— 两样缺一不可**（2026-09-14，外派 J 审
# `防御模式.md` §46 时抓到：那条的修法**只落在 `project.py:479`**，这个共用入口没跟着改）。
#   · **不带 pid**：两个独立进程写同一个文件时共用同一个确定性 `<name>.tmp` ——
#     A replace 成功后 tmp 就没了，B 的 replace 撞 ENOENT；或者两边写入交错，
#     正式文件里多出半个 `}` → 解析失败 → 读侧静默跳过（`project.py:474-478`
#     把那整条链写得最全，那里当时修了）。
#   · **不带锁**：同进程多线程（调度循环 / merge 执行器 / Flask 请求线程）拿到的
#     是**同一个 pid**，pid 后缀挡不住它们 ⇒ 上面那条竞态在进程内原样成立。
# `project.py:479` 两块都做了；这里是把它挪到共用的那一层来 —— 这正是那份文档
# 自己的规矩（§65："按形状全仓扫"）。
# ⚠️ `route_learner.py:156` 的注释写着"atomic_write_json 解决②、这把锁解决①" ——
#   它把**跨进程那半托付给了本函数**；在本函数没 pid 的那段时间里，那个承诺是空的。
_WRITE_LOCK = threading.Lock()


def atomic_write_json(path: Path, data) -> None:
    """原子写 JSON: 先写同目录 .tmp 再 os.replace, crash 不损坏正式文件。

    统一入口: api_store/tracker/_memory_core/_token_budget 共用 (原各自复制一份)。
    **跨进程 + 同进程线程都安全** —— 见上面 `_WRITE_LOCK` 那段说明。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with _WRITE_LOCK:
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




# ═══════════════════════════════════════════════════════════════
# S1：损坏的 JSON/TOML 不许被当成"空"（2026-09-14，C 的草案 + 我核过）
# ═══════════════════════════════════════════════════════════════
# **形状**：`json.loads 失败 → 返回 {}` 这种写法，会把"文件坏了"和"文件是空的"
# 变成同一条路。下游拿到 `{}` 照常跑、照常 **写回整份** —— 于是一次撕裂写
# 在下一次读取时把真数据全盖掉（`api_store` 那条自毁链就是这个形状里最狠的一例）。
#
# **契约（三态，先说死）**：
#   文件不存在                          → `{}` / `[]`（`expect` 决定）—— 这才是"真的空"
#   存在、可解析、顶层类型对            → 解析结果
#   存在但读不了 / 解析不了 / 类型不对  → **None**（返回前已完成三件事，见下）
#
# **返回 None 之前本函数已经做完**：
#   ① 原样字节备份 `<名>.corrupt`（已存在则时间戳轮转，**不毁旧证据**）
#   ② 双通道出声（`log` + `witness`）
#   ③ 原文件一字不动
#
# ⚠️ **调用方纪律**（写在这，但靠每个调用点自觉）：
#   只读路径   → 可以降级：`load_json_or_quarantine(p) or {}`
#   读改写路径 → **必须停**：拿到 None 就 raise / 拒写，宁可这次操作失败，
#                不可拿默认值去重建整份文件（`or {}` 只许出现在没有任何写入的路径上）

def _quarantine_corrupt(path: Path, reason: str) -> None:
    """读坏文件的统一处置：原样备份 + 双通道出声。

    备份失败**不抛** —— 但也不能不吭声：消息里如实写"没备上"，
    而且备份失败本身也单独出一条，免得"没备上"被后来的告警淹没。
    """
    import shutil
    import time as _time
    from .log import warn as _log_warn   # 函数体内 import：witness→tracker→_io 是现成的环

    try:
        bak = path.with_suffix(path.suffix + ".corrupt")
        if bak.exists():
            bak = path.with_suffix(path.suffix + f".corrupt.{int(_time.time())}")
        shutil.copy2(path, bak)          # 原始字节 + mtime，不经过任何解析
        note = f"已备份 {bak.name}"
    except OSError as e:
        _log_warn("io", f"{path.name} 的损坏备份没做成: {type(e).__name__}: {e}"[:200])
        note = "备份失败(原文件未动)"

    _QUARANTINED.add(str(path))
    msg = f"{path.name} 损坏({reason}): {note}, 拒绝当空"[:200]
    _log_warn("io", msg)
    try:
        from . import witness
        witness.warn("io", msg, key=f"json_corrupt:{path.name}")
    except Exception as e:               # noqa: BLE001
        # log 通道已经写过一次了，这里别二次抛；但要说清"只有一条通道出去了"
        _log_warn("io", f"损坏告警的第二通道(witness)没发出去: {type(e).__name__}"[:200])


# 「本进程内判过损坏的路径」。**同一个文件可能有多个写者**（`agents_custom.json`
# 就有 `_dispatch_crud` 和 `skill_loader` 两个），让每个模块各记一个标记迟早漏一个
# ⇒ 把这件事收在**路径**上，写者统一问 `is_quarantined(path)`。
_QUARANTINED: set = set()


def is_quarantined(path: Path) -> bool:
    """这个路径在**本进程内**被判过损坏吗？（判过的意思是：已隔离 + 已出声）

    ⚠️ 写者必须问这一句：读侧降级成空之后，**拿手里那份整份写回去 = 用空表重建** ——
    历史配置没了，而文件名一模一样（2026-09-14，S1 那族形状）。
    ⚠️ 进程内粘住：想恢复要人工处理 `.corrupt` 备份后重启，这正是"坏了就别继续当好的用"。
    """
    return str(path) in _QUARANTINED


def load_json_or_quarantine(path: Path, *, expect: type = dict):
    """读 JSON 状态/配置文件。**三态，不吞** —— 契约见上面那段。"""
    if not path.exists():
        return expect()
    try:
        data = json.loads(path.read_bytes())
    except OSError as e:
        _quarantine_corrupt(path, f"读失败 {type(e).__name__}")   # 读不了多半也备不了，函数会如实说
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _quarantine_corrupt(path, f"{type(e).__name__}: {e}"[:80])
        return None
    if not isinstance(data, expect):
        _quarantine_corrupt(path,
                            f"类型不对: 期望 {expect.__name__}, 实得 {type(data).__name__}")
        return None
    return data


def load_toml_or_quarantine(path: Path) -> "dict | None":
    """`load_json_or_quarantine` 的 TOML 版。

    ⚠️ 既有的 `load_toml`（损坏 = 原样抛）**保留不动** —— `web/app.py` 的 fusion 读写靠它自己兜。
    """
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        _quarantine_corrupt(path, f"读失败 {type(e).__name__}")
        return None
    except tomllib.TOMLDecodeError as e:
        _quarantine_corrupt(path, f"TOMLDecodeError: {e}"[:80])
        return None
    if not isinstance(data, dict):
        _quarantine_corrupt(path, f"类型不对: 期望 dict, 实得 {type(data).__name__}")
        return None
    return data
