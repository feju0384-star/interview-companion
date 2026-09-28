import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.config import ConfigStore, Settings


@pytest.fixture
def store(tmp_path):
    return ConfigStore(tmp_path / "settings.json")


def local_client(app):
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5555))


def snapshot(ws):
    for _ in range(100):
        event = ws.receive_json()
        if event["type"] == "snapshot": return event
    raise AssertionError("No snapshot received")


def test_remote_cannot_manage_or_read_settings(store):
    app = create_app(store)
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as client:
        assert client.get("/phone").status_code == 200
        for path in ["bootstrap", "controller/bootstrap", "devices", "qr", "codex/status", "codex/models"]:
            assert client.get("/api/" + path).status_code == 403
        for path in ["start", "stop", "clear", "ask", "demo", "settings", "shutdown", "pairing/rotate", "context/compact"]:
            assert client.post("/api/" + path, json={}).status_code == 403


def test_local_cross_origin_and_dns_rebinding_blocked(store):
    with local_client(create_app(store)) as client:
        assert client.post("/api/demo", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/bootstrap", headers={"Host": "evil.example"}).status_code == 403
        assert client.post("/api/ask", json={"question":" "}).status_code == 400
        assert client.get("/health").json()["app"] == "interview-companion"


def test_settings_preserve_keys_only_for_same_provider(store):
    saved = Settings(llm_api_key="private-llm", asr_api_key="private-asr", llm_model="test")
    store.save(saved)
    with local_client(create_app(store)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert "private-" not in json.dumps(bootstrap)
        body = saved.model_dump(); body["llm_api_key"] = ""; body["asr_api_key"] = ""
        assert client.post("/api/settings", json=body).status_code == 200
        assert store.load().llm_api_key == "private-llm"
        body["llm_base_url"] = "https://different.example/v1"
        assert client.post("/api/settings", json=body).status_code == 200
        assert store.load().llm_api_key == ""
        assert store.load().asr_api_key == "private-asr"


def test_missing_keys_cannot_record_but_demo_streams_to_phone(store):
    with local_client(create_app(store)) as client:
        assert client.post("/api/start", json={}).status_code == 400
        data = client.get("/api/bootstrap").json()
        token = data["pairing"]["local_url"].split("#")[1]
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"token": token})
            assert snapshot(ws)["items"] == []
            assert client.post("/api/demo", json={}).status_code == 200
            events = []
            for _ in range(200):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "item" and event["item"]["status"] == "done": break
            else: raise AssertionError("Demo did not finish")
            answer = events[-1]["item"]
            assert answer["demo"] and "固定演示" in answer["answer"]
            assert any(event["type"] == "item" and event["item"]["answer"] and event["item"]["status"] == "generating" for event in events)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"token":token})
            assert snapshot(ws)["items"][-1]["answer"] == answer["answer"]
            assert client.post("/api/pairing/rotate", json={}).status_code == 200
            while ws.receive_json()["type"] != "revoked": pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"token":token}); ws.receive_json()


def test_fake_provider_end_to_end_to_authenticated_phone(store):
    store.save(Settings(llm_api_key="test-key", llm_model="test-model"))
    def handler(request):
        body = 'data: {"choices":[{"delta":{"content":"真实链路模拟回答"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body)
    with local_client(create_app(store, httpx.MockTransport(handler))) as client:
        token = client.get("/api/bootstrap").json()["pairing"]["local_url"].split("#")[1]
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"token": token}); snapshot(ws)
            client.post("/api/ask", json={"question":"测试问题"})
            while True:
                event = ws.receive_json()
                if event["type"] == "item" and event["item"]["status"] == "done": break
            assert event["item"]["answer"] == "真实链路模拟回答"
            assert not event["item"]["demo"]
            client.post("/api/clear", json={})
            assert snapshot(ws)["items"] == []


def test_websocket_only_streams_events_and_unknown_token_is_rejected(store):
    with local_client(create_app(store)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"token": "bad"}); ws.receive_json()
        token = client.get("/api/bootstrap").json()["pairing"]["local_url"].split("#")[1]
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"token": token}); snapshot(ws)
                ws.send_text('{"command":"start"}')
                while True: ws.receive_json()


