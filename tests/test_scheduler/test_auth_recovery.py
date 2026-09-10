"""鉴权的可恢复性 —— 锁死回归测试（2026-09-11 审计）。

**这个 bug**：token 是纯哈希落盘的，而明文**从来没被完整输出过** ——
`bootstrap()` 打的是 `token[:8]`（而这是"仅显示一次"）、API 只回 `[:8]`、
`to_dict()` 不含 token。加上 30 天 TTL 且**没有任何续期/换发入口**，
于是 `QIDIAN_AUTH=1` 一开就是全员 401，且无自助恢复路径。

实测存量 `.qidian/users.json` 的 admin 已过期 83.17 天 —— 打开即锁死。

修法：
1. `bootstrap()` 打**完整** token（自部署工具的惯例，Jupyter 打 token URL 同理）
2. 加 `rotate_token(user_id)` + CLI `scheduler auth token <id>` —— 过期/丢失后的恢复通道
3. 堵住"空 token 命中 `_token_map[""]`"的越权形态

**关于隔离**：`_auth.py` 在 **import 时**就建了 `_auth = AuthStore()`，
路径在那时绑死（与 `_token_budget` / `_profiler` 同一类"导入时固定"）。
所以本文件不走 conftest 的路径覆盖，而是**显式新建 store 再替换单例**。

**在旧代码上会红、且红得对**（断言失败）：`test_bootstrap_prints_full_token`
会因打印出来的东西认证不了而失败。
"""
import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import _auth as auth_mod       # noqa: E402
from singularity.scheduler import config as cfg           # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """在隔离目录上新建 AuthStore 并替换模块单例。"""
    monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
    s = auth_mod.AuthStore()
    monkeypatch.setattr(auth_mod, "_auth", s)
    monkeypatch.setattr(auth_mod, "_bootstrapped", True)
    return s


def _bootstrap_capture_token(s) -> str:
    """跑 bootstrap，从 stdout 里把 token 抠出来。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        s.bootstrap()
    return buf.getvalue().strip().splitlines()[-1].strip()


class TestBootstrap:
    def test_bootstrap_prints_full_token(self, store):
        """打印出来的必须是一个**能用的**完整 token（旧代码只打 [:8]）。"""
        token = _bootstrap_capture_token(store)
        assert len(token) >= 32, f"只拿到 {len(token)} 字符，这是个没法用的前缀"
        assert store.authenticate(token) is not None, "打印的 token 认证不了 —— 还是锁死"

    def test_truncated_prefix_does_not_authenticate(self, store):
        """对照：旧行为只给前缀，认证必然失败（这就是"永久锁死"）。"""
        token = _bootstrap_capture_token(store)
        assert store.authenticate(token[:8]) is None


class TestRotate:
    def test_rotate_issues_working_token_and_kills_old(self, store):
        old = _bootstrap_capture_token(store)
        new = store.rotate_token("admin")
        assert new is not None and len(new.token) == 32
        assert store.authenticate(new.token) is not None
        assert store.authenticate(old) is None, "旧 token 没被作废"

    def test_rotate_unknown_user_returns_none(self, store):
        assert store.rotate_token("no-such-user") is None

    def test_expired_token_recovers_via_rotate(self, store):
        """端到端：过期 → 401 → 换发即恢复（原来无路可走，只能删 users.json）。"""
        token = _bootstrap_capture_token(store)
        store._users["admin"].created_at = time.time() - 40 * 86400
        assert store.authenticate(token) is None, "过期 token 竟然还能用"

        fresh = store.rotate_token("admin")
        assert store.authenticate(fresh.token) is not None, "换发后仍进不来 —— 还是锁死"


class TestNoBypass:
    def test_empty_token_rejected(self, store):
        _bootstrap_capture_token(store)
        assert store.authenticate("") is None
        assert store.authenticate(None) is None

    def test_user_without_any_token_is_skipped(self, tmp_path, monkeypatch):
        """既无 hash 也无明文的历史记录不能进 _token_map（否则空 key = 越权）。"""
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        (tmp_path / "users.json").write_text(json.dumps({"users": [
            {"id": "ghost", "name": "幽灵", "role": "admin", "created_at": time.time()},
        ]}))
        s = auth_mod.AuthStore()
        assert "ghost" not in s._users
        assert "" not in s._token_map
        assert s.authenticate("") is None


class TestCli:
    def test_cli_rotate(self, store, capsys):
        from singularity.scheduler import _cli_tasks as ct
        _bootstrap_capture_token(store)
        assert ct._cmd_auth(["list"]) == 0
        assert ct._cmd_auth(["token", "admin"]) == 0
        new_token = capsys.readouterr().out.strip().splitlines()[-1].strip()
        assert store.authenticate(new_token) is not None, "CLI 换发的 token 用不了"

    def test_cli_bad_args(self, store):
        from singularity.scheduler import _cli_tasks as ct
        assert ct._cmd_auth([]) == 2
        assert ct._cmd_auth(["token", "nobody"]) == 1
        assert ct._cmd_auth(["nonsense"]) == 2
