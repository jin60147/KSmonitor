import asyncio
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from pwdlib import PasswordHash
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./monitor.db")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
ALLOW_SIGNUP = os.getenv("ALLOW_SIGNUP", "true").lower() == "true"
MIN_INTERVAL_SECONDS = int(os.getenv("MIN_INTERVAL_SECONDS", "60"))
ALLOWED_HOSTS = {"keepsilentshhh.com", "www.keepsilentshhh.com"}

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

serializer = URLSafeSerializer(SECRET_KEY, salt="keepsilent-session")
password_hash = PasswordHash.recommended()
fernet = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None
worker_task = None


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[str] = mapped_column(String(40))
    watches: Mapped[list["Watch"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    notification: Mapped[Optional["NotificationSettings"]] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")


class Watch(Base):
    __tablename__ = "watches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(255), default="")
    size: Mapped[str] = mapped_column(String(40), default="")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=120)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(40), default="NEW")
    last_checked_at: Mapped[str] = mapped_column(String(40), default="")
    last_notified_at: Mapped[str] = mapped_column(String(40), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40))
    user: Mapped[User] = relationship(back_populates="watches")


class NotificationSettings(Base):
    __tablename__ = "notification_settings"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    discord_webhook_enc: Mapped[str] = mapped_column(Text, default="")
    telegram_token_enc: Mapped[str] = mapped_column(Text, default="")
    telegram_chat_id_enc: Mapped[str] = mapped_column(Text, default="")
    user: Mapped[User] = relationship(back_populates="notification")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encrypt_value(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not fernet:
        raise RuntimeError("ENCRYPTION_KEY 尚未設定")
    return fernet.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    if not value:
        return ""
    if not fernet:
        return ""
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


def current_user_id(request: Request) -> str | None:
    token = request.cookies.get("ks_session")
    if not token:
        return None
    try:
        data = serializer.loads(token)
        return data.get("uid")
    except (BadSignature, AttributeError):
        return None


def require_user(request: Request) -> User:
    uid = current_user_id(request)
    if not uid:
        raise HTTPException(401, "Unauthorized")
    with SessionLocal() as s:
        user = s.get(User, uid)
        if not user:
            raise HTTPException(401, "Unauthorized")
        s.expunge(user)
        return user


def validate_product_url(url: str) -> str:
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https"):
        raise ValueError("網址必須使用 http/https")
    if p.hostname not in ALLOWED_HOSTS:
        raise ValueError("目前只支援 KEEPSILENT 商品頁")
    if "/product/" not in p.path:
        raise ValueError("請貼 KEEPSILENT 商品頁網址")
    return url.strip()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def element_disabled(el) -> bool:
    attrs = el.attrs
    if "disabled" in attrs:
        return True
    if str(attrs.get("aria-disabled", "")).lower() == "true":
        return True
    classes = " ".join(attrs.get("class", []))
    blob = " ".join([
        classes,
        str(attrs.get("data-disabled", "")),
        str(attrs.get("data-stock", "")),
        str(attrs.get("data-state", "")),
    ]).lower()
    return any(x in blob for x in ["disabled", "soldout", "sold-out", "out-of-stock", "unavailable"])


def find_size_state(soup: BeautifulSoup, target: str):
    if not target:
        return None
    t = clean_text(target).upper()
    for el in soup.find_all(["option", "button", "label"]):
        txt = clean_text(el.get_text(" ", strip=True)).upper()
        val = clean_text(str(el.attrs.get("value", ""))).upper()
        if txt == t or val == t:
            return not element_disabled(el)
    for el in soup.find_all(["span", "div"]):
        txt = clean_text(el.get_text(" ", strip=True)).upper()
        if txt == t and len(txt) <= 10:
            return not element_disabled(el)
    html = str(soup)
    escaped = re.escape(target)
    patterns = [
        rf'"(?:size|name|value)"\s*:\s*"{escaped}".{{0,200}}?"(?:available|in_stock|isAvailable)"\s*:\s*(true|false)',
        rf'"(?:available|in_stock|isAvailable)"\s*:\s*(true|false).{{0,200}}?"(?:size|name|value)"\s*:\s*"{escaped}"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            return match.group(1).lower() == "true"
    return None


async def fetch_product_status(url: str, size: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KeepsilentStockMonitor/1.0; personal availability monitor)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True)).upper()
    h1 = soup.find("h1")
    title = clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    has_add = "ADD TO CART" in page_text
    has_sold = "SOLD OUT" in page_text
    has_coming = "COMING SOON" in page_text
    size_state = find_size_state(soup, size)

    if size:
        if size_state is False:
            status = "OUT_OF_STOCK"
        elif size_state is True and has_add:
            status = "IN_STOCK"
        elif size_state is True and not (has_sold or has_coming):
            status = "POSSIBLY_IN_STOCK"
        elif has_sold or has_coming:
            status = "OUT_OF_STOCK"
        else:
            status = "UNKNOWN"
    else:
        if has_add and not has_sold:
            status = "IN_STOCK"
        elif has_sold or has_coming:
            status = "OUT_OF_STOCK"
        else:
            status = "UNKNOWN"
    return title[:255], status


