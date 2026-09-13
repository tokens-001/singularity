"""敏感路径 / 危险命令的**唯一一份**表（2026-09-14 拆出来的叶子模块）。

**为什么要有这个文件**：同一件事原来写了两遍，而且已经不一致了 ——

  · `executors/base.py` 的 `_BLOCKED_PATTERNS` / `_BLOCKED_COMMANDS`
    （**硬地板**：所有执行器、所有 agent 读写文件和跑命令时都过它）
  · `permission.py` 的 `SANDBOXED.blocked_paths` / `blocked_commands`
    （**profile 那一层**：绑了 sandboxed 的 agent 额外受限；界面照着它显示"拦截承诺"）

实测不一致是**双向**的：`base` 有 `id_rsa`/`*.pem`/`.netrc` 而 sandboxed 没有
（这部分**恰好被地板兜住**，所以看不出来）；sandboxed 有 `rm -rf`（无空格版）
而 `base` 只拦 `rm -rf /`、`~`、`.`（`rm -rf build/` 只有绑了 sandboxed 才拦）。

⇒ 按本仓自己的规矩"**抄一份必漂**"，这里收成一份：两边都 import 它，
profile 那边取"地板 ∪ profile 额外"。**改表只改这一处。**

⚠️ 把它放成**叶子模块**（不 import 本包任何东西）不是洁癖：`executors/base.py`
是各执行器的公共基类，`permission.py` 又是 `_dispatch_skills` 要导的 —— 让它们
互相 import 会把导入图接出一条环（本仓在 `dispatcher` 那个"一个毂 + 三根辐条"
上已经吃过一次：**单独导入任意一个兄弟都炸**）。叶子模块没有这个问题。
"""

from __future__ import annotations

import fnmatch

# ── 敏感路径（硬地板） ──────────────────────────────────────────
BLOCKED_PATH_PATTERNS = [
    ".env", ".env.*", "*.token", "*.key", "*.pem", "*.p12", "*.pfx",
    "*.secret", "*.password", "*.credential",
    # ⚠️ **无后缀的私钥**（2026-09-14，外派 K 条6 / H 反7 / E'⑤ 三方独立撞上）：
    # `*.key` / `*.pem` 罩不住 `id_rsa` 这一族 —— 模型一条 read_file 就能把私钥
    # 整读进上下文随输出带走。`web/app.py:1358` 的 `_SENSITIVE_FILES` 早就补了
    # 这几个、注释还点名了这个洞，**这张表当时没跟着改** —— 同一件事只修了一半。
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".flaskenv",
    # ⚠️ **`.crt` 不拦**（2026-09-14，逆向审抓到、我认）：证书是**公开材料**，
    # 拦它没有防泄露的价值，却会把"加 HTTPS / 配 mTLS"这类任务的显式读写**弄瘸**
    # （任务读不到自己的证书就没法干活）。带私钥的那几种后缀
    # （`*.key` / `*.pem` / `*.p12` / `*.pfx`）仍然拦着，覆盖了常规命名。
    ".ssh/", ".ssh/*",
    ".qidian/", ".qidian/*", ".git/", ".git/*", ".claude/",
    "venv/", ".venv/", "__pycache__/", "*.pyc",
    "users.json", "config.toml", "agents.toml",
]

# ── 危险命令（硬地板） ──────────────────────────────────────────
BLOCKED_COMMANDS = [
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "curl", "wget",
    "chmod 777", "chmod -R",
    "sudo ", "su ",
    "mkfs.", "dd if=",
    ":(){ :|:& };:",
    "> /dev/sda",
    "shutdown", "reboot", "halt", "poweroff",
    "iptables", "nc -l", "nc -e",
    "python -c", "perl -e", "ruby -e", "bash -c",
    "eval ", "exec ",
]


def is_blocked_path(path: str, patterns: list[str] | None = None) -> tuple[bool, str]:
    """敏感路径检查。返回 `(blocked, reason)`。

    `patterns` 不给就用地板那张表。profile 层传自己那张（= 地板 ∪ 额外）进来，
    这样**两处匹配语义也一致** —— 原来 `permission.check_path` 只做前两条 fnmatch、
    没有"逐段比"，同一个路径在两处的判定可以不同。
    """
    normalized = path.replace("\\", "/")   # 原来是 base 那边做的，别在搬家时丢了
    for pattern in (BLOCKED_PATH_PATTERNS if patterns is None else patterns):
        if fnmatch.fnmatch(normalized, pattern):
            return True, f"敏感文件/目录: {pattern}"
        if fnmatch.fnmatch(normalized, f"*/{pattern}"):
            return True, f"敏感文件/目录: {pattern}"
        parts = normalized.split("/")
        for part in parts:
            if fnmatch.fnmatch(part, pattern.rstrip("/*")):
                return True, f"敏感文件/目录: {pattern}"
    return False, ""


def is_dangerous_command(command: str, commands: list[str] | None = None) -> tuple[bool, str]:
    """危险命令检查（子串匹配，命中任意一条即拦）。返回 `(dangerous, reason)`。"""
    cmd_lower = command.strip().lower()
    for blocked in (BLOCKED_COMMANDS if commands is None else commands):
        bl = blocked.lower()
        if cmd_lower.startswith(bl) or bl in cmd_lower:
            return True, f"危险命令被拦截: {blocked}"
    return False, ""
