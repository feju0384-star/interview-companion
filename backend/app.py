import asyncio
import contextlib
import io
import ipaddress
import os
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import qrcode
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audio import list_devices, list_microphones
from .config import ConfigStore, Settings
from .codex_backend import CodexBackend, CodexError
from .claude_backend import ClaudeError
from .engine import Engine
from .diagnostics import diagnose
from .providers import Providers
from .screen import ScreenError, ScreenMonitorOptions, ScreenRequest, capture_screen, list_monitors


ROOT = Path(__file__).resolve().parent.parent
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


def lan_addresses() -> list[str]:
    addresses = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            value = result[4][0]
            address = ipaddress.ip_address(value)
            if address.is_private and not address.is_loopback and not address.is_link_local:
                addresses.add(value)
    except OSError:
        pass
    return sorted(addresses)


def require_local(request: Request):
    if not request.client or request.client.host not in LOCAL_NAMES or request.url.hostname not in LOCAL_NAMES:
        raise HTTPException(403, "配对管理仅能在面试电脑的 localhost 页面完成")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "不允许跨站管理操作")


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=12000)


class StartBody(BaseModel):
    device_id: int | None = Field(default=None, ge=0)
    device_name: str | None = Field(default=None, max_length=500)


class PracticeStartBody(StartBody):
    item_id: str = Field(min_length=1, max_length=100)


class PracticeAnswerBody(BaseModel):
    item_id: str = Field(min_length=1, max_length=100)
    answer: str = Field(max_length=12000)


class HotkeyBody(BaseModel):
    enabled: bool
    request: ScreenRequest | None = None


class ScreenMonitorBody(BaseModel):
    enabled: bool
    options: ScreenMonitorOptions | None = None


