"""Same-origin private pilot dashboard. Isolated paper accounts with self-service registration."""

import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .store import Store
from .service import Accounts
from .connections import Connections, ConnectionError, connection_lease

STATIC = Path(__file__).parent / "static"
MOBILE_WEB = Path(__file__).parent / "mobile_web"
PRODUCTS = {"NIFTY", "BANKNIFTY", "GOLDM", "GOLD", "SILVERM", "SILVER", "CRUDEOIL", "CRUDEOILM"}


def defaults():
    return json.loads((Path(__file__).parent / "defaults.json").read_text())


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Strict):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class Register(Login):
    password: str = Field(min_length=12, max_length=128)


class Settings(Strict):
    paper_cash: float = Field(gt=0, le=100000000, allow_inf_nan=False)
    risk_per_trade: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    daily_loss_limit: float = Field(gt=0, le=10000000, allow_inf_nan=False)
    max_open_risk: float = Field(gt=0, le=10000000, allow_inf_nan=False)
    max_lots: int = Field(ge=1, le=100, strict=True)
    max_open_positions: int = Field(ge=1, le=10, strict=True)
    max_entries_per_day: int = Field(ge=1, le=100, strict=True)
    index_channel: str = Field(default="", pattern=r"^$|^-100[0-9]{4,16}$")
    commodity_channel: str = Field(default="", pattern=r"^$|^-100[0-9]{4,16}$")
    products: list[
        Literal["NIFTY", "BANKNIFTY", "GOLDM", "GOLD", "SILVERM", "SILVER", "CRUDEOIL", "CRUDEOILM"]
    ] = Field(min_length=1, max_length=8)


class Channel(Strict):
    id: str = Field(pattern=r"^-100[0-9]{4,16}$")
    name: str = Field(min_length=1, max_length=80)
    products: list[Literal["NIFTY", "BANKNIFTY", "GOLDM", "GOLD", "SILVERM", "SILVER", "CRUDEOIL", "CRUDEOILM"]] = Field(min_length=1, max_length=8)
    profile: Literal["index", "commodity"]


class Channels(Strict):
    channels: list[Channel] = Field(max_length=20)


class Risk(Strict):
    risk_per_trade: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    daily_loss_limit: float = Field(gt=0, le=10000000, allow_inf_nan=False)
    max_open_risk: float = Field(gt=0, le=10000000, allow_inf_nan=False)
    max_lots: int = Field(ge=1, le=100, strict=True)
    max_open_positions: int = Field(ge=1, le=10, strict=True)
    max_entries_per_day: int = Field(ge=1, le=100, strict=True)


class Access(Strict):
    access: Literal["approved", "suspended", "pending"]


class Mode(Strict):
    mode: Literal["paper", "live"]


class PushDevice(Strict):
    token: str = Field(pattern=r"^(ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_-]{10,200}\]$")


class Control(Strict):
    enabled: bool = Field(strict=True)


class Balance(Strict):
    amount: float = Field(ge=0, le=100000000, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=200)


class Credentials(Strict):
    kotak_consumer_key: str | None = Field(default=None, max_length=4096)
    kotak_mobile: str | None = Field(default=None, max_length=30)
    kotak_ucc: str | None = Field(default=None, max_length=50)
    kotak_mpin: str | None = Field(default=None, max_length=30)
    telegram_api_id: str | None = Field(default=None, max_length=20)
    telegram_api_hash: str | None = Field(default=None, max_length=100)


class TelegramStart(Strict):
    phone: str = Field(pattern=r"^\+[1-9][0-9]{6,14}$")


class TelegramCode(Strict):
    code: str = Field(pattern=r"^[0-9]{5,8}$")


class TelegramPassword(Strict):
    password: str = Field(min_length=1, max_length=256)


class KotakVerify(Strict):
    totp: str = Field(pattern=r"^[0-9]{6}$")


