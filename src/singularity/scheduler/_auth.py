"""内部模块 — 多用户认证 & 权限。

Token-based auth + 三级角色 (admin/operator/viewer)。
持久化: .qidian/users.json
"""

from __future__ import annotations

import hashlib
import os
import re
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from singularity.scheduler import config, witness

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
                    if not u.token_hash:
                        # 既无 hash 也无明文 → 谁也认证不了；而且空 key 会进 _token_map，
                        # 使 `Authorization: Bearer `（空 token）命中该用户 → 越权形态。
                        continue
                    self._users[u.id] = u
                    self._token_map[u.token_hash] = u
                # 旧格式迁移: 明文 token → 哈希存储
                if needs_migrate:
                    self._save()
            except Exception as e:
                witness.warn('_auth', f'{e}')

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
        # 只在首次创建时打印明文 token —— 必须打**完整**的。
        # 原来打的是 token[:8]，于是"唯一一次显示"显示了个没法用的前缀：
        # 谁手里都没有完整 token（落盘只有哈希、API 只回 [:8]、to_dict 不含 token）
        # → 一开 QIDIAN_AUTH 就全员 401，且没有任何自助恢复通道。
        # 自部署工具的惯例就是 bootstrap 打一次全量 token（Jupyter 打 token URL 同理）。
        print(f"[auth] 管理员 token（仅此一次显示，请立刻保存）:\n  {token}")
        return admin

    def rotate_token(self, user_id: str) -> Optional[User]:
        """给已有用户换发新 token。明文只随返回值给出一次，调用方负责显示。

        这是 token 过期/丢失后的**唯一自助恢复通道**：TTL 是 30 天且不做滑动续期
        （换发才重置 created_at），到期后老 token 一律 401。没有这个方法就只能
        删掉 users.json 重新 bootstrap —— 那会连带废掉所有其他用户。

        不提供 HTTP 入口是**有意**的：能发 token 的接口一旦可被未鉴权调用就是提权洞；
        本地 CLI 需要文件系统访问权，天然就是授权。
        """
        u = self._users.get(user_id)
        if u is None:
            return None
        token = secrets.token_hex(16)
        token_h = _hash_token_v2(token)
        old_h = u.token_hash
        u.token = token
        u.token_hash = token_h
        u.created_at = time.time()   # 换发即重置 TTL
        self._token_map.pop(old_h, None)
        self._token_map[token_h] = u
        self._save()
        return u

    def authenticate(self, token: str) -> Optional[User]:
        """哈希比对 + 过期检查。v2 优先，v1 兼容 → 命中后自动迁移。"""
        if not token:
            # 空/None 一律拒。除了 None.encode() 会炸，更重要的是：若某条记录
            # 既无 hash 也无明文，_token_map 里会留下空 key，`Bearer `（空 token）
            # 就命中该用户 → 越权。_load 已不让空 hash 进表，这里再兜一道。
            return None
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


# ═══════════════════════════════════════════════════════════════
# WebSocket 的来源校验策略（2026-09-14）
# ═══════════════════════════════════════════════════════════════
# **为什么需要**：两个 WS 服务（`bridge` 的 5051 和 `observer` 的 8765）都绑在回环地址上，
# 但**回环挡不住浏览器** —— 用户访问的任何一个网页都能 `new WebSocket("ws://127.0.0.1:8765")`，
# 浏览器会把请求发出去（WS 不受 CORS 预检限制），而服务端原来**完全不看 `Origin`**。
# observer 那条尤其重：它认识的 action 里有 `chat`，而观察者的工具箱里有
# `create_task` / `delete_task` / `delete_failed_tasks` / `control_loop`
# ⇒ **随手打开的一个网页就能删任务、停调度循环**。
# `QIDIAN_AUTH` 默认是关的（`app.py:228`），所以 token 那一路在默认配置下不咬人；
# **能真正挡住这条路的只有 `Origin` 校验**。
#
# ⚠️ **必须把 `None` 放进允许列表**：`websockets` 的判定是
# 「遍历允许项，`== origin` 则放行，否则 `raise InvalidOrigin`」（`server.py:339-350`，
# 而且匹配用的是 **`fullmatch`**，不是 `match`）。
# 没带 `Origin` 头的客户端（websocat / 脚本 / 自己写的客户端）origin 是 `None` ——
# **不显式允许 `None` 就会被一起拒掉**。浏览器一定带 `Origin`，非浏览器一定不带，
# 所以"允许 None"放行的正是非浏览器客户端，而它们不是这条攻击的载体。
#
# ⚠️ **本机来源的判定只有这一份**（2026-09-14）：`web/app.py` 的 HTTP 门
# （CSRF 守卫 + CORS 头）原来自己又写了一份，**两份内容不同** ——
# HTTP 那份有 `0.0.0.0`、且 `urlparse().hostname` 会把主机名**转小写**；
# 这份正则没有 `0.0.0.0`、而且是**大小写敏感**的。同一个来源在两个门上
# 判定可以不同，而"挡网页删任务"这条路**只靠 Origin**（见上），松一格就是绕行口。
# 现在：主机名一份（`LOCAL_HOSTNAMES`），HTTP 门调 `is_local_origin()`，
# WS 门用从**同一个主机名表**拼出来的正则（websockets 只认精确串或 `re.Pattern`，
# 给不了它一个函数）。两边是否真一致，有测试逐条比。
LOCAL_HOSTNAMES = ("localhost", "127.0.0.1", "0.0.0.0", "::1")