def notification_values(user_id: str):
    with SessionLocal() as s:
        settings = s.get(NotificationSettings, user_id)
        if not settings:
            return "", "", ""
        return (
            decrypt_value(settings.discord_webhook_enc),
            decrypt_value(settings.telegram_token_enc),
            decrypt_value(settings.telegram_chat_id_enc),
        )


async def deliver_notification(user_id: str, text: str):
    discord, tg_token, tg_chat_id = notification_values(user_id)
    errors = []
    async with httpx.AsyncClient(timeout=15) as client:
        if discord:
            try:
                r = await client.post(discord, json={"content": text})
                r.raise_for_status()
            except Exception as exc:
                errors.append(f"Discord: {exc}")
        if tg_token and tg_chat_id:
            try:
                r = await client.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat_id, "text": text, "disable_web_page_preview": False},
                )
                r.raise_for_status()
            except Exception as exc:
                errors.append(f"Telegram: {exc}")
    return errors


async def check_watch(watch_id: str):
    with SessionLocal() as s:
        watch = s.get(Watch, watch_id)
        if not watch or not watch.enabled:
            return
        previous = watch.status
        url, size, user_id = watch.url, watch.size, watch.user_id

    title = ""
    status = "ERROR"
    error = ""
    try:
        title, status = await fetch_product_status(url, size)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:800]

    notify = False
    with SessionLocal() as s:
        watch = s.get(Watch, watch_id)
        if not watch:
            return
        if title:
            watch.title = title
        watch.status = status
        watch.last_checked_at = utcnow()
        watch.last_error = error
        notify = previous not in ("IN_STOCK", "POSSIBLY_IN_STOCK") and status in ("IN_STOCK", "POSSIBLY_IN_STOCK")
        s.commit()
        label = watch.title or watch.url
        watch_url = watch.url
        watch_size = watch.size or "不限尺寸"

    if notify:
        text = (
            "✅ KEEPSILENT 補貨通知\n"
            f"{label}\n"
            f"尺寸：{watch_size}\n"
            f"狀態：{previous} → {status}\n"
            f"{watch_url}"
        )
        await deliver_notification(user_id, text)
        with SessionLocal() as s:
            watch = s.get(Watch, watch_id)
            if watch:
                watch.last_notified_at = utcnow()
                s.commit()


async def monitor_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            due_ids = []
            with SessionLocal() as s:
                watches = s.scalars(select(Watch).where(Watch.enabled == True)).all()  # noqa: E712
                for watch in watches:
                    due = True
                    if watch.last_checked_at:
                        try:
                            last = datetime.fromisoformat(watch.last_checked_at)
                            due = (now - last).total_seconds() >= max(MIN_INTERVAL_SECONDS, watch.interval_seconds)
                        except ValueError:
                            due = True
                    if due:
                        due_ids.append(watch.id)
            for wid in due_ids:
                await check_watch(wid)
                await asyncio.sleep(1)
        except Exception as exc:
            print("monitor_loop error:", exc)
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_task
    Base.metadata.create_all(engine)
    worker_task = asyncio.create_task(monitor_loop())
    yield
    worker_task.cancel()


app = FastAPI(title="KEEPSILENT Online Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user_id(request):
        return RedirectResponse("/", 303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "", "allow_signup": ALLOW_SIGNUP})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    with SessionLocal() as s:
        user = s.scalar(select(User).where(User.username == username))
        valid = bool(user and password_hash.verify(password, user.password_hash))
    if not valid:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "帳號或密碼錯誤", "allow_signup": ALLOW_SIGNUP},
            status_code=401,
        )
    response = RedirectResponse("/", 303)
    response.set_cookie(
        "ks_session", serializer.dumps({"uid": user.id}), httponly=True, samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", max_age=60 * 60 * 24 * 30,
    )
    return response


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if not ALLOW_SIGNUP:
        raise HTTPException(404)
    return templates.TemplateResponse("signup.html", {"request": request, "error": ""})


