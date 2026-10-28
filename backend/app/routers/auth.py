from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4

from app.db.session import get_db
from app.db.redis_client import get_user_active_session_jti, set_user_active_session_jti
from app.models.user import User, UserRole
from app.schemas.auth import RegisterRequest, LoginRequest, TokenResponse, UserResponse
from app.services.auth_service import hash_password, verify_password, create_access_token
from app.dependencies import get_current_user
from app.websocket.manager import manager

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if data.role != UserRole.student:
        raise HTTPException(status_code=403, detail="教师账号不能通过公共注册创建，请联系平台管理员")
    result = await db.execute(select(User).where(User.username == data.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        display_name=data.display_name,
        role=data.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    session_jti = str(uuid4())
    token = create_access_token(str(user.id), user.role.value, jti=session_jti)
    try:
        await set_user_active_session_jti(str(user.id), session_jti)
    except RuntimeError:
        # Redis unavailable: keep backward-compatible login behavior.
        pass
    return TokenResponse(access_token=token, user=UserResponse.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == data.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    user_id = str(user.id)
    old_jti: str | None = None
    try:
        old_jti = await get_user_active_session_jti(user_id)
    except RuntimeError:
        old_jti = None

    session_jti = str(uuid4())
    token = create_access_token(user_id, user.role.value, jti=session_jti)
    try:
        await set_user_active_session_jti(user_id, session_jti)
    except RuntimeError:
        # Redis unavailable: keep backward-compatible login behavior.
        pass

    if old_jti and old_jti != session_jti:
        try:
            await manager.revoke_user_session(
                user_id=user_id,
                session_jti=old_jti,
                payload={
                    "type": "auth:session_revoked",
                    "reason": "logged_in_elsewhere",
                    "message": "你的账号已在其他设备登录，当前会话已失效",
                },
            )
        except Exception:
            # Best effort only: do not block login.
            pass
    return TokenResponse(access_token=token, user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return UserResponse.model_validate(current_user)
