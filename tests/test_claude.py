import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.claude_backend import ClaudeBackend, ClaudeError, local_configuration
from backend.config import ConfigStore, Settings


@pytest.fixture(autouse=True)
def isolated_cli_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token-not-real")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    return tmp_path


def fake_backend():
    return ClaudeBackend(command_factory=lambda _: [sys.executable, str(Path(__file__).with_name("fake_claude_cli.py"))])


async def answer(backend, question, history=None):
    return "".join([part async for part in backend.answer(question, Settings(answer_backend="claude", background="测试履历"), history or [])])


@pytest.mark.asyncio
async def test_stream_deduplicates_final_messages_and_deletes_prompt(isolated_cli_settings):
    backend = fake_backend()
    assert await answer(backend, "问题") == "测试回答"
    assert backend.process is None
    assert not list((isolated_cli_settings / "InterviewCompanion" / "claude-workspace").glob("answer-*"))


@pytest.mark.asyncio
async def test_stdin_system_resume_context_and_shell_characters():
    history = [
        {"question": "过期问题", "answer": "旧回答", "status": "done"},
        {"question": "有效问题", "answer": "有效回答", "status": "done"},
        {"question": "保留前半句", "answer": "取消的回答", "status": "cancelled"},
        {"question": "演示问题", "answer": "演示回答", "status": "done", "demo": True},
        {"question": "未回答", "answer": "", "status": "generating"},
    ]
    result = json.loads(await answer(fake_backend(), 'ECHO_INPUT 中文 & $(echo bad) "quoted"', history))
    assert "测试履历" in result["system"]
    assert "有效回答" in result["prompt"] and "保留前半句" in result["prompt"]
    assert '$(echo bad) "quoted"' in result["prompt"]
    for excluded in ("旧回答", "取消的回答", "演示回答", "演示问题"):
        assert excluded not in result["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question,expected", [("ERROR", "服务错误"), ("TOOL", "工具"), ("MALFORMED", "格式"), ("TRUNCATE", "提前中断")])
async def test_failures_are_explicit_and_do_not_leak_provider_output(question, expected):
    backend = fake_backend()
    with pytest.raises(ClaudeError, match=expected) as error:
        await answer(backend, question)
    assert "secret" not in str(error.value)
    assert backend.process is None


@pytest.mark.asyncio
async def test_cancel_terminates_process_and_next_question_works():
    backend = fake_backend()
    started = asyncio.Event()
    async def slow():
        async for _ in backend.answer("WAIT", Settings(answer_backend="claude"), []):
            started.set()
    task = asyncio.create_task(slow())
    await asyncio.wait_for(started.wait(), 10)
    process = backend.process
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    assert await answer(backend, "后续问题") == "测试回答"


def test_deepseek_config_is_process_local_and_probe_contains_no_secrets(isolated_cli_settings, monkeypatch):
    path = isolated_cli_settings / "settings.json"
    raw = json.dumps({"model": "stale-model", "env": {"ANTHROPIC_MODEL": "configured-model",
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com", "ANTHROPIC_AUTH_TOKEN": "private-token",
        "UNRELATED_COMMAND": "do-not-inherit"}})
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    env, info = local_configuration(Settings())
    assert info["base_url"] == "https://api.deepseek.com/anthropic"
    assert info["model"] == "configured-model"
    assert "private-token" not in json.dumps(info)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "private-token"
    assert "HTTPS_PROXY" not in env and "UNRELATED_COMMAND" not in env
    assert path.read_text(encoding="utf-8") == raw
    env, info = local_configuration(Settings(claude_model="chosen-model", claude_direct=False))
    assert info["model"] == "chosen-model" and env["HTTPS_PROXY"].endswith(":9999")


def test_phone_claude_settings_authentication_and_streaming(tmp_path):
    store = ConfigStore(tmp_path / "app-settings.json")
    store.save(Settings(answer_backend="claude", claude_executable="C:/local-only.exe"))
    app = create_app(store)
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as phone:
        app.state.engine.providers.claude = fake_backend()
        assert phone.get("/api/claude/status").status_code == 403
        token = app.state.engine.controller_token
        phone.headers.update({"Authorization": f"Bearer {token}"})
        public = phone.get("/api/controller/bootstrap").json()["settings"]
        assert "claude_executable" not in public
        assert phone.post("/api/settings", json={"claude_executable": "C:/untrusted.exe"}).status_code == 403
        assert phone.post("/api/settings", json=public | {"llm_key_saved": False}).status_code == 422
        public.pop("llm_key_saved"); public.pop("asr_key_saved")
        assert phone.post("/api/settings", json=public).status_code == 200
        assert store.load().claude_executable == "C:/local-only.exe"
        assert phone.get("/api/claude/status").json()["configured"] is True
        with phone.websocket_connect("/ws") as ws:
            ws.send_json({"token": token})
            assert phone.post("/api/ask", json={"question": "模拟问题"}).status_code == 200
            for _ in range(50):
                event = ws.receive_json()
                if event["type"] == "item" and event["item"]["status"] == "done":
                    assert event["item"]["answer"] == "测试回答"
                    break
            else:
                raise AssertionError("Phone did not receive Claude answer")
        assert phone.post("/api/context/compact", json={}).status_code == 409
        assert phone.post("/api/clear", json={}).status_code == 200
        assert phone.get("/api/controller/bootstrap").json()["snapshot"]["items"] == []
