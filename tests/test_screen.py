import asyncio
from contextlib import nullcontext
import json
from unittest.mock import AsyncMock

import httpx
from PIL import Image
import pytest
from pydantic import ValidationError

from backend.app import create_app
from backend.config import Settings
from backend.engine import Engine
from backend.prompts import SCREEN_INSTRUCTIONS, question_with_context
from backend.providers import ProviderError, Providers
from backend.screen import Region, ScreenError, ScreenRequest, capture_screen, encode_image, region_bounds
from tests.test_app import local_client, snapshot, store
from tests.test_codex import fake_backend


MONITOR = {"id": "monitor:1", "name": "test", "bounds": [-1920, 0, 0, 1080],
           "width": 1920, "height": 1080, "primary": True}
IMAGE = encode_image(Image.new("RGB", (200, 100), "white"))["image"]


def request(**kwargs):
    return ScreenRequest(monitor_id=MONITOR["id"], **kwargs)


def fake_capture(body, *, preview=False):
    return {"image": IMAGE, "width": 200, "height": 100, "monitor": MONITOR}


@pytest.mark.parametrize("values", [dict(x=-.1, y=0, width=1, height=1),
    dict(x=.9, y=0, width=.2, height=1), dict(x=0, y=0, width=0, height=1),
    dict(x=float("nan"), y=0, width=.5, height=1)])
def test_invalid_regions_rejected(values):
    with pytest.raises(ValidationError):
        Region(**values)


def test_negative_monitor_origin_and_crop_bounds():
    assert region_bounds(MONITOR, Region(x=.5, y=.25, width=.5, height=.5)) == (-960, 270, 0, 810)
    with pytest.raises(ScreenError, match="太小"):
        region_bounds(MONITOR, Region(x=0, y=0, width=.001, height=.001))


def test_capture_uses_physical_crop_rejects_changed_monitor_and_keeps_images_in_memory(monkeypatch):
    monkeypatch.setattr("backend.screen.physical_pixels", lambda: nullcontext(None))
    monkeypatch.setattr("backend.screen._monitors", lambda _: [MONITOR])
    seen = []
    def grab(*, bbox, all_screens):
        seen.append((bbox, all_screens))
        return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), "white")
    monkeypatch.setattr("backend.screen.ImageGrab.grab", grab)
    body = request(region=Region(x=.5, y=0, width=.5, height=1))
    result = capture_screen(body)
    assert seen[-1] == ((-960, 0, 0, 1080), True)
    assert result["width"] == 960 and result["image"].startswith("data:image/png;base64,")
    result = capture_screen(body, preview=True)
    assert seen[-1][0] == tuple(MONITOR["bounds"]) and result["width"] == 1280
    with pytest.raises(ScreenError, match="变化"):
        capture_screen(ScreenRequest(monitor_id="old-monitor"))
    monkeypatch.setattr("backend.screen.ImageGrab.grab", lambda **_: Image.new("RGB", (1, 1)))
    with pytest.raises(ScreenError, match="尺寸"):
        capture_screen(body)


def test_capture_errors_do_not_leak_os_details(monkeypatch):
    monkeypatch.setattr("backend.screen.physical_pixels", lambda: nullcontext(None))
    def fail(_):
        raise OSError("private-diagnostic")
    monkeypatch.setattr("backend.screen._monitors", fail)
    with pytest.raises(ScreenError, match="截图失败") as exc:
        capture_screen(request())
    assert "private-diagnostic" not in str(exc.value)