@app.post("/signup")
async def signup(request: Request, username: str = Form(...), password: str = Form(...), password2: str = Form(...)):
    if not ALLOW_SIGNUP:
        raise HTTPException(404)
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
        error = "帳號限 3–40 字元，可用英文小寫、數字、._-"
    elif len(password) < 8:
        error = "密碼至少 8 個字元"
    elif password != password2:
        error = "兩次密碼不一致"
    else:
        error = ""
    if error:
        return templates.TemplateResponse("signup.html", {"request": request, "error": error}, status_code=400)

    with SessionLocal() as s:
        if s.scalar(select(User).where(User.username == username)):
            return templates.TemplateResponse("signup.html", {"request": request, "error": "這個帳號已存在"}, status_code=409)
        user = User(id=uuid.uuid4().hex, username=username, password_hash=password_hash.hash(password), created_at=utcnow())
        s.add(user)
        s.commit()
    response = RedirectResponse("/", 303)
    response.set_cookie(
        "ks_session", serializer.dumps({"uid": user.id}), httponly=True, samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", 303)
    response.delete_cookie("ks_session")
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse("/login", 303)
    with SessionLocal() as s:
        user = s.get(User, uid)
        if not user:
            return RedirectResponse("/login", 303)
        watches = list(s.scalars(select(Watch).where(Watch.user_id == uid).order_by(Watch.created_at.desc())).all())
        settings = s.get(NotificationSettings, uid)
        discord_set = bool(settings and settings.discord_webhook_enc)
        telegram_set = bool(settings and settings.telegram_token_enc and settings.telegram_chat_id_enc)
        telegram_chat = decrypt_value(settings.telegram_chat_id_enc) if settings else ""
        username = user.username
    return templates.TemplateResponse("index.html", {
        "request": request,
        "username": username,
        "watches": watches,
        "discord_set": discord_set,
        "telegram_set": telegram_set,
        "telegram_chat": telegram_chat,
    })


@app.post("/watch")
async def add_watch(request: Request, url: str = Form(...), size: str = Form(""), interval_seconds: int = Form(120)):
    user = require_user(request)
    try:
        url = validate_product_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    interval_seconds = min(max(interval_seconds, MIN_INTERVAL_SECONDS), 3600)
    watch = Watch(
        id=uuid.uuid4().hex,
        user_id=user.id,
        url=url,
        size=clean_text(size),
        interval_seconds=interval_seconds,
        enabled=True,
        status="NEW",
        created_at=utcnow(),
    )
    with SessionLocal() as s:
        s.add(watch)
        s.commit()
    asyncio.create_task(check_watch(watch.id))
    return RedirectResponse("/", 303)


@app.post("/watch/{watch_id}/check")
async def manual_check(request: Request, watch_id: str):
    user = require_user(request)
    with SessionLocal() as s:
        watch = s.get(Watch, watch_id)
        if not watch or watch.user_id != user.id:
            raise HTTPException(404)
    await check_watch(watch_id)
    return RedirectResponse("/", 303)


@app.post("/watch/{watch_id}/toggle")
async def toggle_watch(request: Request, watch_id: str):
    user = require_user(request)
    with SessionLocal() as s:
        watch = s.get(Watch, watch_id)
        if not watch or watch.user_id != user.id:
            raise HTTPException(404)
        watch.enabled = not watch.enabled
        s.commit()
    return RedirectResponse("/", 303)


@app.post("/watch/{watch_id}/delete")
async def delete_watch(request: Request, watch_id: str):
    user = require_user(request)
    with SessionLocal() as s:
        watch = s.get(Watch, watch_id)
        if not watch or watch.user_id != user.id:
            raise HTTPException(404)
        s.delete(watch)
        s.commit()
    return RedirectResponse("/", 303)


@app.post("/settings/notifications")
async def save_notifications(
    request: Request,
    discord_webhook_url: str = Form(""),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    clear_discord: str = Form(""),
    clear_telegram: str = Form(""),
):
    user = require_user(request)
    with SessionLocal() as s:
        settings = s.get(NotificationSettings, user.id)
        if not settings:
            settings = NotificationSettings(user_id=user.id)
            s.add(settings)
        if clear_discord == "1":
            settings.discord_webhook_enc = ""
        elif discord_webhook_url.strip():
            settings.discord_webhook_enc = encrypt_value(discord_webhook_url)
        if clear_telegram == "1":
            settings.telegram_token_enc = ""
            settings.telegram_chat_id_enc = ""
        else:
            if telegram_bot_token.strip():
                settings.telegram_token_enc = encrypt_value(telegram_bot_token)
            if telegram_chat_id.strip():
                settings.telegram_chat_id_enc = encrypt_value(telegram_chat_id)
        s.commit()
    return RedirectResponse("/#notifications", 303)


@app.post("/settings/test")
async def test_notification(request: Request):
    user = require_user(request)
    errors = await deliver_notification(user.id, "🔔 KEEPSILENT Monitor 測試通知\n你的通知設定可以正常送出。")
    target = "/?test=error#notifications" if errors else "/?test=ok#notifications"
    return RedirectResponse(target, 303)
