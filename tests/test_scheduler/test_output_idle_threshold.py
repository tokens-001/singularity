"""「只想不写」那把尺（`_OUTPUT_IDLE_LIMIT`）—— 2026-09-22 用户拍板 480s + 先劝一轮。

**来历**：`round-20260922` 实测 —— 去掉 240s 硬顶（`60a81e16`）之后，打转的调用
不再 240s 止损，而是**烧到剩余预算见底**（实测 cap 809~945s，代价 3.4~3.8 倍）。
而原来那把停滞尺（`_STALL_TIMEOUT`，90s）**认 `reasoning_content` 是进展**
（那是对的，"正在想"不是"卡住"），代价是"想了几万字、正文一个字没有"那种形状
它**永远掐不到**。所以需要第二把尺：**量多久没吐出正文/工具调用，思考不算**。

**阈值的取数**（`docs/轮次成绩单-20260922.md` §三，单轮 33 条分诊账）：
  · 未被掐的 25 条 `elapsed` 落在 20~222.8 秒
  · 打转被掐的 8 条落在 904~945 秒
⇒ 两组**完全不相交**；480 是前者的 2.15 倍余量。
⚠️ **圈数 `loops` 不能当判据**（实测重叠：正常 269~7451 vs 打转 448~3734）。

🔴 **接线比函数本身更容易断**：`_NoOutputError` 是 `_NetworkError` 的**子类**，
而 `_api_call` 对 `_NetworkError` 是**原样重试**的 ⇒ 漏掉 `_api_call` 里那条
`except _NoOutputError: raise`，"只想不写"会被静默重试掉，turn 循环里那支
"先劝一轮"**永远轮不到**（函数对 ≠ 接线通）。下面有用例专门钉这一条。
"""
import http.server
import threading
import time

import httpx
import pytest

from singularity.scheduler.executors import openai_agent as oa


def _executor(tid="t_output_idle"):
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor
    ex = OpenAIAgentExecutor({"model": "m", "api_key_env": "K"}, "测试任务", tid, cwd=".")
    ex._api_key = "k"
    ex._url = "http://x"
    ex._is_responses_api = False
    return ex


# ══════════════════════════════════════════════════════════════
# 一、走路：它必须仍是 `_NetworkError` 子类
# ══════════════════════════════════════════════════════════════

def test_只想不写仍是网络错误的子类():
    """捕获点写的是 `except (_NetworkError, _FormatError)` —— 改成平级的新异常，
    那几处就接不住、异常会直接穿出去（行为变了）。同 `_StalledError` 的理由。"""
    assert issubclass(oa._NoOutputError, oa._NetworkError)


# ══════════════════════════════════════════════════════════════
# 二、接线：`_api_call` **不许重试**它
# ══════════════════════════════════════════════════════════════

def test_api_call_不重试只想不写(monkeypatch):
    """🔴 **这条钉的是接线，不是函数**。

    `_NoOutputError` 是 `_NetworkError` 的子类，而 `_api_call` 那条
    `except (_NetworkError, _TransientError)` 会**原样重试** ⇒ 漏掉
    `except _NoOutputError: raise`，同一个模型拿同一个 prompt 会再打转一个
    `_OUTPUT_IDLE_LIMIT`，而 turn 循环那支"先劝一轮"**永远轮不到**。

    变异验证：删掉 `_api_call` 里那句 `except _NoOutputError: raise` → 红
    （`_api_call_once` 会被调多次，且中间会 sleep）。
    """
    ex = _executor()
    seen = []

    def _once(body):
        seen.append(1)
        raise oa._NoOutputError("只想不写 480s：一直没有正文/工具调用")

    monkeypatch.setattr(ex, "_api_call_once", _once)

    with pytest.raises(oa._NoOutputError):
        ex._api_call({"model": "m", "messages": []})

    assert len(seen) == 1, (
        f"`_NoOutputError` 被重试了 {len(seen)} 次 —— 原样重试 = 再打转一个 480 秒；"
        f"且 turn 循环里那支'先劝一轮'永远轮不到")


# ══════════════════════════════════════════════════════════════
# 三、尺子真的会触发（用**真 HTTP 服务器** —— 替身喂的是我猜的行为）
# ══════════════════════════════════════════════════════════════

class _OnlyReasoningServer:
    """一直在发**合法 SSE 行**，但 `delta` 里**只有 reasoning_content** ——
    正是"想了几万字、正文一个字没有"的那个形状。"""

    def __init__(self, feed_s: float, with_content: bool = False):
        self.feed_s = feed_s
        self.with_content = with_content

    def __enter__(self):
        feed_s, with_content = self.feed_s, self.with_content

        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                t_end = time.time() + feed_s
                i = 0
                try:
                    while time.time() < t_end:
                        if with_content:
                            body = 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'.encode()
                        else:
                            body = 'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}\n\n'.encode()
                        self.wfile.write(body)
                        self.wfile.flush()
                        i += 1
                        time.sleep(0.05)
                except Exception:
                    pass

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self.srv

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
        return False


