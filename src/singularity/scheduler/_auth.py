"""内部模块 — 多用户认证 & 权限。

Token-based auth + 三级角色 (admin/operator/viewer)。
持久化: .qidian/users.json
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from singularity.scheduler import config

_TOKEN_TTL = 30 * 86400  # 30 天过期


@dataclass
class User:
    id: str
    name: str
    token: str          # 明文 token，仅内存持有，不落盘
    role: str           # admin | operator | viewer
    created_at: float = 0.0
    token_hash: str = ""  # sha256 哈希，落盘用

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "token_hash": self.token_hash,
                "role": self.role, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        token_hash = d.get("token_hash", "")
        # 向后兼容: 旧格式有明文 token 无 hash → 现场哈希
        if not token_hash and d.get("token"):
            token_hash = _hash_token(d["token"])
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            token="",  # 从磁盘恢复的无明文
            token_hash=token_hash,
            role=d.get("role", "viewer"),
            created_at=d.get("created_at", 0.0),
        )

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "operator")

    @property
    def can_manage(self) -> bool:
        return self.role == "admin"

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > _TOKEN_TTL


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ponytail: v2 加盐哈希 — 新用户直接用; 旧用户认证时自动迁移
_TOKEN_SALT = "qidian-auth-v2"


def _hash_token_v2(token: str) -> str:
    return hashlib.sha256(_TOKEN_SALT.encode() + token.encode()).hexdigest()


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
                needs_migrate = any("token" in d and not d.get("token_hash") for d in data.get("users", []))
                for d in data.get("users", []):
                    u = User.from_dict(d)
                    self._users[u.id] = u
                    key = u.token_hash
                    self._token_map[key] = u
                # 旧格式迁移: 明文 token → 哈希存储
                if needs_migrate:
                    self._save()
            except Exception as e:
                witness.heartbeat('_auth', f'warn:{e}')

    def _save(self):
        config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
        data = {"users": [u.to_dict() for u in self._users.values()]}
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def bootstrap(self) -> User:
        """首次运行: 创建 admin 用户。明文 token 只在 console 打印一次。"""
        if self._users:
            return list(self._users.values())[0]
        token = secrets.token_hex(16)
        token_h = _hash_token_v2(token)
        admin = User(id="admin", name="管理员", token=token, token_hash=token_h,
                     role="admin", created_at=time.time())
        self._users["admin"] = admin
        self._token_map[token_h] = admin
        self._save()
        # 只在首次创建时打印明文 token
        print(f"[auth] admin token: {token[:8]}... (仅显示一次，请保存)")
        return admin

    def authenticate(self, token: str) -> Optional[User]:
        """哈希比对 + 过期检查。v2 优先，v1 兼容 → 命中后自动迁移。"""
        token_h_v2 = _hash_token_v2(token)
        token_h_v1 = _hash_token(token)
        for h, u in self._token_map.items():
            if h == token_h_v2:
                if u.expired:
                    return None
                return u
            if h == token_h_v1:
                if u.expired:
                    return None
                # 自动迁移: 旧哈希 → 新哈希
                del self._token_map[token_h_v1]
                u.token_hash = token_h_v2
                self._token_map[token_h_v2] = u
                self._save()
                return u
        # 向后兼容: 旧明文 token 直存 key → 现场哈希比对
        u = self._token_map.get(token)
        if u and not u.expired:
            return u
        return None

    def add_user(self, user_id: str, name: str, role: str = "viewer") -> User:
        token = secrets.token_hex(16)
        token_h = _hash_token_v2(token)
        u = User(id=user_id, name=name, token=token, token_hash=token_h,
                 role=role, created_at=time.time())
        self._users[user_id] = u
        self._token_map[token_h] = u
        self._save()
        return u

    def remove_user(self, user_id: str) -> bool:
        u = self._users.pop(user_id, None)
        if u:
            self._token_map.pop(u.token_hash, None)
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
