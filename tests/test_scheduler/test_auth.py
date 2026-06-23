"""_auth.py 单元测试 — _hash_token / _hash_token_v2 / User expired。"""

import pytest
import time


class TestHashToken:
    def test_deterministic(self):
        from singularity.scheduler._auth import _hash_token
        assert _hash_token("abc") == _hash_token("abc")

    def test_different_inputs(self):
        from singularity.scheduler._auth import _hash_token
        assert _hash_token("abc") != _hash_token("def")

    def test_sha256_length(self):
        from singularity.scheduler._auth import _hash_token
        assert len(_hash_token("hello")) == 64  # sha256 hex = 64 chars


class TestHashTokenV2:
    def test_salted_deterministic(self):
        from singularity.scheduler._auth import _hash_token_v2
        assert _hash_token_v2("abc") == _hash_token_v2("abc")

    def test_different_from_v1(self):
        from singularity.scheduler._auth import _hash_token, _hash_token_v2
        assert _hash_token("abc") != _hash_token_v2("abc")


class TestUser:
    def test_not_expired(self):
        from singularity.scheduler._auth import User
        u = User(id="u1", name="test", token="t", role="user", created_at=time.time())
        assert not u.expired

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
