"""第四批修复（2026-09-14 深夜）—— 五处，全是"同一件事只修了一半"那个形状。

出处：外派 `K` 的条3/条4/条5/条6（`复核核实-01`，我核过 6 条全真）、
`E'` 的 `反审-02`、`H` 的 `判清单-01` 反7 —— 三份独立来路指向同几处。

  ① `base._BLOCKED_PATTERNS` 漏 `id_rsa` 一族 / `.netrc` / `.flaskenv` / `.crt` / `.ssh/`
     —— 而 `web/app.py:1358` 那张表**早就补了**、注释还点名了这个洞
  ② 模块级 `_run_command` 的 `subprocess.run` 不传 `env=` ⇒ 子进程继承全量 `os.environ`
     —— anthropic 执行器直接把 `run_command` 派到它，全仓只此一条路不脱敏
  ③ `mcp_server_add` 只写配置、不喂注册表（delete 09-14 修了，add 漏了）
  ④ `mcp_server_reconnect` 喂的是**单个** config，而 `load_configs` 是整体替换
     ⇒ 重连一个把别的全抹了
  ⑤ anthropic `_tmo` 循环外算一次、多轮共用 ⇒ 最坏 max_turns 倍预算
  ⑥ `_io.atomic_write_json` 的 tmp 不带 pid、写入不拿锁（外派 J 审 `防御模式.md` §46 抓到：
     那条的修法只落在 `project.py:479`，这个共用入口没跟着改）

测试都**钉接线**：删掉对应那一行判据（或把参数改回去），测试必须红。
"""
import pytest


# ═══════════════════════════════════════════════════════════════
# ① agent 侧读文件黑名单
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", [
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "/home/u/.ssh/id_rsa", "keys/id_rsa",
    ".netrc", "~/.netrc", ".flaskenv",
    "server.crt", "ca.crt",
    ".ssh/known_hosts",
])
def test_无后缀私钥那族要被拦(path):
    """`*.key`/`*.pem` 罩不住 `id_rsa` —— 模型一条 read_file 就能把私钥带走。

    这是"同一件事两半"：`web/app.py:1358` 的 `_SENSITIVE_FILES` 早就有这几个，
    agent 侧三执行器共用的这张表没跟着改（它的注释自己写着"统一入口"）。
    """
    from singularity.scheduler.executors.base import is_blocked_path
    blocked, reason = is_blocked_path(path)
    assert blocked, f"{path!r} 没被拦 —— 私钥能整读进上下文"


@pytest.mark.parametrize("path", [
    "src/main.py", "README.md", "tests/test_ok.py", "docs/notes.md",
])
def test_正常文件别误伤(path):
    """对照：别把闸门修成"什么都读不了"。"""
    from singularity.scheduler.executors.base import is_blocked_path
    blocked, _ = is_blocked_path(path)
    assert not blocked, f"{path!r} 被误拦了"


# ═══════════════════════════════════════════════════════════════
# ② 模块级 `_run_command` 必须脱敏环境变量
# ═══════════════════════════════════════════════════════════════

def test_模块级_run_command_要脱敏_env(monkeypatch, tmp_path):
    """真跑一条 `env`：敏感变量不许出现在子进程的输出里。

    不脱敏的后果：anthropic 执行器把 `run_command` 派到这个模块级函数
    （类方法 `_tool_run` 有 `env=safe_env`、这条路没有）⇒ 模型一条 `env`
    就能读走 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`。
    """
    from singularity.scheduler.executors.openai_agent import _run_command
    monkeypatch.setenv("MY_TEST_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_TEST_PLAIN", "visible-ok")

    out = _run_command({"command": "env"}, str(tmp_path))

    assert "sk-should-not-leak" not in out, f"敏感变量漏给子进程了：{out[:400]}"
    assert "visible-ok" in out, "把正常变量也滤掉了 —— 过度过滤会把模型跑的命令弄坏"


# ═══════════════════════════════════════════════════════════════
# ⑤ anthropic：每轮按剩余预算封顶
# ═══════════════════════════════════════════════════════════════

