import pytest
from fastapi.testclient import TestClient

import backend.diagnostics as diagnostics
from backend.app import create_app
from backend.config import ConfigStore, Settings


@pytest.mark.asyncio
async def test_diagnostics_reports_configuration_without_leaking_keys_or_claiming_phone_access(monkeypatch):
    monkeypatch.setattr(diagnostics, "list_devices", lambda: [dict(name="耳机", default=True)])
    monkeypatch.setattr(diagnostics, "list_microphones", lambda: [dict(name="麦克风", default=True)])
    settings = Settings(llm_model="model", llm_api_key="private-llm", asr_api_key="private-asr", background="private-resume")
    result = await diagnostics.diagnose(settings, ["192.168.1.2"], 8765, 0)
    assert "private-" not in str(result)
    by_name = {check["name"]:check for check in result["checks"]}
    assert by_name["控制台连接"]["status"] == "warning"
    assert "不能证明手机可达" in by_name["控制台连接"]["detail"]
    assert "尚未验证" in by_name["回答与复盘模型"]["detail"]
    assert by_name["电脑麦克风"]["status"] == "ok"


@pytest.mark.asyncio
async def test_diagnostics_degrades_when_devices_or_configuration_missing(monkeypatch):
    def broken():
        raise RuntimeError("private-driver-details")
    monkeypatch.setattr(diagnostics, "list_devices", broken)
    monkeypatch.setattr(diagnostics, "list_microphones", lambda: [])
    result = await diagnostics.diagnose(Settings(), [], 8765, 0)
    assert not result["ready"]
    assert "private-driver" not in str(result)
    assert sum(check["status"] == "warning" for check in result["checks"]) == 6


def test_diagnostics_requires_control_and_can_be_used_from_pairing_page(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "list_devices", lambda: [])
    monkeypatch.setattr(diagnostics, "list_microphones", lambda: [])
    app = create_app(ConfigStore(tmp_path / "settings.json"))
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)) as client:
        assert client.get("/api/diagnostics").status_code == 200
        assert client.get("/api/diagnostics", headers={"Origin":"https://evil.example"}).status_code == 403
    with TestClient(app, base_url="http://192.168.1.2", client=("192.168.1.5", 5000)) as client:
        assert client.get("/api/diagnostics").status_code == 403
        headers = {"Authorization":"Bearer " + app.state.engine.controller_token}
        assert client.get("/api/diagnostics", headers=headers).status_code == 200