class TestRuler:
    def _run(self, monkeypatch, srv, limit):
        ex = _executor()
        ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
        ex._deadline_at = time.time() + 60.0     # 别让"剩余预算"先到，要让这把尺先触发
        # 停滞那把尺调大：这一条要测的是**第二把**尺，别让它抢答
        monkeypatch.setattr(oa, "_STALL_TIMEOUT", 30.0)
        monkeypatch.setattr(oa, "_OUTPUT_IDLE_LIMIT", limit)
        monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
        t0 = time.time()
        try:
            with pytest.raises(oa._NoOutputError) as ei:
                ex._stream_call({"model": "m", "messages": []})
            return ei.value, time.time() - t0
        finally:
            pass

    def test_只想不写会被掐断(self, monkeypatch):
        """只吐 reasoning ⇒ 到 `_OUTPUT_IDLE_LIMIT` 就断，**不等服务器收工**。

        变异验证：删掉 `_lines()` 里那段 `_out_idle > _OUTPUT_IDLE_LIMIT` → 红
        （会一直等到服务器收工、然后正常返回一个空回答）。
        """
        with _OnlyReasoningServer(feed_s=8.0) as srv:
            err, elapsed = self._run(monkeypatch, srv, limit=1.0)
        assert elapsed < 4.0, f"{elapsed:.1f}s 才断 —— 服务器要吐 8 秒，说明没掐住"
        assert "只想不写" in str(err)

    def test_吐了正文就不掐(self, monkeypatch):
        """**反例**：正文一直在长 ⇒ 这把尺**不该**触发。

        它守的是"阈值定低了会误杀'想很久、然后一次吐完'的调用"那一侧 ——
        这条用例只证明"有产出就不掐"，**不能**证明 480 秒这个数选对了
        （那要靠 `docs/轮次成绩单-20260922.md` §三 的两组实测，不是靠这个测试）。
        """
        with _OnlyReasoningServer(feed_s=3.0, with_content=True) as srv:
            ex = _executor()
            ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
            ex._deadline_at = time.time() + 60.0
            monkeypatch.setattr(oa, "_STALL_TIMEOUT", 30.0)
            monkeypatch.setattr(oa, "_OUTPUT_IDLE_LIMIT", 1.0)
            monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
            try:
                ex._stream_call({"model": "m", "messages": []})
            except oa._NoOutputError as e:            # pragma: no cover
                pytest.fail(f"正文一直在长却被「只想不写」掐了：{e}")
            except Exception:
                pass                                   # 别的尺子/收尾异常与本条无关


# ══════════════════════════════════════════════════════════════
# 四、掐了之后：**先劝一轮，第二次才判失败**
# ══════════════════════════════════════════════════════════════

def test_先劝一轮_第二次才判失败(monkeypatch):
    """2026-09-22 用户拍板：「先注入『别想了、现在写文件』再给一轮」。

    判据三条：
      · 第一次掐断后**没有**直接失败，而是又调了一次（`len(bodies) == 2`）
      · 第二次那次请求里**带上了那句系统指令**（否则"劝"根本没发生）
      · 第二次还掐 ⇒ 判失败，**不再劝第三轮**（`_no_output_urged` 是局部变量）

    变异验证：
      · 去掉 `messages.append(...)` 那段 → 第二条断言红
      · 去掉 `_no_output_urged` 判断 → `len(bodies)` 变成 `max_turns`（不再等于 2）
    """
    ex = _executor()
    bodies = []

    def _boom(body):
        bodies.append(body)
        raise oa._NoOutputError("只想不写 480s：一直没有正文/工具调用")

    monkeypatch.setattr(ex, "_api_call", _boom)
    res = ex.run()

    assert len(bodies) == 2, f"劝了/调了 {len(bodies)} 次，期望 2 次（第一次掐 + 劝后一次）"
    urge = [m for m in bodies[1].get("messages", [])
            if str(m.get("content", "")).startswith("[系统]")]
    assert any("停止思考" in str(m.get("content")) for m in urge), \
        f"第二次请求里没有那句'别想了、现在写文件'：{[m.get('content') for m in urge]}"
    assert not res.success, "劝了两轮还不产出，应当判失败"