def test_bad_config_returns_validation_message_without_key(store):
    with local_client(create_app(store)) as client:
        body = Settings().model_dump()
        body["llm_api_key"] = "should-never-leak"
        body["llm_base_url"] = "http://unsafe.example/v1"
        response = client.post("/api/settings", json=body)
        assert response.status_code == 422
        assert "should-never-leak" not in response.text


def test_paired_phone_controls_capture_settings_answers_and_reset(store, monkeypatch):
    import backend.engine as engine_module

    class FakeCapture:
        stopped = False
        selected = None

        def __init__(self, *args):
            pass

        def start(self, device_id, *args):
            FakeCapture.selected = device_id
            return "测试电脑扬声器"

        def stop(self):
            FakeCapture.stopped = True

    monkeypatch.setattr(engine_module, "Capture", FakeCapture)
    monkeypatch.setattr("backend.app.list_devices", lambda: [{"id": 7, "name": "测试设备", "default": True}])
    saved = Settings(llm_api_key="private-answer", asr_api_key="private-asr", llm_model="test-model")
    store.save(saved)

    def handler(request):
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"手机发起的回答"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    app = create_app(store, httpx.MockTransport(handler))
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as phone:
        # Pairing is only issued by the local computer; the phone receives this capability via QR.
        token = app.state.engine.controller_token
        phone.headers.update({"Authorization": f"Bearer {token}", "Origin": "http://192.168.1.10"})
        info = phone.get("/api/controller/bootstrap").json()
        assert "private-" not in json.dumps(info)
        assert "token" not in info and "codex_executable" not in info["settings"]
        assert phone.get("/api/devices").json()["devices"][0]["id"] == 7
        body = {key: value for key, value in info["settings"].items() if not key.endswith("_key_saved")}
        body["silence_seconds"] = 1.3
        body["audio_gain"] = 4
        assert phone.post("/api/settings", json=body).status_code == 200
        assert store.load().asr_api_key == "private-asr"
        assert store.load().silence_seconds == 1.3
        assert store.load().audio_gain == 4
        with phone.websocket_connect("ws://192.168.1.10/ws") as ws:
            ws.send_json({"token": token})
            assert snapshot(ws)["state"]["controller_count"] == 1
            assert phone.post("/api/start", json={"device_id": 7}).status_code == 200
            assert app.state.engine.state["listening"] and FakeCapture.selected == 7
            # Resolve the device by name if indices moved after the list was shown.
            assert phone.post("/api/source", json={"device_id": 99, "device_name": "测试设备"}).status_code == 200
            assert FakeCapture.selected == 7 and app.state.engine.state["listening"]
            assert phone.post("/api/source", json={"device_id": 99, "device_name": "已拔出的设备"}).status_code == 400
            assert phone.post("/api/settings", json=body).status_code == 409
            assert phone.post("/api/stop", json={}).status_code == 200
            assert FakeCapture.stopped and not app.state.engine.state["listening"]
            assert phone.post("/api/ask", json={"question": "手机测试问题"}).status_code == 200
            for _ in range(100):
                event = ws.receive_json()
                if event["type"] == "item" and event["item"]["status"] == "done":
                    assert event["item"]["answer"] == "手机发起的回答"
                    break
            else:
                raise AssertionError("Phone answer did not complete")
        with phone.websocket_connect("ws://192.168.1.10/ws") as ws:
            ws.send_json({"token": token})
            assert snapshot(ws)["items"][-1]["answer"] == "手机发起的回答"
            assert phone.post("/api/clear", json={}).status_code == 200
            assert snapshot(ws)["items"] == []


