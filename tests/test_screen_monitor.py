import asyncio
import base64
import io
import json
import threading

from fastapi.testclient import TestClient
import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError
import pytest

from backend.app import create_app
from backend.config import Settings
from backend.engine import Engine
from backend.providers import ProviderError
from backend.screen import ScreenChangeDetector, ScreenError, ScreenMonitorOptions
from tests.test_app import snapshot, store
from tests.test_screen import FakeHotkey, SlowProviders, request


def options(**kwargs):
    # Shorten only internal test scheduling; HTTP configuration still enforces >= 1 second.
    return ScreenMonitorOptions(request=request(), **kwargs).model_copy(update={"interval_seconds": .01})


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


class ScreenSamples:
    def __init__(self, color=255):
        self.color = color
        self.calls = 0
    def __call__(self, request):
        self.calls += 1
        image = Image.new("RGB", (200, 100), (self.color,) * 3)
        return image, ScreenChangeDetector.signature(image)


class Answers:
    def __init__(self):
        self.colors = []
        self.hold = asyncio.Event()
        self.hold.set()
        self.fail = False
    async def answer_screen(self, image, request, settings):
        decoded = Image.open(io.BytesIO(base64.b64decode(image.split(",", 1)[1])))
        self.colors.append(decoded.getpixel((0, 0))[0])
        yield "答案"
        await self.hold.wait()
        if self.fail:
            raise ProviderError("模拟额度不足")
        yield "完成"


def test_detector_stabilizes_ignores_small_noise_and_remembers_recent_frames():
    detector = ScreenChangeDetector()
    white = np.full((100, 200), 255, dtype=np.int16)
    black = np.zeros_like(white)
    tiny = white.copy(); tiny[0, 0] = 0
    assert not detector.observe(white)
    assert detector.observe(tiny)
    detector.mark_submitted(tiny)
    assert not detector.observe(white)
    assert not detector.observe(black)
    assert detector.observe(black)
    detector.mark_submitted(black)
    assert not detector.observe(white)
    assert not detector.observe(white)  # Back to an already submitted question.
    assert not detector.observe(np.zeros((50, 100), dtype=np.int16))
    assert detector.observe(np.zeros((50, 100), dtype=np.int16))


def test_detector_waits_out_transitions_and_sensitivity_changes_small_edit_handling():
    white = np.full((100, 200), 255, dtype=np.int16)
    edit = white.copy(); edit[10:12, 10:12] = 0
    assert not ScreenChangeDetector("normal").changed(white, edit)
    assert ScreenChangeDetector("high").changed(white, edit)
    detector = ScreenChangeDetector()
    for shade in [255, 0, 255, 0]:
        assert not detector.observe(np.full_like(white, shade))
    assert detector.observe(np.zeros_like(white))
    for shade in range(20):
        detector.mark_submitted(np.full_like(white, shade))
    assert len(detector.submitted) == 12


def test_standard_sensitivity_detects_numeric_question_change_in_full_screen():
    def question(text):
        image = Image.new("RGB", (1440, 900), "white")
        ImageDraw.Draw(image).text((80, 200), text, font=ImageFont.load_default(size=42), fill="black")
        return ScreenChangeDetector.signature(image)
    detector = ScreenChangeDetector()
    first, second = question("17 + 25 = ?"), question("18 + 24 = ?")
    assert detector.changed(first, second)


@pytest.mark.parametrize("interval", [0, -.1, .5, 16, float("nan")])
def test_monitor_interval_is_bounded(interval):
    with pytest.raises(ValidationError):
        ScreenMonitorOptions(request=request(), interval_seconds=interval)


@pytest.mark.asyncio
async def test_monitor_answers_initial_screen_only_once_then_answers_stable_change(monkeypatch):
    sample, provider = ScreenSamples(), Answers()
    monkeypatch.setattr("backend.engine.sample_screen", sample)
    engine = Engine(Settings(), provider)
    try:
        await engine.set_screen_monitor(options())
        await until(lambda: len(engine.items) == 1 and engine.items[-1]["status"] == "done")
        await until(lambda: sample.calls >= 6)
        assert len(provider.colors) == 1
        sample.color = 0
        await until(lambda: len(provider.colors) == 2)
        assert provider.colors == [255, 0]
        assert engine.state["screen_monitor_count"] == 2
        assert "data:image" not in json.dumps(engine.snapshot())
        sample.color = 255
        call_count = sample.calls
        await until(lambda: sample.calls >= call_count + 4)
        assert len(provider.colors) == 2
    finally:
        await engine.close()
    stopped = sample.calls
    await asyncio.sleep(.025)
    assert sample.calls == stopped and not engine.state["screen_monitoring"]


@pytest.mark.asyncio
async def test_busy_answer_keeps_only_latest_screen_without_cancelling_it(monkeypatch):
    sample, provider = ScreenSamples(), Answers()
    provider.hold.clear()
    monkeypatch.setattr("backend.engine.sample_screen", sample)
    engine = Engine(Settings(), provider)
    try:
        await engine.set_screen_monitor(options())
        await until(lambda: len(provider.colors) == 1)
        sample.color = 0
        call_count = sample.calls
        await until(lambda: sample.calls >= call_count + 3)
        assert provider.colors == [255] and engine.items[0]["status"] == "generating"
        sample.color = 100
        call_count = sample.calls
        await until(lambda: sample.calls >= call_count + 3)
        provider.hold.set()
        await until(lambda: len(provider.colors) == 2)
        assert provider.colors == [255, 100]  # Do not queue the obsolete black screen.
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_stop_cancels_auto_answer_and_clear_removes_it(monkeypatch):
    sample, provider = ScreenSamples(), SlowProviders()
    monkeypatch.setattr("backend.engine.sample_screen", sample)
    engine = Engine(Settings(), provider)
    await engine.set_screen_monitor(options())
    await asyncio.wait_for(provider.started.wait(), 3)
    await engine.set_screen_monitor(None)
    assert provider.cancelled
    assert engine.items[-1]["status"] == "cancelled"
    assert engine.items[-1]["answer"] == "部分答案"
    assert not engine.state["screen_monitoring"]
    await engine.clear()
    assert not engine.items