def create_app(store: ConfigStore | None = None, provider_transport=None):
    store = store or ConfigStore()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=provider_transport, follow_redirects=False) as client:
            try:
                settings = store.load()
                config_error = ""
            except Exception:
                settings = Settings()
                config_error = "本机设置文件无法读取，已加载默认值；请重新填写设置"
            app.state.engine = Engine(settings, Providers(client))
            app.state.engine.state["error"] = config_error
            yield
            await app.state.engine.close()

    app = FastAPI(title="听答", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": "；".join(
            f"{'.'.join(str(x) for x in error['loc'][1:])}: {error['msg']}" for error in exc.errors()
        )})

    @app.get("/")
    async def index(request: Request):
        page = "index.html" if request.url.hostname in LOCAL_NAMES else "phone.html"
        return FileResponse(ROOT / "web" / page)

    @app.get("/phone")
    async def phone():
        return FileResponse(ROOT / "web" / "phone.html")

    @app.get("/health")
    async def health():
        return {"ok": True, "app": "interview-companion"}

    def engine() -> Engine:
        return app.state.engine

    admin = [Depends(require_local)]

    def require_phone(request: Request):
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "不允许跨站控制操作")
        # IP hosts keep an attacker-controlled DNS name from becoming a control origin.
        host = request.url.hostname
        if host not in LOCAL_NAMES:
            try:
                address = ipaddress.ip_address(host or "")
                if not address.is_private or address.is_unspecified or address.is_multicast:
                    raise ValueError()
            except ValueError:
                raise HTTPException(403, "请使用电脑提供的局域网连接地址")
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not token or len(token) > 200 or not token.isascii() or not secrets.compare_digest(token, engine().controller_token):
            raise HTTPException(403, "手机控制连接已失效，请重新扫描电脑二维码")

    def require_control(request: Request):
        if request.client and request.client.host in LOCAL_NAMES and request.url.hostname in LOCAL_NAMES:
            require_local(request)
        else:
            require_phone(request)

    control = [Depends(require_control)]

    def phone_settings():
        result = engine().settings.public()
        result.pop("codex_executable", None)
        result.pop("claude_executable", None)
        return result

    def pairing(request: Request):
        port = request.url.port or int(os.environ.get("INTERVIEW_PORT", "8765"))
        addresses = lan_addresses()
        urls = [f"http://{address}:{port}/phone#{engine().controller_token}" for address in addresses]
        return {"urls": urls, "local_url": f"http://127.0.0.1:{port}/phone#{engine().controller_token}"}

    @app.get("/api/bootstrap", dependencies=admin)
    async def bootstrap(request: Request):
        return {"token": engine().admin_token, "settings": engine().settings.public(),
                "pairing": await asyncio.to_thread(pairing, request), "snapshot": engine().snapshot()}

    @app.get("/api/controller/bootstrap", dependencies=[Depends(require_phone)])
    async def controller_bootstrap():
        return {"settings": phone_settings(), "snapshot": engine().snapshot()}

    @app.get("/api/devices", dependencies=control)
    async def devices():
        try:
            return {"devices": await asyncio.to_thread(list_devices)}
        except Exception as exc:
            detail = str(exc) if isinstance(exc, RuntimeError) else "无法列出系统声音设备，请检查耳机或扬声器连接"
            raise HTTPException(400, detail) from exc

    @app.post("/api/settings", dependencies=control)
    async def save_settings(settings: Settings, request: Request):
        async with engine().command_lock:
            require_control(request)
            if engine().state["screen_monitoring"]:
                raise HTTPException(409, "请先停止屏幕自动监测，再保存设置")
            if engine().state["listening"] or engine().busy():
                raise HTTPException(409, "请先停止监听和生成，再保存设置")
            old = engine().settings
            local = request.client and request.client.host in LOCAL_NAMES and request.url.hostname in LOCAL_NAMES
            if not local:
                for key in ("codex_executable", "claude_executable"):
                    if key in settings.model_fields_set and getattr(settings, key) != getattr(old, key):
                        raise HTTPException(403, "手机不能更换电脑上的可执行程序")
                    setattr(settings, key, getattr(old, key))
            # Changing endpoint never forwards an old provider's key to a new provider.
            if not settings.llm_api_key and settings.llm_base_url == old.llm_base_url:
                settings.llm_api_key = old.llm_api_key
            if not settings.asr_api_key and settings.asr_base_url == old.asr_base_url and settings.asr_provider == old.asr_provider:
                settings.asr_api_key = old.asr_api_key
            await asyncio.to_thread(store.save, settings)
            await engine().apply_settings(settings)
            return settings.public() if local else phone_settings()

    @app.get("/api/codex/status", dependencies=control)
    async def codex_status():
        async with engine().command_lock:
            return await engine().providers.codex.probe(engine().settings)

    @app.get("/api/codex/models", dependencies=control)
    async def codex_models():
        # Discovery uses its own short-lived process, never resetting the interview.
        backend = CodexBackend()
        try:
            async with asyncio.timeout(15):
                return {"models": await backend.list_models(engine().settings.model_copy())}
        except (CodexError, OSError, TimeoutError) as exc:
            detail = str(exc) if isinstance(exc, CodexError) else "无法读取 Codex 模型列表，请检查登录状态后刷新"
            raise HTTPException(400, detail) from exc
        finally:
            await backend.close()

    @app.get("/api/claude/status", dependencies=control)
    async def claude_status():
        try:
            return await engine().providers.claude.probe(engine().settings)
        except ClaudeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/context/compact", dependencies=control)
    async def compact_context():
        async with engine().command_lock:
            try:
                await engine().compact_context()
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/start", dependencies=control)
    async def start(body: StartBody):
        async with engine().command_lock:
            try:
                await engine().start(await resolve_source(body))
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    async def resolve_source(body: StartBody):
        if body.device_id is None or body.device_name is None:
            return body.device_id
        # Device indices can change after hotplug; do not capture an unrelated source.
        devices = await asyncio.to_thread(list_devices)
        matches = [device for device in devices if device["name"] == body.device_name]
        if len(matches) != 1:
            raise ValueError("声音设备已变化，请刷新列表后重新选择")
        return matches[0]["id"]

    @app.post("/api/source", dependencies=control)
    async def source(body: StartBody):
        async with engine().command_lock:
            try:
                await engine().switch_source(await resolve_source(body))
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/stop", dependencies=control)
    async def stop():
        async with engine().command_lock:
            await engine().stop()
        return {"ok": True}

    @app.post("/api/ask", dependencies=control)
    async def ask(body: AskBody):
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "请输入问题")
        async with engine().command_lock:
            try:
                return {"id": await engine().ask(question)}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.post("/api/demo", dependencies=control)
    async def demo():
        async with engine().command_lock:
            try:
                return {"id": await engine().ask("什么是数值孔径？", demo=True)}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.get("/api/screen/monitors", dependencies=control)
    async def screen_monitors():
        try:
            return {"monitors": await asyncio.to_thread(list_monitors)}
        except ScreenError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/screen/preview", dependencies=control)
    async def screen_preview(body: ScreenRequest, request: Request):
        async with engine().command_lock:
            require_control(request)
            try:
                return await asyncio.to_thread(capture_screen, body, preview=True)
            except ScreenError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.post("/api/screen/ask", dependencies=control)
    async def screen_ask(body: ScreenRequest, request: Request):
        async with engine().command_lock:
            require_control(request)
            try:
                return {"id": await engine().ask_screen(body)}
            except (ValueError, ScreenError) as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.post("/api/screen/hotkey", dependencies=control)
    async def screen_hotkey(body: HotkeyBody, request: Request):
        if body.enabled and body.request is None:
            raise HTTPException(400, "请先选择截图屏幕")
        async with engine().command_lock:
            require_control(request)
            try:
                await engine().set_screen_hotkey(body.request if body.enabled else None)
            except (ValueError, ScreenError) as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"enabled": engine().state["screen_hotkey"]}

    @app.post("/api/screen/monitor", dependencies=control)
    async def screen_monitor(body: ScreenMonitorBody, request: Request):
        if body.enabled and body.options is None:
            raise HTTPException(400, "请先选择屏幕和自动监测设置")
        async with engine().command_lock:
            require_control(request)
            try:
                await engine().set_screen_monitor(body.options if body.enabled else None)
            except (ValueError, ScreenError) as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"enabled": engine().state["screen_monitoring"]}

    @app.post("/api/clear", dependencies=control)
    async def clear():
        async with engine().command_lock:
            await engine().clear()
        return {"ok": True}

    @app.get("/api/microphones", dependencies=control)
    async def microphones():
        try:
            return {"devices": await asyncio.to_thread(list_microphones)}
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/diagnostics", dependencies=control)
    async def diagnostics(request: Request):
        addresses = await asyncio.to_thread(lan_addresses)
        return await diagnose(engine().settings.model_copy(), addresses, request.url.port or 80,
                              engine().state["controller_count"])

    @app.post("/api/practice/start", dependencies=control)
    async def practice_start(body: PracticeStartBody, request: Request):
        async with engine().command_lock:
            require_control(request)
            try:
                device_id = body.device_id
                if device_id is not None:
                    sources = await asyncio.to_thread(list_microphones)
                    matches = [d for d in sources if d["id"] == device_id and (body.device_name is None or d["name"] == body.device_name)]
                    if len(matches) != 1:
                        raise ValueError("麦克风列表已变化，请刷新后重新选择")
                await engine().practice.start(body.item_id, device_id)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/practice/question", dependencies=control)
    async def practice_question(body: AskBody, request: Request):
        async with engine().command_lock:
            require_control(request)
            try:
                item = engine().practice.add_question(body.question)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"id": item["id"]}

    @app.post("/api/practice/finish", dependencies=control)
    async def practice_finish(request: Request):
        async with engine().command_lock:
            require_control(request)
            try:
                await engine().practice.finish_recording()
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/practice/answer", dependencies=control)
    async def practice_answer(body: PracticeAnswerBody, request: Request):
        async with engine().command_lock:
            require_control(request)
            try:
                engine().practice.save(body.item_id, body.answer)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/practice/review", dependencies=control)
    async def practice_review(body: PracticeAnswerBody, request: Request):
        async with engine().command_lock:
            require_control(request)
            try:
                await engine().practice.review(body.item_id, body.answer)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/shutdown", dependencies=admin)
    async def shutdown():
        async with engine().command_lock:
            await engine().close()
            engine().rotate_controller()
        hook = getattr(app.state, "shutdown_hook", None)
        if hook:
            hook()
        return {"ok": True}

    @app.post("/api/pairing/rotate", dependencies=admin)
    async def rotate(request: Request):
        async with engine().command_lock:
            await engine().stop()
            engine().rotate_controller()
        return await asyncio.to_thread(pairing, request)

    @app.get("/api/qr", dependencies=admin)
    async def qr(request: Request, index: int = -1):
        info = await asyncio.to_thread(pairing, request)
        if index >= len(info["urls"]) or index < -1:
            raise HTTPException(400, "无效的网络地址")
        url = info["urls"][index] if index >= 0 else (info["urls"][0] if info["urls"] else info["local_url"])
        buffer = io.BytesIO()
        qrcode.make(url, box_size=5, border=2).save(buffer, format="PNG")
        return Response(buffer.getvalue(), media_type="image/png")

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        origin = ws.headers.get("origin")
        expected_origin = f"{'https' if ws.url.scheme == 'wss' else 'http'}://{ws.headers.get('host')}"
        if origin and origin != expected_origin:
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = None
        sender_task = receiver_task = None
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
            token = hello.get("token", "") if isinstance(hello, dict) else ""
            if not isinstance(token, str) or not token.isascii() or len(token) > 200:
                await ws.close(code=1008)
                return
            local_admin = ws.client and ws.client.host in LOCAL_NAMES and ws.url.hostname in LOCAL_NAMES
            if local_admin and secrets.compare_digest(token, engine().admin_token):
                role = "admin"
            elif secrets.compare_digest(token, engine().controller_token):
                role = "controller"
            else:
                await ws.close(code=1008)
                return
            queue = engine().subscribe(role)

            async def sender():
                while True:
                    event = await queue.get()
                    await asyncio.wait_for(ws.send_json(event), timeout=10)
                    if event["type"] == "revoked":
                        await ws.close(code=1008)
                        return

            async def receiver():
                while True:
                    data = await ws.receive_text()
                    if data == "ping":
                        await ws.send_json({"type": "pong"})
                    else:
                        await ws.close(code=1008)
                        return

            sender_task = asyncio.create_task(sender())
            receiver_task = asyncio.create_task(receiver())
            done, _ = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, TimeoutError, ValueError, RuntimeError):
            pass
        finally:
            for task in [sender_task, receiver_task]:
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError, TimeoutError):
                        await task
            if queue:
                engine().unsubscribe(queue)

    return app


app = create_app()