def test_anthropic_每轮都要重算超时(monkeypatch):
    """`_tmo` 必须在**循环内**按剩余预算重算，不能循环外算一次共用。

    循环外算一次 ⇒ 最坏 `max_turns`（默认 10）倍预算，照样撞穿任务死线
    被外面那把 900s 的刀无声收割。zhipu / openai_agent 都逐轮看表，只有这份没防。
    """
    from singularity.scheduler.executors import anthropic_api as A

    seen = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            # 每轮都返回一个 tool_use ⇒ 循环会继续到下一轮
            return {"usage": {}, "content": [
                {"type": "tool_use", "id": "t1", "name": "read_file",
                 "input": {"path": "x"}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append(timeout)
        return _Resp()

    # `httpx` 是在 run() 里 `import httpx` 的局部导入 ⇒ 只能打全局模块
    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    ex = A.AnthropicApiExecutor(agent_cfg={}, task="t", task_id="tid", cwd="/tmp",
                          skill_tools=[], mcp_tools=[], skill_prompt="")
    ex.budget_s = 30.0
    monkeypatch.setattr(ex, "_execute_tool", lambda name, args: "ok")
    monkeypatch.setattr(ex, "_get_changed_files", lambda: [])

    ex.run()

    assert len(seen) >= 2, f"没跑到第二轮，测不到递减：{seen}"
    # 每轮的超时都必须 ≤ 预算；且后面那轮要**比第一轮小**（时间真的在走）
    assert all(t <= 30.0 for t in seen), f"某轮超时越过了预算：{seen}"
    assert seen[1] < seen[0], f"第二轮没重算剩余预算，还在用第一轮的值：{seen}"


# ═══════════════════════════════════════════════════════════════
# ⑥ Observer WS 的默认绑定必须是回环（扫bug-02④ 的另一半）
# ═══════════════════════════════════════════════════════════════

def test_observer_默认绑回环不许对外():
    """`observer/server.py` 全文没有任何鉴权判定 ⇒ 默认绑 `0.0.0.0` 是 fail-open。

    ⚠️ **生产路径本来就是对的**（`web/app.py:2289` 显式传 `host="127.0.0.1"`），
    所以要钉的**不是"线上有没有在漏"**，而是**默认值往哪一侧倒** ——
    下一个省略 `host=` 的调用方直接就把一个无鉴权入口挂到公网上了。
    同族：batch2 那条 `/api/auth/status` 默认不许吐用户清单（fail-closed 默认值）。
    """
    import inspect

    from singularity.observer import config as OC
    from singularity.scheduler import bridge as B

    assert OC.DEFAULT_HOST == "127.0.0.1", \
        f"observer 的默认绑定位被改回对外了：{OC.DEFAULT_HOST}"
    default_host = inspect.signature(B.start_observer_server).parameters["host"].default
    assert default_host == "127.0.0.1", \
        f"start_observer_server 的默认 host 被改回对外了：{default_host}"


def test_observer_无鉴权这件事本身还开着(monkeypatch):
    """把已知状态**钉成测试**，而不是让它只活在文档里。

    这条**不是**在说"没问题"—— `server.py` 的 `_handler` 连上来就发 welcome、
    任何 action 都不验身份。生产上靠"只绑了回环"挡着。
    ⚠️ **真正的修法是加 token 握手（抄 `bridge.py:111-123`）+ 前端带上 token**，
    那要动 WS 协议和前端两侧，本轮**没做** —— 见 `OPEN.md`。
    所以这里钉的是"**回环绑定是当前唯一的防线**"：一旦绑定被放开，就没有第二道。
    """
    import pathlib
    from singularity.observer import server as S

    src = pathlib.Path(S.__file__).read_text(encoding="utf-8")
    assert "authenticate" not in src and "token" not in src.lower(), (
        "observer/server.py 里出现鉴权了 —— 那说明 token 握手已经补上，"
        "这条测试和 OPEN.md 里那条待办都该销账了"
    )


# ═══════════════════════════════════════════════════════════════
# ⑦ `_io.atomic_write_json` —— tmp 带 pid + 写入拿锁
# ═══════════════════════════════════════════════════════════════
# 外派 J 审 `防御模式.md` §46 时抓到：那条的修法**只落在 `project.py:479`**，
# 而 `_io.atomic_write_json` 是 api_store/tracker/_memory_core/_token_budget 的共用入口，
# tmp 仍是确定性命名、写入也不拿锁 —— §46 描述的那个竞态在共享层原样活着。

def test_原子写的tmp名要带pid(monkeypatch, tmp_path):
    """不带 pid ⇒ 两个独立进程共用同一个 `<name>.tmp`：

    A replace 成功后 tmp 就没了，B 的 replace 撞 ENOENT；或者两边写入交错，
    正式文件里多出半个 `}` → 解析失败 → 读侧静默跳过。
    """
    import os as _os
    from pathlib import Path as _P
    from singularity.scheduler import _io

    seen = {}
    real = _P.write_text

    def spy(self, *a, **k):
        seen.setdefault("tmp", self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(_P, "write_text", spy)
    monkeypatch.setattr(_os, "getpid", lambda: 4242)
    _io.atomic_write_json(tmp_path / "x.json", {"a": 1})

    assert "4242" in seen.get("tmp", ""), \
        f"tmp 名里没有 pid ⇒ 跨进程会撞（project.py:474-478 记着这条链）：{seen.get('tmp')}"
    assert not list(tmp_path.glob("*.tmp")), "写完没清干净 tmp 残留"


def test_原子写多线程并发不炸(tmp_path):
    """同进程多线程拿到的是**同一个 pid** ⇒ pid 后缀挡不住它们，必须还有锁。"""
    import json
    import threading
    from singularity.scheduler import _io

    p = tmp_path / "y.json"
    errs: list = []

    def w(n: int) -> None:
        try:
            for i in range(60):
                _io.atomic_write_json(p, {"n": n, "i": i})
        except Exception as e:            # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=w, args=(n,)) for n in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errs, f"并发写炸了（没锁时 replace 会撞 ENOENT）：{errs[:3]}"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["n"] in range(8), f"读出来的不是完整的一份：{data}"
