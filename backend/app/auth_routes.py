"""Authentication API: sign up, log in, log out, session info and password recovery."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core import auth
from app.core.auth import CurrentUser, require_user

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


class SignupRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)
    confirm_password: str = Field(..., min_length=1, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class ForgotPasswordRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    recovery_key: str = Field(..., min_length=1, max_length=64)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=1, max_length=256)
    confirm_password: str = Field(..., min_length=1, max_length=256)


def _session_payload(user_id: int, username: str, csrf_token: str) -> dict:
    return {"user": {"id": user_id, "username": username}, "csrf_token": csrf_token}


@router.post("/signup", status_code=201)
def signup(payload: SignupRequest, request: Request, response: Response):
    auth.ensure_same_origin(request)
    ip = auth.client_ip(request)
    if auth.signup_attempts.is_limited(ip):
        auth.raise_rate_limited()
    auth.signup_attempts.hit(ip)

    username = auth.validate_username(payload.username)
    auth.ensure_password_strength(payload.password, payload.confirm_password, username)
    user, recovery_key = auth.create_user(username, payload.password)

    token, csrf_token = auth.create_session(user["id"])
    auth.set_session_cookie(response, request, token)
    response.headers["Cache-Control"] = "no-store"
    return {
        **_session_payload(user["id"], user["username"], csrf_token),
        "recovery_key": recovery_key,
        "message": "Account created. Save your recovery key: it is shown only once and is required to reset your password.",
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response):
    auth.ensure_same_origin(request)
    ip = auth.client_ip(request)
    user_key = payload.username.strip().lower()
    if auth.login_failures_by_user.is_limited(user_key) or auth.login_failures_by_ip.is_limited(ip):
        auth.raise_rate_limited()

    user = auth.authenticate(payload.username, payload.password)
    if not user:
        auth.login_failures_by_user.hit(user_key)
        auth.login_failures_by_ip.hit(ip)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    auth.login_failures_by_user.reset(user_key)
    token, csrf_token = auth.create_session(user["id"])
    auth.set_session_cookie(response, request, token)
    response.headers["Cache-Control"] = "no-store"
    return _session_payload(user["id"], user["username"], csrf_token)


@router.post("/logout")
def logout(request: Request, response: Response):
    # Logging out needs no CSRF token: the worst a forged request can do is end a session.
    auth.ensure_same_origin(request)
    user = auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE_NAME))
    if user:
        auth.revoke_session(user.session_id)
    auth.clear_session_cookie(response, request)
    return {"status": "success", "message": "Logged out."}


@router.get("/me")
def me(response: Response, user: CurrentUser = Depends(require_user)):
    response.headers["Cache-Control"] = "no-store"
    return _session_payload(user.id, user.username, user.csrf_token)


@router.post("/password/forgot")
def forgot_password(payload: ForgotPasswordRequest, request: Request, response: Response):
    """
    Step 1 of recovery: exchange username + recovery key for a short-lived,
    single-use reset token. The same error is returned for unknown usernames
    and wrong keys.
    """
    auth.ensure_same_origin(request)
    ip = auth.client_ip(request)
    user_key = payload.username.strip().lower()
    if auth.recovery_attempts.is_limited(user_key) or auth.recovery_attempts.is_limited("ip:" + ip):
        auth.raise_rate_limited()
    auth.recovery_attempts.hit(user_key)
    auth.recovery_attempts.hit("ip:" + ip)

    issued = auth.request_reset_with_recovery_key(payload.username, payload.recovery_key)
    if not issued:
        raise HTTPException(status_code=400, detail="Username or recovery key is incorrect.")
    token, expires_at = issued
    response.headers["Cache-Control"] = "no-store"
    return {"reset_token": token, "expires_at": expires_at}


@router.post("/password/reset")
def reset_password(payload: ResetPasswordRequest, request: Request, response: Response):
    """Step 2 of recovery: set a new password with a valid reset token."""
    auth.ensure_same_origin(request)
    username, new_recovery_key = auth.reset_password_with_token(
        payload.token, payload.new_password, payload.confirm_password
    )
    auth.clear_session_cookie(response, request)
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": "success",
        "username": username,
        "recovery_key": new_recovery_key,
        "message": "Password updated. All sessions were signed out. Your old recovery key no longer works; save the new one.",
    }