@pytest.mark.asyncio
async def test_stopping_during_slow_capture_discards_late_result(monkeypatch):
    began, release = threading.Event(), threading.Event()
    sample, provider = ScreenSamples(), Answers()
    def delayed(body):
        began.set()
        assert release.wait(3)
        return sample(body)
    monkeypatch.setattr("backend.engine.sample_screen", delayed)
    engine = Engine(Settings(), provider)
    try:
        await engine.set_screen_monitor(options())
        assert await asyncio.to_thread(began.wait, 2)
        await asyncio.wait_for(engine.stop(), .5)
    finally:
        release.set()
        await engine.close()
    await asyncio.sleep(.03)
    assert not engine.items and not provider.colors


@pytest.mark.asyncio
async def test_capture_and_provider_errors_stop_instead_of_repeating_requests(monkeypatch):
    def fail(body):
        raise ScreenError("显示器已断开")
    monkeypatch.setattr("backend.engine.sample_screen", fail)
    provider = Answers()
    engine = Engine(Settings(), provider)
    await engine.set_screen_monitor(options())
    await until(lambda: not engine.state["screen_monitoring"])
    assert "显示器已断开" in engine.state["screen_notice"] and not provider.colors
    monkeypatch.setattr("backend.engine.sample_screen", ScreenSamples())
    provider.fail = True
    await engine.set_screen_monitor(options())
    await until(lambda: not engine.state["screen_monitoring"])
    assert len(provider.colors) == 1 and engine.items[-1]["status"] == "error"
    assert "解题失败" in engine.state["screen_notice"]
    await engine.close()


@pytest.mark.asyncio
async def test_monitor_mutual_exclusion_settings_and_revocation(monkeypatch):
    sample, provider = ScreenSamples(), SlowProviders()
    monkeypatch.setattr("backend.engine.sample_screen", sample)
    monkeypatch.setattr("backend.engine.ScreenHotkey", FakeHotkey)
    engine = Engine(Settings(), provider)
    await engine.set_screen_hotkey(request())
    listener = engine.screen_hotkey
    await engine.set_screen_monitor(options())
    assert listener.stopped and not engine.state["screen_hotkey"]
    for operation in [engine.start(None), engine.ask_screen(request()), engine.set_screen_hotkey(request()),
                      engine.compact_context(), engine.ask("手动问题")]:
        with pytest.raises(ValueError, match="监测"):
            await operation
    await asyncio.wait_for(provider.started.wait(), 3)
    engine.rotate_controller()
    await until(lambda: provider.cancelled)
    assert not engine.state["screen_monitoring"] and engine.items[-1]["status"] == "cancelled"
    await engine.set_screen_monitor(options())
    await engine.apply_settings(Settings())
    assert not engine.state["screen_monitoring"] and engine.screen_monitor_task is None
    await engine.close()


def test_monitor_api_auth_validation_settings_guard_and_stream(store, monkeypatch):
    monkeypatch.setattr("backend.engine.sample_screen", ScreenSamples())
    store.save(Settings(llm_model="vision", llm_api_key="private"))
    def handler(req):
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"自动答案"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    app = create_app(store, httpx.MockTransport(handler))
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as phone:
        assert phone.post("/api/screen/monitor", json={"enabled": False}).status_code == 403
        token = app.state.engine.controller_token
        phone.headers.update({"Authorization": f"Bearer {token}"})
        assert phone.post("/api/screen/monitor", json={"enabled": True}).status_code == 400
        config = ScreenMonitorOptions(request=request(backend="api"), interval_seconds=1).model_dump()
        invalid = {**config, "interval_seconds": .1}
        assert phone.post("/api/screen/monitor", json={"enabled": True, "options": invalid}).status_code == 422
        assert phone.post("/api/screen/monitor", json={"enabled": True, "options": config}, headers={"Origin": "https://evil.example"}).status_code == 403
        with phone.websocket_connect("ws://192.168.1.10/ws") as ws:
            ws.send_json({"token": token}); snapshot(ws)
            assert phone.post("/api/screen/monitor", json={"enabled": True, "options": config}).json()["enabled"]
            assert phone.post("/api/settings", json=Settings().model_dump()).status_code == 409
            for _ in range(100):
                event = ws.receive_json()
                assert "data:image" not in json.dumps(event)
                if event["type"] == "item" and event["item"]["status"] == "done":
                    assert event["item"]["answer"] == "自动答案"
                    break
            else:
                raise AssertionError("No automatic answer")
            assert not phone.post("/api/screen/monitor", json={"enabled": False}).json()["enabled"]
            assert not app.state.engine.state["screen_monitoring"]
        phone.portal.call(app.state.engine.rotate_controller)
        assert phone.post("/api/screen/monitor", json={"enabled": True, "options": config}).status_code == 403
