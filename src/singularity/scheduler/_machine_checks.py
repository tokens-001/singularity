"""机械检查：把架构里的 `constraints[].check` 变成**真能跑**的东西。

背景（`docs/信任上限-可测化-20260912.md`）：架构 prompt 一直写着"每条约束必须可
机器检查"，但全仓**没有任何执行器** —— 那个字段从出生起只被拼进 prompt 喂给 LLM。
于是"信任上限 = 机械证据覆盖的验证面比例"这句话，分子永远是 0。

## 安全前提（这是本文件存在的主要理由）

跑这条链路等于 **让模型生成的字符串变成平台要执行的命令**，而架构 JSON 来自委员会、
受任务描述影响 —— 是 prompt injection 面。所以：

1. **argv 数组，不过 shell。** 平台直接 exec，不解析字符串 → 引号 / 分号 / 管道
   全都只是普通字符，注入面归零。
2. **argv[0] 白名单**，且**解释器只允许紧跟 `-m pytest`**。
   `python3 -c "..."` 等于任意代码执行，必须堵死。堵法是**在参数层判**，不是只判程序名。
3. **cwd 锁死在项目仓库内**（传进来的 root 之下），不许跑出去。
4. **超时**必给。
5. **环境变量洗过** —— 只留 PATH/HOME/LANG：不带 API key，也不带代理（断掉"顺着代理出网"）。
6. **只跑人批准过的。** 本模块提供能力，**是否执行由 GATE2 的人工决定**（见调用方）。

## 仍然残留的风险（说清楚，别当已解决）

- 参数里可以放**绝对路径**（`cat /etc/passwd`）。白名单挡不住，cwd 也锁不住。
- `npm run *` 跑的是项目自己 `package.json` 里的脚本 —— 那脚本是模型写的。
- **直连出网挡不住**（洗环境只断了走代理那条路）。真要封得靠 OS 级沙箱。
  所以这里的定位是"**人在环里的机械化**"，不是"无人值守的安全沙箱"。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# argv[0] 白名单（按 basename 比）
ALLOWED_ARGV0 = {
    "python3", "python", "pytest", "npm", "node", "git",
    "ls", "cat", "wc", "test", "grep", "head", "tail",
}

# 解释器后面**只允许**跟这几个 token —— 见模块头第 2 条
_INTERPRETERS = {"python3", "python"}
_INTERPRETER_OK_PREFIX = ["-m", "pytest"]

DEFAULT_TIMEOUT = 60.0


def parse_check(check) -> dict | None:
    """`check` 字段 → `{"argv": [...], "expect_exit": int}`；散文 / 非法 → None。

    两种写法是**故意的**：能机器跑的给 argv，验不了的如实写散文。
    只有前者算"兑现"，后者算"如实承认验不了"——两者的比例就是那个数。
    """
    if not isinstance(check, dict):
        return None
    argv = check.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    try:
        expect = int(check.get("expect_exit", 0))
    except (TypeError, ValueError):
        return None
    return {"argv": list(argv), "expect_exit": expect}


def validate_check(check) -> tuple[bool, str]:
    """(能不能跑, 不能跑的原因)。散文返回 (False, "不是 argv 结构")。"""
    parsed = parse_check(check)
    if parsed is None:
        return False, "不是 {argv, expect_exit} 结构（散文 or 格式错）"
    argv = parsed["argv"]
    prog = os.path.basename(argv[0])
    if prog not in ALLOWED_ARGV0:
        return False, f"argv[0] 不在白名单: {prog}"
    if prog in _INTERPRETERS and argv[1:3] != _INTERPRETER_OK_PREFIX:
        return False, f"解释器只允许 `-m pytest`，实际: {' '.join(argv[1:3]) or '(无参数)'}"
    return True, ""


def coverage(constraints) -> tuple[int, int]:
    """(可机器跑条数, 总条数)。**这就是那个数**（分母 = 全部约束，分子 = 能跑的）。

    ⚠️ 分母取自约束清单本身，而约束是架构师列的 —— 所以它是"自洽率"，
    不是"对需求的覆盖率"。后者要拿需求侧（`scope_clarification.core`）当分母，
    见 `docs/信任上限-可测化-20260912.md` §二。这一步先把**能测的那半**落下来。
    """
    items = list(constraints or [])
    ok = sum(1 for c in items
             if isinstance(c, dict) and validate_check(c.get("check"))[0])
    return ok, len(items)


def describe(check) -> str:
    """给人看的一行。前端 / prompt 都用它，保证两边口径一致。"""
    parsed = parse_check(check)
    if parsed is None:
        return str(check or "")
    return f"{' '.join(parsed['argv'])}（期望退出码 {parsed['expect_exit']}）"


def _clean_env(home: str = "") -> dict:
    """跑检查用的环境：只留 PATH / HOME / LANG。

    抽成独立函数是为了**能单独测** —— 否则"没带 API key"这件事只能靠跑一条命令
    去验，而"跑模型给的命令"正是本模块要防的东西。
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home or os.environ.get("HOME", ""),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }


def run_check(check, root: str | os.PathLike, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """跑一条检查。**只该在人批准之后调**（见模块头第 6 条）。

    返回 {"ran", "passed", "exit", "stdout", "stderr", "reason"}。
    任何一步不满足安全前提 → ran=False 并说明原因，**绝不"降级也跑一下"**。
    """
    ok, why = validate_check(check)
    if not ok:
        return {"ran": False, "passed": False, "exit": None,
                "stdout": "", "stderr": "", "reason": why}
    parsed = parse_check(check)

    root_p = Path(root).resolve()
    if not root_p.is_dir():
        return {"ran": False, "passed": False, "exit": None,
                "stdout": "", "stderr": "", "reason": f"根目录不存在: {root_p}"}

    # 环境洗干净：不带 API key（模型生成的命令不该能读到凭证），
    # 也不带 http(s)_proxy（断掉"顺着代理出网"那条路）。
    env = _clean_env(str(root_p))
    try:
        proc = subprocess.run(
            parsed["argv"], cwd=str(root_p), env=env, timeout=timeout,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": False, "exit": None, "stdout": "", "stderr": "",
                "reason": f"超时 {timeout}s"}
    except (OSError, ValueError) as e:
        return {"ran": False, "passed": False, "exit": None, "stdout": "", "stderr": "",
                "reason": f"无法执行: {type(e).__name__}: {e}"}

    return {
        "ran": True,
        "passed": proc.returncode == parsed["expect_exit"],
        "exit": proc.returncode,
        "stdout": (proc.stdout or "")[:2000],
        "stderr": (proc.stderr or "")[:2000],
        "reason": "",
    }
