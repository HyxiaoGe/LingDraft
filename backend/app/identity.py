"""这个请求是谁：游客、登录用户、管理员（三档见 model_catalog.TIERS）。

登录走 Sean 自己的 auth 服务（auth-service）：页面用它的 SDK（auth-client-web）跳过去登录、换回 access token（RS256 JWT），
每个请求带 Bearer 过来，这里用它的 Python SDK（seanfield-auth-client）按 JWKS 验签，并照它的契约锁定签发者、本应用的 audience 和 type=access。
验过签再查它的 Redis 里的吊销标记（revoked_sid:{sid}、revoked_user:{sub}）：在别的应用退出了，这里下一个请求就拒绝；Redis 不通就放行，等 token 自己过期
（照 Fusion，见 fusion/backend/app/core/revocation.py）。
游客是灵稿自己签的令牌（HS256，签发者 GUEST_ISSUER），和 auth 的令牌按签发者分流；游客的 sub 带 guest: 前缀，库里一眼看得出哪些对话是游客的。
2026-11-06 Sean 定：游客能聊（只能选 Auto 和轻量模型），登录后游客期间的对话并进账号（见 ResearchRunner.claim）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import jwt
import redis
from auth_service_client import JWTValidator

from app.model_catalog import TIERS

GUEST, USER, ADMIN = TIERS
GUEST_ISSUER = "lingdraft-guest"
GUEST_PREFIX = "guest:"
GUEST_TTL_S = 365 * 86400  # 游客令牌一年：不常来的也能找回对话
_SID = re.compile(r"^[A-Za-z0-9_-]+$")

log = logging.getLogger(__name__)


class BadToken(Exception):
    """令牌没有、不对、过期、被吊销：接口回 401。"""


class AuthUnavailable(Exception):
    """验不了：取不到 auth 服务的公钥。接口回 503，页面稍后再试，不当成没登录。"""


@dataclass(frozen=True)
class Identity:
    sub: str  # 对话归谁：登录用户是 auth 服务里的用户 id，游客是 guest:<随机 id>
    tier: str  # guest / user / admin
    email: str = ""

    @property
    def guest(self) -> bool:
        return self.tier == GUEST


def is_guest_sub(sub: str | None) -> bool:
    return bool(sub) and sub.startswith(GUEST_PREFIX)


def _ip_key(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:16]  # 日志和内存里不留原地址


class GuestTokens:
    """签、验游客令牌。每个来源地址一天最多签 per_ip_daily 个（在内存里数，重启清零）。"""

    def __init__(self, secret: str, per_ip_daily: int, clock: Callable[[], float] = time.time) -> None:
        self.secret, self.per_ip_daily, self.clock = secret, per_ip_daily, clock
        self._mu = threading.Lock()
        self._day, self._issued = "", {}  # 哪一天 → {地址的摘要: 今天签了几个}

    def issue(self, ip: str) -> tuple[str, int] | None:
        """签一个新游客身份：返回（令牌, 过期时刻）；这个地址今天领够了返回 None。"""
        now = self.clock()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        key = _ip_key(ip or "")
        with self._mu:
            if day != self._day:
                self._day, self._issued = day, {}
            if self._issued.get(key, 0) >= self.per_ip_daily:
                log.info("游客身份：地址 %s 今天领够了（%d 个）", key, self.per_ip_daily)
                return None
            self._issued[key] = self._issued.get(key, 0) + 1
        sub = GUEST_PREFIX + uuid.uuid4().hex
        iat = int(now)
        exp = iat + GUEST_TTL_S
        token = jwt.encode({"iss": GUEST_ISSUER, "sub": sub, "iat": iat, "exp": exp}, self.secret, algorithm="HS256")
        return token, exp

    def verify(self, token: str) -> Identity:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], issuer=GUEST_ISSUER, options={"require": ["exp", "iat", "sub", "iss"]})
        except jwt.PyJWTError as e:
            raise BadToken(f"游客令牌不对：{e}") from e
        sub = claims.get("sub")
        if not isinstance(sub, str) or not is_guest_sub(sub):
            raise BadToken("游客令牌不对：sub")
        return Identity(sub, GUEST)


def is_guest_token(token: str) -> bool:
    """只看没验签的签发者来分流，信不信看 GuestTokens.verify。"""
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss") == GUEST_ISSUER
    except jwt.PyJWTError:
        return False


class Revocations:
    """auth 服务登出时写在它的 Redis 里的吊销标记（契约见 auth-service docs/AUTH_CONTRACT.md）。Redis 不通：记日志、放行。"""

    def __init__(self, url: str) -> None:
        self._redis = redis.from_url(url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1)

    def revoked(self, payload: dict) -> bool:
        sid, sub, iat = payload.get("sid"), payload.get("sub"), payload.get("iat")
        try:
            if isinstance(sid, str) and _SID.fullmatch(sid) and self._redis.get(f"revoked_sid:{sid}") is not None:
                return True
            if sub and iat is not None and (mark := self._redis.get(f"revoked_user:{sub}")) is not None:
                return float(iat) < float(mark)  # 标记是登出时刻（浮点秒），iat 是整秒：严格小于，登出前签的都算吊销
        except (redis.RedisError, OSError, ValueError) as e:
            log.warning("查吊销标记失败（Redis），放行：%s", e)
        return False


class Authenticator:
    """Bearer 令牌 → Identity。validator 是 auth 服务的 JWTValidator（没配登录时 None，只认游客）。"""

    def __init__(self, guests: GuestTokens, validator: JWTValidator | None = None, revocations: Revocations | None = None) -> None:
        self.guests, self.validator, self.revocations = guests, validator, revocations

    @property
    def login_enabled(self) -> bool:
        return self.validator is not None

    def identify(self, token: str) -> Identity:
        if is_guest_token(token):
            return self.guests.verify(token)
        if self.validator is None:
            raise BadToken("没有开登录")
        try:
            user = self.validator.verify(token)
        except jwt.PyJWTError as e:
            raise BadToken(f"登录令牌不对：{e}") from e
        except httpx.HTTPError as e:
            raise AuthUnavailable(f"取不到 auth 服务的公钥：{e}") from e
        if self.revocations and self.revocations.revoked(user.raw_payload):
            raise BadToken("已经退出登录")
        return Identity(user.sub, ADMIN if "admin" in (user.scopes or []) else USER, user.email or "")


def make_authenticator(auth_url: str, client_id: str, internal_url: str, redis_url: str, guest_secret: str, per_ip_daily: int) -> Authenticator:
    """按配置建（见 app.config 的 AUTH_*、LINGDRAFT_GUEST_*）。没配 client_id 就没有登录。"""
    if not guest_secret:
        log.warning("没配 LINGDRAFT_GUEST_SECRET：游客令牌用临时密钥，重启后游客要重新领身份，之前的对话找不回")
        guest_secret = uuid.uuid4().hex + uuid.uuid4().hex
    validator = revocations = None
    if client_id:
        validator = JWTValidator(jwks_url=f"{internal_url or auth_url}/.well-known/jwks.json", issuer=auth_url, audience=client_id,
                                 require_token_type="access", leeway_seconds=5)
        if redis_url:
            revocations = Revocations(redis_url)
        else:
            log.warning("没配 AUTH_REDIS_URL：别处退出登录后，这里的 token 要等它自己过期")
    return Authenticator(GuestTokens(guest_secret, per_ip_daily), validator, revocations)