@pytest.mark.asyncio
async def test_api_picture_payload_uses_separate_prompt_and_no_resume():
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"B，解析"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    settings = Settings(llm_model="vision", llm_api_key="private", background="private-resume")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = Providers(client)
        result = "".join([part async for part in providers.answer_screen(IMAGE, request(backend="api", style="steps"), settings)])
        assert result == "B，解析"
        payload = seen[0]
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["content"] == SCREEN_INSTRUCTIONS
        assert payload["messages"][1]["content"][1]["image_url"]["url"] == IMAGE
        assert "详细解题步骤" in payload["messages"][1]["content"][0]["text"]
        assert "private-resume" not in json.dumps(payload)
        history = [{"source": "screen", "question": "screen-secret", "answer": "picture-answer", "status": "done"}]
        assert question_with_context("面试问题", history) == "面试问题"
        _ = [part async for part in providers.answer("面试问题", settings, history)]
        assert "picture-answer" not in json.dumps(seen[1]) and "screen-secret" not in json.dumps(seen[1])
        await providers.close()


@pytest.mark.asyncio
async def test_api_unsupported_image_is_actionable_and_sanitized():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400, text="secret"))) as client:
        providers = Providers(client)
        with pytest.raises(ProviderError, match="支持图片") as exc:
            _ = [part async for part in providers.answer_screen(IMAGE, request(backend="api"), Settings())]
        assert "secret" not in str(exc.value)
        await providers.close()


@pytest.mark.asyncio
async def test_codex_picture_context_closes_without_touching_interview():
    async with httpx.AsyncClient() as client:
        providers = Providers(client)
        providers.screen_codex = fake_backend()
        providers.screen_codex.instructions = SCREEN_INSTRUCTIONS
        providers.codex = fake_backend()
        settings = Settings(answer_backend="codex")
        _ = [part async for part in providers.answer("REMEMBER:interview-only", settings, [])]
        original = providers.codex.thread_id
        answer = "".join([part async for part in providers.answer_screen(IMAGE, request(), settings)])
        assert answer == "图片答案：B，解析示例。"
        assert providers.screen_codex.process is None
        assert providers.codex.thread_id == original
        result = "".join([part async for part in providers.answer("RECALL", settings, [])])
        assert result == "interview-only"
        await providers.close()


class SlowProviders:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def answer_screen(self, image, body, settings):
        assert image == IMAGE
        try:
            self.started.set()
            yield "部分答案"
            await asyncio.sleep(30)
            yield "迟到答案"
        finally:
            self.cancelled = True


@pytest.mark.asyncio
async def test_screen_cancel_prevents_late_answer_and_never_puts_image_in_snapshot(monkeypatch):
    monkeypatch.setattr("backend.engine.capture_screen", fake_capture)
    providers = SlowProviders()
    engine = Engine(Settings(), providers)  # Codex screen works without ASR/API keys.
    await engine.ask_screen(request())
    await providers.started.wait()
    await engine.stop()
    assert providers.cancelled and engine.items[-1]["status"] == "cancelled"
    assert engine.items[-1]["answer"] == "部分答案"
    assert "data:image" not in json.dumps(engine.snapshot())
    await engine.clear()
    assert not engine.items


@pytest.mark.asyncio
async def test_readiness_checked_before_capturing_and_failure_preserves_old_answer(monkeypatch):
    capture = AsyncMock()
    monkeypatch.setattr("backend.engine.capture_screen", capture)
    engine = Engine(Settings(), SlowProviders())
    with pytest.raises(ValueError, match="支持图片"):
        await engine.ask_screen(request(backend="api"))
    engine.state["listening"] = True
    with pytest.raises(ValueError, match="停止声音监听"):
        await engine.ask_screen(request())
    capture.assert_not_called()
    engine.state["listening"] = False
    def fail(_):
        raise ScreenError("截图失败")
    monkeypatch.setattr("backend.engine.capture_screen", fail)
    engine.items.append({"id": "kept", "answer": "原回答", "status": "done"})
    with pytest.raises(ScreenError):
        await engine.ask_screen(request())
    assert engine.items[-1]["answer"] == "原回答" and not engine.state["screen_capturing"]


