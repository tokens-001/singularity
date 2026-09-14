"""_auth.py 单元测试 — _hash_token / _hash_token_v2 / User expired。"""

import pytest
import time


class TestHashToken:
    def test_deterministic(self):
        """同一个输入给同一个值 —— **而且换个输入得变**。

        ⚠️ 原来只有 `_hash_token("abc") == _hash_token("abc")` —— **任何确定性函数都满足**：
        把函数体删空、两边都返回 `None`，这条照样绿（2026-09-14 审计读到即坐实）。
        """
        from singularity.scheduler._auth import _hash_token
        h = _hash_token("abc")
        assert h == _hash_token("abc")
        assert h != _hash_token("abd"), "换个输入却没变 —— 那常函数也算'确定'了"

    def test_different_inputs(self):
        from singularity.scheduler._auth import _hash_token
        assert _hash_token("abc") != _hash_token("def")

    def test_sha256_length(self):
        from singularity.scheduler._auth import _hash_token
        assert len(_hash_token("hello")) == 64  # sha256 hex = 64 chars


class TestHashTokenV2:
    def test_salted_deterministic(self):
        """同 `test_deterministic`：确定性 + 换输入要变，两条一起才算数。"""
        from singularity.scheduler._auth import _hash_token_v2
        h = _hash_token_v2("abc")
        assert h == _hash_token_v2("abc")
        assert h != _hash_token_v2("abd"), "换个输入却没变"

    def test_different_from_v1(self):
        from singularity.scheduler._auth import _hash_token, _hash_token_v2
        assert _hash_token("abc") != _hash_token_v2("abc")


class TestUser:
    def test_not_expired(self):
        """**贴着 TTL 边界**的两侧都要对 —— 别把"差一点过期"也判成过期、也别反过来。

        ⚠️ 原来用 `created_at=time.time()`（离过期还有整整一个 TTL，30 天）⇒
        把 `expired` 的函数体删空（返回 None）、`not None` 仍是真 ⇒ **恒真**
        （2026-09-14 审计读到即坐实）。贴着边界才钉得住比较符和算术。
        """
        from singularity.scheduler._auth import User, _TOKEN_TTL
        fresh = User(id="u1", name="test", token="t", role="user",
                     created_at=time.time() - (_TOKEN_TTL - 60))
        assert not fresh.expired, "还差 60 秒才到期，却判成过期了"

        stale = User(id="u2", name="test", token="t", role="user",
                     created_at=time.time() - (_TOKEN_TTL + 60))
        assert stale.expired, "已经超期 60 秒了，却没判过期"

    def test_expired(self):
        from singularity.scheduler._auth import User
        u = User(id="u1", name="test", token="t", role="user", created_at=0)
        assert u.expired

    def test_role_admin_can_manage(self):
        from singularity.scheduler._auth import User
        u = User(id="u1", name="admin", token="t", role="admin", created_at=time.time())
        assert u.can_manage

    def test_role_admin_can_write(self):
        from singularity.scheduler._auth import User
        u = User(id="u1", name="admin", token="t", role="admin", created_at=time.time())
        assert u.can_write

    def test_role_operator_can_write(self):
        from singularity.scheduler._auth import User
        u = User(id="u1", name="op", token="t", role="operator", created_at=time.time())
        assert u.can_write
        assert not u.can_manage

    def test_role_viewer_cannot_write(self):
        from singularity.scheduler._auth import User
        u = User(id="u1", name="v", token="t", role="viewer", created_at=time.time())
        assert not u.can_write
        assert not u.can_manage
