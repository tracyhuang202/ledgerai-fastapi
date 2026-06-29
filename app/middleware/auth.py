from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from app.core.config import get_settings

logger = logging.getLogger(__name__)
cfg = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)

@dataclass
class AuthContext:
    user_id: str
    firm_id: str
    role: str
    email: str

def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            cfg.supabase_jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except JWTError as e:
        logger.warning("JWT decode failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

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
    payload = _decode_jwt(credentials.credentials)
    return _extract_auth(payload)

async def require_admin(auth: AuthContext = Depends(require_auth)) -> AuthContext:
    if auth.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin permission required.")
    return auth