class FakeHotkey:
    def __init__(self, callback):
        self.callback = callback
        self.stopped = False
    def start(self):
        pass
    def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_hotkey_no_capture_on_enable_debounces_and_stop_revokes(monkeypatch):
    monkeypatch.setattr("backend.engine.ScreenHotkey", FakeHotkey)
    monkeypatch.setattr("backend.engine.capture_screen", fake_capture)
    providers = SlowProviders()
    engine = Engine(Settings(), providers)
    await engine.set_screen_hotkey(request())
    hotkey = engine.screen_hotkey
    assert not engine.items
    engine.screen_hotkey_pressed()
    engine.screen_hotkey_pressed()
    await engine.screen_hotkey_task
    await providers.started.wait()
    engine.screen_hotkey_pressed()
    await engine.screen_hotkey_task
    assert len(engine.items) == 1 and "忽略" in engine.state["screen_notice"]
    await engine.stop()
    assert hotkey.stopped and not engine.state["screen_hotkey"]
    engine.screen_hotkey_pressed()
    assert engine.screen_hotkey_task is None and len(engine.items) == 1
    await engine.set_screen_hotkey(request())
    engine.rotate_controller()
    engine.screen_hotkey_pressed()
    assert not engine.state["screen_hotkey"] and engine.screen_hotkey_request is None


def test_screen_routes_require_auth_and_phone_receives_stream(store, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("backend.app.list_monitors", lambda: [MONITOR])
    monkeypatch.setattr("backend.app.capture_screen", fake_capture)
    monkeypatch.setattr("backend.engine.capture_screen", fake_capture)
    store.save(Settings(llm_model="vision", llm_api_key="private"))
    def handler(req):
        assert IMAGE in req.content.decode()
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"B，手机答案"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    app = create_app(store, httpx.MockTransport(handler))
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as phone:
        assert phone.get("/api/screen/monitors").status_code == 403
        for path in ["preview", "ask", "hotkey"]:
            assert phone.post("/api/screen/" + path, json={}).status_code == 403
        token = app.state.engine.controller_token
        phone.headers.update({"Authorization": f"Bearer {token}"})
        assert phone.get("/api/screen/monitors").json()["monitors"] == [MONITOR]
        body = request(backend="api").model_dump()
        assert phone.post("/api/screen/preview", json=body).json()["image"] == IMAGE
        assert not app.state.engine.items
        assert phone.post("/api/screen/ask", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
        with phone.websocket_connect("ws://192.168.1.10/ws") as ws:
            ws.send_json({"token": token}); snapshot(ws)
            assert phone.post("/api/screen/ask", json=body).status_code == 200
            for _ in range(100):
                event = ws.receive_json()
                assert "data:image" not in json.dumps(event)
                if event["type"] == "item" and event["item"]["status"] == "done":
                    assert event["item"]["answer"] == "B，手机答案"
                    assert event["item"]["source"] == "screen"
                    break
            else:
                raise AssertionError("No screen answer")
        phone.portal.call(app.state.engine.rotate_controller)
        assert phone.post("/api/screen/ask", json=body).status_code == 403


def test_screen_invalid_region_and_hotkey_errors(store, monkeypatch):
    with local_client(create_app(store)) as client:
        body = request().model_dump()
        body["region"] = {"x": .9, "y": 0, "width": .8, "height": 1}
        assert client.post("/api/screen/ask", json=body).status_code == 422
        assert client.post("/api/screen/hotkey", json={"enabled": True}).status_code == 400
        def fail(_):
            raise ScreenError("快捷键已被占用")
        monkeypatch.setattr(FakeHotkey, "start", fail)
        monkeypatch.setattr("backend.engine.ScreenHotkey", FakeHotkey)
        response = client.post("/api/screen/hotkey", json={"enabled": True, "request": request().model_dump()})
        assert response.status_code == 400 and "占用" in response.text
        assert not client.get("/api/bootstrap").json()["snapshot"]["state"]["screen_hotkey"]
