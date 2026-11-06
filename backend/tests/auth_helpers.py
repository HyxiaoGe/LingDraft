"""身份相关的测试工具：一对测试用的 RSA 钥匙充当 auth 服务（签 access token、公钥喂给验签器），游客令牌用固定密钥。
经接口跑的测试默认带 USER（登录用户 u1）的令牌；要游客、管理员、别的人用 bearer(...)。"""

from __future__ import annotations

import time
import uuid

import jwt
from auth_service_client import JWTValidator
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.identity import Authenticator, GuestTokens

AUTH_URL = "https://auth.test"
CLIENT_ID = "app_test"
KID = "test-key"
GUEST_SECRET = "test-guest-secret-" + "x" * 32  # HS256 的密钥要 32 字节以上，短了 PyJWT 会警告
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True) | {"kid": KID, "alg": "RS256", "use": "sig"}


def validator() -> JWTValidator:
    """像生产那样建的验签器，只是公钥不去取，直接喂进缓存（永不过期）。"""
    v = JWTValidator(jwks_url="http://auth.test/.well-known/jwks.json", issuer=AUTH_URL, audience=CLIENT_ID, cache_ttl=10**9,
                     require_token_type="access", leeway_seconds=5)
    v._jwks_cache, v._cache_time = {"keys": [_JWK]}, time.time()
    return v


def user_token(sub: str = "u1", admin: bool = False, email: str | None = None, sid: str = "sid1", ttl: int = 3600, **claims) -> str:
    """auth 服务签出来的 access token（照它的契约：sub、email、iss、aud、type、scopes、sid、iat/exp、jti）。claims 覆盖、追加字段。"""
    now = int(time.time())
    payload = {"sub": sub, "email": email if email is not None else f"{sub}@test", "iss": AUTH_URL, "aud": CLIENT_ID, "type": "access",
               "scopes": ["admin"] if admin else ["user"], "sid": sid, "iat": now, "exp": now + ttl, "jti": uuid.uuid4().hex, "auth_generation": 1}
    return jwt.encode(payload | claims, _KEY, algorithm="RS256", headers={"kid": KID})


def make_authenticator(revocations=None, login: bool = True) -> Authenticator:
    return Authenticator(GuestTokens(GUEST_SECRET, per_ip_daily=1000), validator() if login else None, revocations)


def guest_token() -> str:
    token, _ = GuestTokens(GUEST_SECRET, per_ip_daily=1000).issue("test")
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


USER = bearer(user_token())
ADMIN = bearer(user_token("root", admin=True))