# 正则**从上面那张表拼**出来（不是再抄一遍名字）：IPv6 回环在 URL 里带方括号
# （`http://[::1]:5050`），而 `urlparse().hostname` 给的、以及表里写的都是不带括号的
# `::1` —— 这一处括号差就是 09-14 修过的那个"永远匹配不上"。
_LOCAL_HOST_RE = "|".join(
    re.escape(h) if ":" not in h else re.escape(f"[{h}]") for h in LOCAL_HOSTNAMES)
_LOCAL_ORIGIN_RE = re.compile(
    rf"https?://(?:{_LOCAL_HOST_RE})(?::\d+)?",
    re.IGNORECASE)   # 浏览器会把 Origin 的主机名规范成小写，HTTP 那侧也是（urlparse），这里跟上


def is_local_origin(origin: str) -> bool:
    """`Origin`（或 `Referer`）是不是**本机 UI** 发出的。HTTP 门和 WS 门共用这一份。

    精确比主机名，不做前缀匹配 —— `http://127.0.0.1.evil.com` 必须判 False
    （那是 `startswith` 写法的经典绕过口）。
    """
    from urllib.parse import urlparse
    if not origin:
        return False
    try:
        hostname = urlparse(origin).hostname      # 会自动剥掉 `[::1]` 的方括号、转小写
    except Exception as e:
        # `Origin` 是**外部可控**的头，`urlparse` 对畸形 IPv6（`http://[::1`）会抛。
        # 判不了 = 不放行（安全的一侧），但要出声：一个畸形 Origin 本身就是
        # 值得看一眼的事件，静默吞掉会让它在日志里长得跟"没带 Origin"一样。
        witness.warn("auth", f"bad_origin_header:{type(e).__name__}"[:120])
        return False
    return bool(hostname) and hostname in LOCAL_HOSTNAMES


def ws_allowed_origins() -> list:
    """两个 WS 服务共用的 `origins=` 参数值。见上面那段说明。"""
    return [_LOCAL_ORIGIN_RE, None]


# ═══════════════════════════════════════════════════════════════
# WebSocket 的**逐连接鉴权**（2026-09-14）
# ═══════════════════════════════════════════════════════════════
# 上面那段 Origin 校验挡的是**浏览器**（任何网页都能 new WebSocket 连回环，
# WS 不受 CORS 预检限制）。但两个 WS 服务**全文没有任何 token 判定** ——
# 也就是说：`QIDIAN_AUTH=1` 时 HTTP 那侧全员要 token，**WS 这侧一个都不问**
# （配置说的"要鉴权"在 WS 上不成立）。这里补上，并且**和 HTTP 共用同一个开关**。

def auth_enabled() -> bool:
    """`QIDIAN_AUTH=1` 才要求鉴权 —— HTTP / WS 两个门**共用这一个判据**。

    ⚠️ 之前只有 `web/app.py` 里读了一遍环境变量（`_AUTH_ENABLED`），
    WS 那侧压根不读 ⇒ "同一个开关"只对一半的门成立。谁要判"开没开"都调这里。
    """
    return os.environ.get("QIDIAN_AUTH") == "1"


def _bearer_or_query_token(request) -> str:
    """从 WS 握手请求里取 token：优先 `Authorization: Bearer`，其次 `?token=`。

    两个都收是因为客户端有两种：**浏览器不能给 `new WebSocket()` 设自定义头**
    （只能挂 query），而脚本/websocat 用头更自然。
    """
    auth = request.headers.get("Authorization", "") or ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    path = getattr(request, "path", "") or ""
    if "?" in path:
        from urllib.parse import parse_qs, urlparse
        return (parse_qs(urlparse(path).query).get("token") or [""])[0]
    return ""


def ws_authorize(connection, request):
    """WS 握手鉴权，给 `websockets.serve(process_request=...)` 用。

    返回 `None` = 放行；返回一个 HTTP 响应 = 拒绝。
    `QIDIAN_AUTH` 没开（默认）⇒ 不要求 token，**行为与今天逐字相同**
    （真挡住浏览器的那道仍是 Origin 校验，见上面那段）。

    ⚠️ **这里不 catch 任何异常**是有意的：`websockets` 对 `process_request` 抛出的
    异常会**拒绝握手（500）**（`asyncio/server.py:148-157`）⇒ 抛 = 拒。
    反过来若在这里 `except: return None`，那就是"鉴权自己坏了就放行" —— fail-open。
    """
    if not auth_enabled():
        return None
    token = _bearer_or_query_token(request)
    if token and get_auth().authenticate(token):
        return None
    # 拒得**说清楚**：客户端要能一眼看出"是鉴权没过"而不是"服务端坏了"
    return connection.respond(401, "unauthorized: 需要 ?token=<token> 或 Authorization: Bearer\n")
