#!/usr/bin/env python3
"""接线核对：一次改动里**新出现的「值/键/信号」**，谁写、谁读、读点在哪。

来历（2026-09-30，一天两次、同一形状）：

  · `9fd32832` —— 把"任务没交产出文件"这个信号塞进 `validation.unverified`，
    指望 `_decide_cascade` 拼反馈时带走它。**那条接线不存在**
    ⇒ 模型收到的反馈等于什么都没说 ⇒ 盲试 3 次、109,118 token、零产出。
  · `af3dfec5` —— 读 `tr.get("output")`，而 `run_check` 写的是 `"stdout"`
    ⇒ fail-closed 把它咽成"照旧判红"，**盘上一点痕迹都没有**（哑弹）。

共同形状：**有生产者 ≠ 送到了消费者**。

用法：

    .venv/bin/python scripts/wiring_check.py              # 工作区 vs HEAD
    .venv/bin/python scripts/wiring_check.py <rev>        # 那个提交 vs 它的父

⚠️ **它只查机械那一半**（谁写、谁读）。"读点是不是**干活的那一端**"要人判 ——
   脚本把它摆出来，不替你回答；判不了的它写"判不了"，不猜。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# `WIRING_SRC` 只给**回测**用：拿老提交的源码树跑一遍，看这把尺当时会不会响。
# 正常使用不设它。
import os

SRC = Path(os.environ["WIRING_SRC"]) if os.environ.get("WIRING_SRC") else ROOT / "src"

# **只抽"用在键位上"的字面量** —— 09-30 回测：宽松版（任何字符串字面量）在
# `9fd32832` 上报了 11 个，其中 10 个是**字符串值**（`"git"` / `"add"` / `"tasks"`…），
# 真信号 `missing_own_deliverable` 埋在里头 ⇒ **信噪比差到会被直接无视**，等于没造。
# 三种键位：`"k":` · `.get("k")` · `["k"]`。
_KEY = re.compile(r"""(?:["']([a-z][a-z0-9_]{2,})["']\s*:"""
                  r"""|\.get\(\s*["']([a-z][a-z0-9_]{2,})["']"""
                  r"""|\[["']([a-z][a-z0-9_]{2,})["']\])""")

# 写点：`"k":`  /  `["k"] =`  /  `setdefault("k"`  /  `update({"k"`
# ⚠️ 最后那条 kwarg 写法（`unverified=[...]`）是 09-30 回测逼出来的：
#    只认 `"k":` 的话两边都数成 0 ⇒ **被当成文案静默跳过** —— 正是要抓的那种洞。
_WRITE = re.compile(r'"{k}"\s*:|\["{k}"\]\s*[+\-*/]?=|\.setdefault\(\s*"{k}"|\b{k}\s*=\s*[\[\{\'"]')
# 读点：`["k"]`（不接 =） / `.get("k")`  / `.pop("k"`
_READ = re.compile(r'\.get\(\s*"{k}"|\.pop\(\s*"{k}"|\["{k}"\]|\.{k}\b')


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=True).stdout


def new_keys(rev: str | None) -> list[str]:
    """从那一次改动的**新增行**里抽字面量键。"""
    args = ["show", "--format=", "-U0", rev] if rev else ["diff", "-U0", "HEAD"]
    added = [ln[1:] for ln in _git(*args).splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]
    keys: set[str] = set()
    for ln in added:
        for groups in _KEY.findall(ln):
            keys.update(g for g in groups if g)
    return sorted(keys)


def scan(key: str) -> tuple[list[str], list[str]]:
    """在整个 `src/` 里找谁写真名、谁读它。"""
    writes, reads = [], []
    for f in sorted(SRC.rglob("*.py")):
        for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{f.relative_to(SRC.parent)}:{i}"
            # 直接替换 `{k}`，不用 `str.format` —— 正则里本来就有 `{`，
            # 走 format 会被当成占位符开头（踩过：`expected '}' before end of string`）。
            if re.search(_WRITE.pattern.replace("{k}", re.escape(key)), ln):
                writes.append(f"{where}  {ln.strip()[:96]}")
            elif re.search(_READ.pattern.replace("{k}", re.escape(key)), ln):
                reads.append(f"{where}  {ln.strip()[:96]}")
    return writes, reads


# 读点长什么样才像"送到了干活的那一端"——只是个**提示**，不是判据（判不了就写判不了）。
# ⚠️ `append(` **不在**这里：回测发现 `report.unverified.append(...)` 是**写**，
#    算进来会把"自己往自己身上塞"误判成"送到干活的人了"。
_ACTION = re.compile(r"feedback|prompt|context_snippet|dispatch|cascade|payload|"
                     r"message|describe|render|模板|反馈|描述")


def main() -> int:
    rev = sys.argv[1] if len(sys.argv) > 1 else None
    keys = new_keys(rev)
    if not keys:
        print("这次改动里没抽出新的字面量键 —— 没什么可核的。")
        return 0

    where = rev or "工作区 vs HEAD"
    print(f"接线核对（{where}）：新增 {len(keys)} 个字面量键\n")

    hard, soft = [], []
    for k in keys:
        w, r = scan(k)
        if not w and not r:
            continue                      # 两边都没有 ⇒ 是文案，不是键
        mark = ""
        if w and not r:
            mark = "  🔴 写了没人读"
            hard.append(k)
        elif r and not w:
            mark = "  🔴 读了没人写（键名可能对不上）"
            hard.append(k)
        elif not any(_ACTION.search(x) for x in r):
            # ⚠️ **这一档噪音大**（09-30 回测：`af3dfec5` 一次报 18 条、17 条假）
            # ⇒ 只当**弱提示**，不决定退出码 —— 否则整条命令会被无视。
            mark = "  🟡 有读点，但没一个像'干活的那一端'"
            soft.append(k)
        print(f"── {k}{mark}")
        for x in w:
            print(f"   写  {x}")
        for x in r:
            print(f"   读  {x}")
        print()

    if soft:
        print("🟡 弱提示（噪音大，看一眼即可，可能是正常的）：" + " · ".join(soft))
    if hard:
        print("\n🔴 硬的（写读缺一边 —— 这类**没有例外**，缺了就是没接上）：" + " · ".join(hard))
        return 1
    print("\n✅ 没有「写读缺一边」的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
