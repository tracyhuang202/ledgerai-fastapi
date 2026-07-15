from __future__ import annotations
import logging
import httpx
from dataclasses import dataclass
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from app.core.config import get_settings

logger = logging.getLogger(__name__)
cfg = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)

# Cache JWKS keys
_jwks_cache: dict = {}

async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache:
        return _jwks_cache
    url = f"{cfg.supabase_url}/auth/v1/.well-known/jwks.json"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        _jwks_cache = resp.json()
    return _jwks_cache

def _decode_jwt(token: str, jwks: dict) -> dict:
    try:
        # Try each key in JWKS
        headers = jwt.get_unverified_header(token)
        kid = headers.get("kid")
        for key_data in jwks.get("keys", []):
            if kid and key_data.get("kid") != kid:
                continue
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
            return jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
    except Exception:
        pass

    # Fallback: try Legacy JWT Secret (HS256)
    try:
        from jose import jwt as jose_jwt
        return jose_jwt.decode(
            token,
            cfg.supabase_jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except Exception as e:
        logger.warning("JWT decode failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

@dataclass
class AuthContext:
    user_id: str
    firm_id: str
    role: str
    email: str

def _extract_auth(payload: dict) -> AuthContext:
    app_meta = payload.get("app_metadata") or {}
    firm_id = app_meta.get("firm_id")
    role = app_meta.get("firm_role", "member")
    user_id = payload.get("sub")
    email = payload.get("email", "")
    if not firm_id:
        raise HTTPException(status_code=403, detail="No firm associated.")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token.")
    return AuthContext(user_id=user_id, firm_id=firm_id, role=role, email=email)

async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> AuthContext:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization header missing.")
    jwks = await _get_jwks()
    payload = _decode_jwt(credentials.credentials, jwks)
    return _extract_auth(payload)

async def require_admin(auth: AuthContext = Depends(require_auth)) -> AuthContext:
    if auth.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin permission required.")
    return auth
    