# ══════════════════════════════════════════════════════════════
# 五、阈值**不是常量 480** —— 预算快见底时它跟着剩余预算压下来
# ══════════════════════════════════════════════════════════════
#
# 🔴 **2026-09-27 真机**（`round-20260927d` 的 T6）：它 5 次调用 `cap` 只有
# **58~211 秒**，次次零产出被**预算**掐，而 480 秒那把尺**一秒都没等到**
# ⇒ 上面第四节那支"先劝一轮"**全历史响过 0 次**（`no_output_urge` 在
# `alerts.jsonl` 里 0 条），而同一形状的 `llm_spin_no_output` 响了 **19 次**。
# ⇒ 老形状又一例：**闸门装在一个不发生的条件上**。
# 修法：阈值取 `min(480, 剩余预算 − 收尾余量)` —— 预算够时**一个字不变**。

class TestBudgetAwareLimit:
    def test_阈值本身(self):
        """`min(480, 剩余预算 − 收尾余量)`。

        变异：把 `_output_idle_limit` 的 `min(...)` 去掉 ⇒ 下面几条红。
        """
        M = oa.config.TASK_WRAPUP_MARGIN_S
        assert oa._output_idle_limit(480 + M) == 480.0, "正好够 ⇒ 必须还是 480"
        assert oa._output_idle_limit(600.0) == 480.0, "富余 ⇒ 必须还是 480"
        assert oa._output_idle_limit(480 + M - 1) == 480 - 1, "差一点 ⇒ 开始压"
        # 🔴 **这一格的期望在 2026-09-27 真机之后反过来了**（`round-20260927f`）：
        #    原来写的是 `_output_idle_limit(158.0) == 158.0 - M`（= 68 秒）——
        #    而那正是把 T7 杀死的算法（判词 `只想不写 1s（阈值 1s，剩余预算 91s）`）。
        #    现在**低于判别力下界就不开口**。⇒ 断言反过来。
        assert oa._output_idle_limit(158.0) == 480.0, \
            "预算紧到判别力没了 ⇒ 该闭嘴，不该给个 68 秒的阈值去误杀正常调用"
        # 🔴 **这一格是回归判据**（第一版漏了它，被既有的 `test_吐了正文就不掐` 抓红）：
        # 剩余预算连余量都不够 ⇒ **返回 480、行为一字不变**。
        # 要是让它压到 0，阈值就是 0、`_out_idle > 0` 几乎立刻成立
        # ⇒ **连正文一直在长的正常调用也照杀**。没有余量可劝时，那劝不动，
        # 就该让预算那条路（`_over_budget`）按老样子收尾。
        assert oa._output_idle_limit(M) == 480.0, "正好等于余量 ⇒ 不变"
        assert oa._output_idle_limit(10.0) == 480.0, "不够余量 ⇒ 不变"
        assert oa._output_idle_limit(-5.0) == 480.0, "负数也一样"

    def test_判别力下界(self):
        """🔴 **有判别力才开口**（2026-09-27 真机 `round-20260927f`）。

        量出来的分界：**正常调用空转 20~222.8s、打转 904~945s** ⇒ 阈值压到
        222.8 以下分不出好坏，那时掐了只会误杀。实测被咬的一发：
        `只想不写 1s（阈值 1s，剩余预算 91s）` —— 1 秒没吐字在正常调用里太常见。

        判据：**下面那两条边界**（`刚好 ≥ 下界` 要压、`差一点` 要闭嘴）。
        变异：把 `if limit < _OUTPUT_IDLE_FLOOR: return _OUTPUT_IDLE_LIMIT` 那句删掉
        ⇒ 本条红。
        """
        M = oa.config.TASK_WRAPUP_MARGIN_S
        F = oa._OUTPUT_IDLE_FLOOR
        assert F > 222.8, "下界必须高于实测的正常上限 —— 否则就是拿它去猜"
        # 正好在下界上 ⇒ **要压**（判别力还在）
        assert oa._output_idle_limit(F + M) == F, "正好够下界 ⇒ 按预算压，别闭嘴"
        # 差一点 ⇒ **闭嘴**（这就是 T7 那一发所在的位置）
        assert oa._output_idle_limit(F + M - 1) == 480.0, "低于下界 ⇒ 不开口"
        # 真机上真出现过的那几个数，一个都不许再压
        for left in (91.0, 96.0, 105.0, 145.0, 161.0):
            assert oa._output_idle_limit(left) == 480.0, \
                f"剩余预算 {left}s 是老代码给出 1/6/15/55/71 秒的位置 ⇒ 现在必须闭嘴"

    def test_预算不够余量时那格还承重吗(self, monkeypatch):
        """🔴 **2026-09-28 补的钉子** —— 上面那句"不够余量 ⇒ 不变"原来**没有东西守着**。

        ## 它是怎么变成"没人守"的

        `b9571003`（09-27 上午）加这一格时，变异复核**确实是红的**：
        删掉它 ⇒ 阈值算成 0 ⇒ 既有那条 `test_吐了正文就不掐` 当场红。
        **但同一天下午 `d3972c61` 加了 `_OUTPUT_IDLE_FLOOR = 300`**，而
        `budget_left ≤ 余量` ⇒ 算出来的 `limit` **恒 ≤ 0** ⇒ `0 < 300` 恒成立
        ⇒ **下界把这一格整个盖住了**。
        ⇒ 从那以后，**删掉这一格，全量测试一条都不红**（2026-09-28 实测：9 passed）。

        ## 那它现在是死代码吗？—— 不是

        把下界调低（`QIDIAN_OUTPUT_IDLE_FLOOR` 是环境变量，文档写着"真机数据到了再调"）
        它就露出来：下界 = 0 时 `limit = 0`，`0 < 0` 为假 ⇒ 下界兜不住 ⇒
        `min(480, 0) = 0` ⇒ **阈值 0 秒，连正文一直在长的正常调用也照杀** ——
        正是 `b9571003` 要修的那个 bug。

        ⇒ 它是**配置相关的兜底**，不是冗余。这条测试就是让它变成"有人守的"。
        判据：删掉 `if budget_left <= config.TASK_WRAPUP_MARGIN_S:` 那两行 ⇒ 本条红。
        """
        M = oa.config.TASK_WRAPUP_MARGIN_S
        monkeypatch.setattr(oa, "_OUTPUT_IDLE_FLOOR", 0.0)   # 下界兜不住的那种配置
        assert oa._output_idle_limit(10.0) == 480.0, \
            "下界兜不住时这一格必须自己顶上 —— 否则阈值成了 0 秒，好调用被照杀"
        assert oa._output_idle_limit(-5.0) == 480.0, "负数同理"
        assert oa._output_idle_limit(M) == 480.0, "正好等于余量同理"

    def test_预算见底时提前掐断(self, monkeypatch):
        """**接线那半边**：`_lines()` 里真的用了这个阈值。

        变异：把那行的 `_output_idle_limit(_budget_left)` 换回 `_OUTPUT_IDLE_LIMIT`
        ⇒ 本条红（会一直等到服务器收工，白等 8 秒）。

        ⚠️ **`_OUTPUT_IDLE_FLOOR` 必须一起放小**（2026-09-27 加下界之后）：
        有了下界，**能被短测触发的最小阈值是 300 秒**，8 秒的桩怎么等都等不到。
        ⇒ 这里把**下界这个策略值**剥出去（另有 `test_判别力下界` 单独钉它），
        本条只钉"接线有没有用那个算出来的阈值"。
        """
        M = oa.config.TASK_WRAPUP_MARGIN_S
        with _OnlyReasoningServer(feed_s=8.0) as srv:
            ex = _executor()
            ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
            ex._deadline_at = time.time() + M + 5.0     # 剩余预算只够"余量 + 5 秒"
            monkeypatch.setattr(oa, "_STALL_TIMEOUT", 30.0)
            monkeypatch.setattr(oa, "_OUTPUT_IDLE_LIMIT", 480.0)   # **那把尺本身没动**
            monkeypatch.setattr(oa, "_OUTPUT_IDLE_FLOOR", 1.0)     # 见 docstring
            monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
            t0 = time.time()
            with pytest.raises(oa._NoOutputError):
                ex._stream_call({"model": "m", "messages": []})
            elapsed = time.time() - t0
        assert elapsed < 6.0, f"{elapsed:.1f}s 才断 —— 阈值没跟着剩余预算压下来"

    def test_对照_预算充裕时行为一个字不变(self, monkeypatch):
        """**反例**：预算充裕 ⇒ 阈值还是 480 ⇒ 这段思考流**不该**被掐。

        这条守的是本条改动里最该防的回归：**别把"预算感知"做成"动不动就掐"**
        —— 那会把"想很久、然后一次吐完"的正常调用误杀，比原来的病更贵。
        """
        with _OnlyReasoningServer(feed_s=3.0) as srv:
            ex = _executor()
            ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
            ex._deadline_at = time.time() + 600.0       # 预算充裕
            monkeypatch.setattr(oa, "_STALL_TIMEOUT", 30.0)
            monkeypatch.setattr(oa, "_OUTPUT_IDLE_LIMIT", 480.0)
            monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
            try:
                ex._stream_call({"model": "m", "messages": []})
            except oa._NoOutputError as e:            # pragma: no cover
                pytest.fail(f"预算充裕时不该被「只想不写」掐：{e}")
            except Exception:
                pass                                   # 别的尺子/收尾异常与本条无关