def create_app(root, key, origin):
    parsed = urlsplit(origin)
    if parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("Origin must contain only scheme, host and optional port")
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError("HTTPS required except for loopback development")
    os.umask(0o077)
    store = Store(root, key)
    accounts = Accounts(store, defaults())
    connections = Connections(store)

    @asynccontextmanager
    async def lifespan(app):
        async def cleanup():
            while True:
                await asyncio.sleep(30)
                connections.expire()

        task = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            connections.pending.clear()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.connections = connections

    @app.exception_handler(ConnectionError)
    async def connection_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    app.state.store = store
    app.state.accounts = accounts
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed.hostname])

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Invalid fields or values; check the form."}, status_code=422)

    @app.middleware("http")
    async def security(request, call_next):
        if request.method not in ("GET", "HEAD"):
            native_login = request.url.path in ("/api/mobile/login", "/api/mobile/register") and not request.headers.get("origin")
            bearer = request.headers.get("authorization", "").startswith("Bearer ") and not request.headers.get("origin")
            if request.headers.get("origin") != origin and not native_login and not bearer:
                return JSONResponse({"detail": "Origin rejected"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON required"}, status_code=415)
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 32768:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(data)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    def identity(request, mutation=False, approved=True):
        auth = request.headers.get("authorization", "")
        native = auth.startswith("Bearer ")
        token = auth[7:] if native else request.cookies.get("orion_session", "")
        session = store.session(token)
        if not session or session["client"] != ("mobile" if native else "web"):
            raise HTTPException(401, "Sign in required")
        if mutation and not native and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
            raise HTTPException(403, "Session verification failed")
        user = store.user(session["user_id"])
        if approved and user["access"] != "approved":
            raise HTTPException(403, "Account " + user["access"] + "; owner approval required")
        return session

    def owner(request, mutation=False):
        session = identity(request, mutation)
        if store.user(session["user_id"])["role"] != "owner":
            raise HTTPException(403, "Owner access required")
        return session["user_id"]

    @app.get("/")
    def home():
        return FileResponse((MOBILE_WEB if (MOBILE_WEB / "index.html").exists() else STATIC) / "index.html")

    @app.post("/api/mobile/register", status_code=201)
    @app.post("/api/register", status_code=201)
    def register(body: Register, request: Request):
        try:
            store.register(body.username, body.password, defaults(), request.client.host)
        except ValueError as exc:
            raise HTTPException(429 if str(exc) == "Try again later" else 400, str(exc)) from None
        return {"ok": True, "mode": "paper"}

    @app.post("/api/mobile/login")
    def mobile_login(body: Login, request: Request):
        # No cookies issued. Native bearer sessions cannot authenticate the browser surface.
        try:
            token, _ = store.login(body.username, body.password, request.client.host, client="mobile")
        except ValueError as exc:
            raise HTTPException(429 if str(exc) == "Try again later" else 401, str(exc)) from None
        return {"token": token, "expires_in": 28800, "mode": "paper"}

    @app.post("/api/login")
    def login(body: Login, request: Request):
        try:
            token, csrf = store.login(body.username, body.password, request.client.host)
        except ValueError as exc:
            raise HTTPException(429 if str(exc) == "Try again later" else 401, str(exc)) from None
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            "orion_session",
            token,
            max_age=28800,
            httponly=True,
            secure=parsed.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        token=request.headers.get("authorization", "")[7:] or request.cookies.get("orion_session", "")
        if request.headers.get("authorization", "").startswith("Bearer ") and not store.session(token):
            # Possession of an expired native token may revoke only its own device registration.
            store.logout(token)
        else:
            session = identity(request, True, approved=False)
            connections.cancel(session["user_id"])
            store.logout(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie("orion_session", path="/")
        return response

    @app.get("/api/me")
    def me(request: Request):
        session = identity(request, approved=False)
        return {**accounts.summary(session["user_id"]), "csrf": session["csrf"], "live_available": False}

    @app.get("/api/attention")
    def attention_status(request: Request):
        from .attention import Attention
        uid=identity(request, approved=False)["user_id"]
        return Attention(store).view(uid, owner=store.user(uid)["role"] == "owner")

    @app.put("/api/push-device")
    def register_device(body: PushDevice, request: Request):
        from .attention import Attention
        session=identity(request, True)
        if session["client"] != "mobile":
            raise HTTPException(403, "Use the installed mobile app to enable phone alerts")
        try:
            Attention(store).register(session["user_id"],body.token,request.headers["authorization"][7:])
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None
        return {"ok":True}

    @app.delete("/api/push-device")
    def remove_device(body: PushDevice, request: Request):
        from .attention import Attention
        uid=identity(request, True, approved=False)["user_id"]
        Attention(store).disable(uid,body.token)
        return {"ok":True}

    def performance_result(uid, period, instrument, start, end, group_by):
        try:
            return accounts.performance(uid,period=period,instrument=instrument,start=start,end=end,group_by=group_by)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None

    @app.get("/api/performance")
    def own_performance(request: Request, period: str="week", instrument: str="ALL", start: str | None=None, end: str | None=None, group_by: str="day"):
        uid=identity(request)["user_id"]
        return performance_result(uid,period,instrument,start,end,group_by)

    @app.get("/api/admin/users/{uid}/performance")
    def user_performance(uid: str, request: Request, period: str="week", instrument: str="ALL", start: str | None=None, end: str | None=None, group_by: str="day"):
        owner(request)
        if uid not in {u["id"] for u in store.users()}:
            raise HTTPException(404,"User not found")
        return performance_result(uid,period,instrument,start,end,group_by)

    @app.get("/api/activity")
    def own_activity(request: Request, before: int | None = None):
        from .analytics import activity
        uid = identity(request, approved=False)["user_id"]
        return activity(store.account_dir(uid) / "paper.db", before)

    @app.get("/api/admin/users/{uid}/activity")
    def user_activity(uid: str, request: Request, before: int | None = None):
        from .analytics import activity
        owner(request)
        if uid not in {u["id"] for u in store.users()}:
            raise HTTPException(404, "User not found")
        return activity(store.account_dir(uid) / "paper.db", before)

    @app.put("/api/mode")
    def mode(body: Mode, request: Request):
        identity(request, True)
        if body.mode != "paper":
            raise HTTPException(409, "Live trading is unavailable in this release")
        return {"mode": "paper", "live_available": False}

    @app.get("/api/admin/users")
    def admin_users(request: Request):
        owner(request)
        result = []
        for user in store.users():
            summary = accounts.summary(user["id"])
            result.append({**user, "enabled": summary["enabled"], "worker_online": summary["worker_online"],
                           "worker_health": summary["worker_health"], "pnl": summary["pnl"]["totals"]})
        return {"users": result, "mode": "paper", "audit": store.admin_history()}

    @app.get("/api/admin/users/{uid}")
    def admin_user(uid: str, request: Request):
        owner(request)
        if uid not in {u["id"] for u in store.users()}:
            raise HTTPException(404, "User not found")
        summary = accounts.summary(uid)
        summary.pop("credentials", None)
        return summary

    @app.put("/api/admin/users/{uid}/access")
    def admin_access(uid: str, body: Access, request: Request):
        actor = owner(request, True)
        if uid not in {u["id"] for u in store.users()}:
            raise HTTPException(404, "User not found")
        try:
            with store.lock(uid):
                store.set_access(actor, uid, body.access)
            accounts.set_enabled(uid, False)
            connections.cancel(uid)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.post("/api/admin/users/{uid}/pause")
    def admin_pause(uid: str, request: Request):
        actor = owner(request, True)
        if uid not in {u["id"] for u in store.users()}:
            raise HTTPException(404, "User not found")
        accounts.set_enabled(uid, False)
        store.audit_admin(actor, uid, "pause_entries")
        return {"ok": True}

    @app.put("/api/channels")
    def channels(body: Channels, request: Request):
        uid = identity(request, True)["user_id"]
        cfg = store.user(uid)["settings"]
        cfg["channels"] = {}
        for channel in body.channels:
            if channel.id in cfg["channels"]:
                raise HTTPException(422, "Choose each channel once")
            allowed = {"NIFTY", "BANKNIFTY"} if channel.profile == "index" else PRODUCTS - {"NIFTY", "BANKNIFTY"}
            if not set(channel.products) <= allowed:
                raise HTTPException(422, "Instruments must match the channel profile")
            cfg["channels"][channel.id] = {"name": channel.name, "profile": channel.profile,
                "products": sorted(set(channel.products)), "allow_overnight": True,
                "exit_time_ist": "15:15" if channel.profile == "index" else "22:45"}
        try:
            accounts.save_settings(uid, cfg)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.put("/api/risk")
    def risk(body: Risk, request: Request):
        uid = identity(request, True)["user_id"]
        if body.risk_per_trade > body.max_open_risk:
            raise HTTPException(422, "Per-trade risk must fit total open risk")
        cfg = store.user(uid)["settings"]
        cfg.update(body.model_dump())
        try:
            accounts.save_settings(uid, cfg)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.put("/api/settings")
    def settings(body: Settings, request: Request):
        uid = identity(request, True)["user_id"]
        if body.index_channel and body.index_channel == body.commodity_channel:
            raise HTTPException(422, "Use different channels for the two provider profiles")
        if body.risk_per_trade > body.max_open_risk:
            raise HTTPException(422, "Per-trade risk must fit the total open-risk budget")
        cfg = store.user(uid)["settings"]
        cfg["channels"] = {}
        for field in ("paper_cash", "risk_per_trade", "daily_loss_limit", "max_open_risk"):
            cfg[field] = str(getattr(body, field))
        for field in ("max_lots", "max_open_positions", "max_entries_per_day"):
            cfg[field] = getattr(body, field)
        for channel, name, allowed, cutoff in (
            (body.index_channel, "index-options", {"NIFTY", "BANKNIFTY"}, "15:15"),
            (body.commodity_channel, "commodity-options", PRODUCTS - {"NIFTY", "BANKNIFTY"}, "22:45"),
        ):
            selected = sorted(set(body.products) & allowed)
            if selected:
                if not channel:
                    raise HTTPException(422, "Enter the channel ID for every selected instrument group")
                cfg["channels"][channel] = {
                    "name": name,
                    "products": selected,
                    "allow_overnight": True,
                    "exit_time_ist": cutoff,
                }
        try:
            accounts.save_settings(uid, cfg)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.post("/api/paper-balance")
    def paper_balance(body: Balance, request: Request):
        uid = identity(request, True)["user_id"]
        try:
            accounts.adjust_balance(uid, body.amount, body.reason)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.put("/api/credentials")
    def credentials(body: Credentials, request: Request):
        uid = identity(request, True)["user_id"]
        patch = body.model_dump(exclude_unset=True)
        with connection_lease(store, uid), store.lock(uid):
            if store.user(uid)["enabled"]:
                raise HTTPException(409, "Pause entries and stop the worker before updating credentials")
            store.save_credentials(uid, patch)
            if any(k.startswith("telegram_") for k in patch):
                connections.cancel(uid)
        return store.credential_status(uid)

    @app.get("/api/connections")
    async def connection_status(request: Request):
        session = identity(request)
        return connections.status(session["user_id"], session["csrf"])

    @app.post("/api/connections/telegram/start")
    async def telegram_start(body: TelegramStart, request: Request):
        session = identity(request, True)
        return await connections.telegram(session["user_id"], session["csrf"], "start", body.phone)

    @app.post("/api/connections/telegram/code")
    async def telegram_code(body: TelegramCode, request: Request):
        session = identity(request, True)
        return await connections.telegram(session["user_id"], session["csrf"], "code", body.code)

    @app.post("/api/connections/telegram/password")
    async def telegram_password(body: TelegramPassword, request: Request):
        session = identity(request, True)
        return await connections.telegram(session["user_id"], session["csrf"], "password", body.password)

    @app.post("/api/connections/telegram/cancel")
    async def telegram_cancel(request: Request):
        session = identity(request, True)
        return await connections.telegram(session["user_id"], session["csrf"], "cancel")

    @app.post("/api/connections/telegram/channels")
    async def telegram_channels(request: Request):
        session = identity(request, True)
        return await connections.telegram(session["user_id"], session["csrf"], "channels")

    @app.post("/api/connections/kotak/verify")
    async def kotak_verify(body: KotakVerify, request: Request):
        session = identity(request, True)
        return await connections.kotak(session["user_id"], body.totp)

    @app.post("/api/control")
    def control(body: Control, request: Request):
        uid = identity(request, True)["user_id"]
        if body.enabled and not store.user(uid)["settings"]["channels"]:
            raise HTTPException(409, "Configure at least one channel and instrument first")
        accounts.set_enabled(uid, body.enabled)
        return {"enabled": body.enabled, "worker_online": store.worker_alive(uid)}

    @app.post("/api/demo")
    def demo(request: Request):
        uid = identity(request, True)["user_id"]
        return accounts.demo(uid)

    if MOBILE_WEB.exists():
        for folder in ("_expo", "assets"):
            if (MOBILE_WEB / folder).exists():
                app.mount("/" + folder, StaticFiles(directory=MOBILE_WEB / folder), name="mobile-" + folder)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def factory():
    key_path = Path(os.environ["ORION_PORTAL_KEY_FILE"])
    if key_path.stat().st_mode & 0o077:
        raise ValueError("Encryption key file must be owner-only")
    return create_app(
        os.environ["ORION_PORTAL_DATA"], key_path.read_bytes().strip(), os.environ["ORION_PORTAL_ORIGIN"]
    )