def test_phone_token_scope_cross_origin_executable_and_revocation(store):
    app = create_app(store)
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as phone:
        token = app.state.engine.controller_token
        phone.headers.update({"Authorization": f"Bearer {token}"})
        assert phone.get("/api/controller/bootstrap").status_code == 200
        assert phone.get("/api/controller/bootstrap", headers={"Authorization": f"Bearer {app.state.engine.admin_token}"}).status_code == 403
        assert phone.get("/api/controller/bootstrap?token=" + token, headers={"Authorization": ""}).status_code == 403
        for path in ["bootstrap", "qr"]:
            assert phone.get("/api/" + path).status_code == 403
        for path in ["shutdown", "pairing/rotate"]:
            assert phone.post("/api/" + path, json={}).status_code == 403
        assert phone.post("/api/stop", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert phone.post("/api/stop", json={}, headers={"Origin": "https://192.168.1.10"}).status_code == 403
        assert phone.get("/api/controller/bootstrap", headers={"Host": "evil.example"}).status_code == 403
        assert phone.post("/api/settings", json={"codex_executable": "C:/untrusted.exe"}).status_code == 403
        with phone.websocket_connect("ws://192.168.1.10/ws") as ws:
            ws.send_json({"token": token}); snapshot(ws)
            phone.portal.call(app.state.engine.rotate_controller)
            while ws.receive_json()["type"] != "revoked":
                pass
        assert phone.get("/api/controller/bootstrap").status_code == 403
        assert phone.post("/api/stop", json={}).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with phone.websocket_connect("ws://192.168.1.10/ws") as ws:
                ws.send_json({"token": token}); ws.receive_json()


def test_phone_can_compact_codex_context(store):
    from tests.test_codex import fake_backend
    store.save(Settings(answer_backend="codex"))
    app = create_app(store)
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as phone:
        app.state.engine.providers.codex = fake_backend()
        app.state.engine.providers.set_context_callback(app.state.engine.context_changed)
        token = app.state.engine.controller_token
        phone.headers.update({"Authorization": f"Bearer {token}"})
        assert phone.get("/api/codex/status").json()["logged_in"]
        with phone.websocket_connect("ws://192.168.1.10/ws") as ws:
            ws.send_json({"token": token}); snapshot(ws)
            assert phone.post("/api/ask", json={"question": "测试上下文"}).status_code == 200
            for _ in range(100):
                event = ws.receive_json()
                if event["type"] == "item" and event["item"]["status"] == "done":
                    break
            assert phone.post("/api/context/compact", json={}).status_code == 200
            for _ in range(100):
                event = ws.receive_json()
                if event.get("state", {}).get("context", {}).get("compactions") == 1:
                    break
            else:
                raise AssertionError("Phone compaction did not complete")
            assert phone.post("/api/clear", json={}).status_code == 200
            assert snapshot(ws)["state"]["context"]["active"] is False


def test_paired_phone_lists_models_without_touching_interview_and_saves_choice(store, monkeypatch):
    from tests.test_codex import fake_backend
    probes = []
    def discovery_backend():
        backend = fake_backend()
        probes.append(backend)
        return backend
    monkeypatch.setattr("backend.app.CodexBackend", discovery_backend)
    store.save(Settings(answer_backend="codex", codex_model="old-model", asr_api_key="private-asr"))
    app = create_app(store)
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as phone:
        engine = app.state.engine
        engine.items.append({"id": "kept", "status": "done", "question": "原问题", "answer": "原回答"})
        token = engine.controller_token
        phone.headers.update({"Authorization": f"Bearer {token}"})
        response = phone.get("/api/codex/models")
        assert response.status_code == 200
        assert response.json()["models"][1]["id"] == "test-gpt-b"
        assert probes[0].process is None and probes[0].thread_id is None
        assert engine.items[0]["id"] == "kept" and engine.controller_token == token
        assert engine.settings.codex_model == "old-model"
        payload = {k:v for k,v in engine.settings.public().items() if not k.endswith("_key_saved")}
        payload["codex_model"] = "test-gpt-b"
        assert phone.post("/api/settings", json=payload).status_code == 200
        assert store.load().codex_model == "test-gpt-b"
        assert store.load().asr_api_key == "private-asr"
        assert not engine.items


def test_model_discovery_failure_closes_its_process_without_leaking_diagnostics(store, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.codex_backend import CodexBackend
    backend = CodexBackend()
    monkeypatch.setattr(backend, "list_models", AsyncMock(side_effect=OSError("private-diagnostic")))
    close = AsyncMock()
    monkeypatch.setattr(backend, "close", close)
    monkeypatch.setattr("backend.app.CodexBackend", lambda: backend)
    with local_client(create_app(store)) as client:
        response = client.get("/api/codex/models")
        assert response.status_code == 400 and "刷新" in response.json()["detail"]
        assert "private-diagnostic" not in response.text
        close.assert_awaited_once()
