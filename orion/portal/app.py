"""Same-origin private pilot dashboard. Accounts are provisioned by an operator."""

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
PRODUCTS = {"NIFTY", "BANKNIFTY", "GOLDM", "GOLD", "SILVERM", "SILVER", "CRUDEOIL", "CRUDEOILM"}


def defaults():
    return json.loads((Path(__file__).parent / "defaults.json").read_text())


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Strict):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=1, max_length=128)


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


class Control(Strict):
    enabled: bool = Field(strict=True)


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
            if request.headers.get("origin") != origin:
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
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    def identity(request, mutation=False):
        session = store.session(request.cookies.get("orion_session", ""))
        if not session:
            raise HTTPException(401, "Sign in required")
        if mutation and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
            raise HTTPException(403, "Session verification failed")
        return session

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html")

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
        session = identity(request, True)
        connections.cancel(session["user_id"])
        store.logout(request.cookies["orion_session"])
        response = JSONResponse({"ok": True})
        response.delete_cookie("orion_session", path="/")
        return response

    @app.get("/api/me")
    def me(request: Request):
        session = identity(request)
        return {**accounts.summary(session["user_id"]), "csrf": session["csrf"]}

    @app.put("/api/settings")
    def settings(body: Settings, request: Request):
        uid = identity(request, True)["user_id"]
        if body.index_channel and body.index_channel == body.commodity_channel:
            raise HTTPException(422, "Use different channels for the two provider profiles")
        if body.risk_per_trade > body.max_open_risk or body.risk_per_trade > body.paper_cash:
            raise HTTPException(422, "Per-trade risk must fit the open-risk budget and starting capital")
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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def factory():
    key_path = Path(os.environ["ORION_PORTAL_KEY_FILE"])
    if key_path.stat().st_mode & 0o077:
        raise ValueError("Encryption key file must be owner-only")
    return create_app(
        os.environ["ORION_PORTAL_DATA"], key_path.read_bytes().strip(), os.environ["ORION_PORTAL_ORIGIN"]
    )
