"""内部模块 — 多用户认证 & 权限。

Token-based auth + 三级角色 (admin/operator/viewer)。
持久化: .qidian/users.json
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from . import config


@dataclass
class User:
    id: str
    name: str
    token: str          # Bearer token
    role: str           # admin | operator | viewer
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "token": self.token,
                "role": self.role, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(**{k: d.get(k, "" if k != "created_at" else 0.0)
                      for k in ["id", "name", "token", "role", "created_at"]})

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "operator")

    @property
    def can_manage(self) -> bool:
        return self.role == "admin"


class AuthStore:
    def __init__(self):
        self._path = config.QIDIAN_DIR / "users.json"
        self._users: dict[str, User] = {}
        self._token_map: dict[str, User] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                for d in data.get("users", []):
                    u = User.from_dict(d)
                    self._users[u.id] = u
                    self._token_map[u.token] = u
            except Exception:
                pass

    def _save(self):
        config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
        data = {"users": [u.to_dict() for u in self._users.values()]}
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def bootstrap(self) -> User:
        """首次运行: 创建 admin 用户。"""
        if self._users:
            return list(self._users.values())[0]
        token = secrets.token_hex(16)
        admin = User(id="admin", name="管理员", token=token, role="admin",
                     created_at=time.time())
        self._users["admin"] = admin
        self._token_map[token] = admin
        self._save()
        return admin

    def authenticate(self, token: str) -> Optional[User]:
        return self._token_map.get(token)

    def add_user(self, user_id: str, name: str, role: str = "viewer") -> User:
        token = secrets.token_hex(16)
        u = User(id=user_id, name=name, token=token, role=role,
                 created_at=time.time())
        self._users[user_id] = u
        self._token_map[token] = u
        self._save()
        return u

    def remove_user(self, user_id: str) -> bool:
        u = self._users.pop(user_id, None)
        if u:
            self._token_map.pop(u.token, None)
            self._save()
            return True
        return False

    def list_users(self) -> list[dict]:
        return [{"id": u.id, "name": u.name, "role": u.role,
                 "created_at": u.created_at} for u in self._users.values()]


_auth = AuthStore()
_bootstrapped = False


def get_auth() -> AuthStore:
    global _bootstrapped
    if not _bootstrapped:
        _auth.bootstrap()
        _bootstrapped = True
    return _auth


def require_auth(request) -> tuple[Optional[User], Optional[str]]:
    """验证请求。返回 (user, error_msg)。"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, "缺少 Authorization: Bearer <token>"
    token = auth_header[7:]
    user = get_auth().authenticate(token)
    if not user:
        return None, "无效 token"
    return user, None


def require_write(request) -> tuple[Optional[User], Optional[str]]:
    user, err = require_auth(request)
    if err:
        return None, err
    if not user.can_write:
        return None, "权限不足: 需要 operator 或 admin"
    return user, None
