"""
Auth Router — Ceraldi Group ERP
Login/Logout con bcrypt + PyJWT httpOnly cookie.
Singolo utente admin configurato via env.
"""
import os
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Response, Request, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/api", tags=["auth"])

ADMIN_EMAIL         = os.getenv("ADMIN_EMAIL", "")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
SECRET_KEY          = os.getenv("SECRET_KEY", "fallback-secret-change-me")
TOKEN_EXPIRE_HOURS  = 24 * 7   # 7 giorni


class LoginRequest(BaseModel):
    email: str
    password: str


def _make_token(email: str) -> str:
    payload = {
        "sub": email,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def verify_token(request: Request) -> str:
    """Verifica JWT da cookie o header Authorization. Ritorna email utente."""
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Non autenticato")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessione scaduta")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token non valido")


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    if body.email.lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=401, detail="Credenziali errate")

    if not ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD_HASH non configurato")

    password_ok = bcrypt.checkpw(
        body.password.encode(),
        ADMIN_PASSWORD_HASH.encode()
    )
    if not password_ok:
        raise HTTPException(status_code=401, detail="Credenziali errate")

    token = _make_token(body.email)

    # Cookie httpOnly (sicuro, non accessibile da JS)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=False,      # True in produzione con HTTPS
        samesite="lax",
        max_age=TOKEN_EXPIRE_HOURS * 3600,
        path="/",
    )
    # Cookie non httpOnly per il frontend (solo flag di sessione)
    response.set_cookie(
        key="session_active",
        value="1",
        httponly=False,
        secure=False,
        samesite="lax",
        max_age=TOKEN_EXPIRE_HOURS * 3600,
        path="/",
    )

    return {"ok": True, "email": body.email}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    response.delete_cookie("session_active")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    email = verify_token(request)
    return {"email": email, "ok": True}


@router.get("/auth/verify")
async def verify(request: Request):
    """Compatibilità AuthContext frontend: verifica sessione attiva."""
    email = verify_token(request)
    return {
        "ok":    True,
        "user":  {"email": email, "name": "Admin", "role": "admin"},
        "email": email,
    }


@router.post("/auth/login")
async def auth_login(body: LoginRequest, response: Response):
    """Alias /api/auth/login → /api/login per compatibilità frontend."""
    if body.email.lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=401, detail="Credenziali errate")
    if not ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD_HASH non configurato")
    password_ok = bcrypt.checkpw(body.password.encode(), ADMIN_PASSWORD_HASH.encode())
    if not password_ok:
        raise HTTPException(status_code=401, detail="Credenziali errate")
    token = _make_token(body.email)
    response.set_cookie(key="access_token", value=token, httponly=True,
                        secure=False, samesite="lax", max_age=TOKEN_EXPIRE_HOURS * 3600, path="/")
    response.set_cookie(key="session_active", value="1", httponly=False,
                        secure=False, samesite="lax", max_age=TOKEN_EXPIRE_HOURS * 3600, path="/")
    return {
        "ok":          True,
        "email":       body.email,
        "access_token": token,   # il frontend lo ignora (usa cookie)
        "user":        {"email": body.email, "name": "Admin", "role": "admin"},
    }


@router.post("/auth/logout")
async def auth_logout(response: Response):
    """Alias /api/auth/logout."""
    response.delete_cookie("access_token")
    response.delete_cookie("session_active")
    return {"ok": True}
