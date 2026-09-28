import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.practice as practice_module
import backend.providers as provider_module
from backend.app import create_app
from backend.audio import AudioSegment
from backend.config import ConfigStore, Settings
from backend.engine import Engine
from backend.providers import ProviderError, Providers


def configured():
    return Settings(llm_model="test", llm_api_key="private-llm", asr_api_key="private-asr", role="工程师", background="做过项目甲")


def add_question(engine, ident="q1"):
    item = dict(id=ident, question="请介绍项目", answer="AI编造的参考稿绝不能当成用户回答", status="done", source="manual",
                demo=False, first_token_ms=None, at=datetime.now(timezone.utc).isoformat())
    engine.items.append(item)
    return item


class FakeProvider:
    async def transcribe(self, wav, settings):
        await asyncio.sleep(.01)
        return wav.decode()

    async def review(self, question, answer, settings):
        yield "依据：“" + answer + "”。"
        await asyncio.sleep(.01)
        yield "建议补充职责；追问：如何验证效果？"


@pytest.fixture
def capture(monkeypatch):
    captures = []
    class FakeCapture:
        def __init__(self, on_segment, on_level, on_error):
            self.on_segment, self.on_level, self.on_error = on_segment, on_level, on_error
            self.stopped = False
            self.final = None
            captures.append(self)
        def start(self, device_id, *args):
            return "测试麦克风"
        def stop(self):
            self.stopped = True
            if self.final:
                self.on_segment(AudioSegment(self.final, True))
    monkeypatch.setattr(practice_module, "MicrophoneCapture", FakeCapture)
    return captures


async def settled(engine):
    async with asyncio.timeout(2):
        while engine.practice.busy():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_question_binding_short_speech_final_flush_and_review(capture):
    engine = Engine(configured(), FakeProvider())
    item = add_question(engine)
    assert not engine.state["practice"]["recording"] and not capture
    await engine.practice.start(item["id"])
    capture[0].on_segment(AudioSegment("我".encode(), False))
    later = add_question(engine, "q2")
    capture[0].final = "负责测试".encode()
    await engine.practice.finish_recording()
    await settled(engine)
    assert item["candidate_answer"] == "我 负责测试"
    assert "candidate_answer" not in later
    assert not engine.transcripts and capture[0].stopped
    await engine.practice.review(item["id"], item["candidate_answer"])
    await settled(engine)
    assert item["review"]["status"] == "done"
    assert "AI编造" not in item["review"]["text"]
    assert engine.state["context"]["turns"] == 0
    await engine.close()


@pytest.mark.asyncio
async def test_stop_discards_late_asr_and_keeps_previous_text(capture):
    began = asyncio.Event()
    class Delayed(FakeProvider):
        async def transcribe(self, wav, settings):
            began.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                return "迟到结果"
    engine = Engine(configured(), Delayed())
    item = add_question(engine)
    engine.practice.save(item["id"], "已保存")
    await engine.practice.start(item["id"])
    capture[0].on_segment(AudioSegment(b"audio", True))
    await began.wait()
    await engine.stop()
    capture[0].on_segment(AudioSegment(b"later", True))
    await asyncio.sleep(.02)
    assert item["candidate_answer"] == "已保存"
    assert item["candidate_status"] == "cancelled" and capture[0].stopped
    assert not engine.practice.busy()


@pytest.mark.asyncio
async def test_asr_failure_during_finish_stops_without_false_success(capture):
    class Broken(FakeProvider):
        async def transcribe(self, *args):
            await asyncio.sleep(.01)
            raise ProviderError("识别额度不足")
    engine = Engine(configured(), Broken())
    item = add_question(engine)
    await engine.practice.start(item["id"])
    capture[0].final = b"audio"
    await engine.practice.finish_recording()
    await settled(engine)
    assert item["candidate_status"] == "error" and "额度不足" in item["candidate_error"]
    assert engine.state["practice"]["notice"] == "识别额度不足"


@pytest.mark.asyncio
async def test_empty_speech_manual_correction_and_stale_review(capture):
    engine = Engine(configured(), FakeProvider())
    item = add_question(engine)
    await engine.practice.start(item["id"])
    await engine.practice.finish_recording()
    await settled(engine)
    with pytest.raises(ValueError, match="实际说过"):
        await engine.practice.review(item["id"], " ")
    await engine.practice.review(item["id"], "原始回答")
    await settled(engine)
    old_review = item["review"]
    engine.practice.save(item["id"], "校正回答")
    assert item["review"] is None and old_review["answer"] == "原始回答"


@pytest.mark.asyncio
async def test_practice_question_needs_no_model_and_keeps_its_own_answer():
    engine = Engine(Settings(), None)
    item = engine.practice.add_question("  请介绍你的项目  ")
    assert item["status"] == "ready" and item["question"] == "请介绍你的项目"
    engine.practice.save(item["id"], "这是我的经历")
    assert item["candidate_answer"] == "这是我的经历" and item["answer"] == ""
    assert engine.answer_task is None
    with pytest.raises(ValueError):
        engine.practice.add_question(" ")
    await engine.close()


@pytest.mark.asyncio
async def test_start_failure_does_not_erase_previous_review(capture, monkeypatch):
    engine = Engine(configured(), FakeProvider())
    item = add_question(engine)
    await engine.practice.review(item["id"], "原来的回答")
    await settled(engine)
    previous = item["review"]
    def fail(*args):
        raise RuntimeError("麦克风没有权限")
    monkeypatch.setattr(practice_module.MicrophoneCapture, "start", fail)
    with pytest.raises(RuntimeError, match="权限"):
        await engine.practice.start(item["id"])
    assert not engine.practice.busy() and capture[-1].stopped
    assert item["review"] is previous and item["candidate_answer"] == "原来的回答"


@pytest.mark.asyncio
async def test_text_limit_preserves_prior_transcript_and_stops(capture):
    engine = Engine(configured(), FakeProvider())
    item = add_question(engine)
    engine.practice.save(item["id"], "字" * 11999)
    await engine.practice.start(item["id"])
    capture[0].on_segment(AudioSegment("新增内容".encode(), True))
    await settled(engine)
    assert len(item["candidate_answer"]) == 11999 and item["candidate_status"] == "error"
    assert "12000" in item["candidate_error"] and capture[0].stopped


@pytest.mark.asyncio
async def test_busy_guards_and_clear_cancel_review(capture):
    engine = Engine(configured(), FakeProvider())
    item = add_question(engine)
    await engine.practice.start(item["id"])
    with pytest.raises(ValueError):
        engine.practice.save(item["id"], "覆盖")
    with pytest.raises(ValueError):
        await engine.practice.start(item["id"])
    await engine.stop()
    await engine.practice.review(item["id"], "实际回答")
    await asyncio.sleep(0)
    await engine.clear()
    assert not engine.items and not engine.practice.busy()
    assert item["review"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_microphone_revoke_invalidates_callbacks(capture):
    engine = Engine(configured(), FakeProvider())
    item = add_question(engine)
    await engine.practice.start(item["id"])
    engine.rotate_controller()
    capture[0].on_segment(AudioSegment("撤销后内容".encode(), True))
    await settled(engine)
    assert item["candidate_answer"] == "" and capture[0].stopped


@pytest.mark.asyncio
async def test_recording_limit_is_graceful_finish(capture, monkeypatch):
    engine = Engine(configured(), FakeProvider())
    item = add_question(engine)
    original_sleep = asyncio.sleep
    async def short_limit(seconds):
        await original_sleep(.02 if seconds == 300 else seconds)
    monkeypatch.setattr(practice_module.asyncio, "sleep", short_limit)
    await engine.practice.start(item["id"])
    capture[0].final = "限时前的最后一句".encode()
    await settled(engine)
    assert capture[0].stopped and item["candidate_answer"] == "限时前的最后一句"


@pytest.mark.asyncio
async def test_review_wire_only_contains_actual_answer_and_resume():
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"复盘"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Providers(client)
        assert "".join([part async for part in provider.review("问题", "实际回答", configured())]) == "复盘"
        payload = requests[0]
        assert len(payload["messages"]) == 2
        data = json.loads(payload["messages"][1]["content"])
        assert data == {"问题":"问题", "实际回答":"实际回答", "目标岗位":"工程师", "简历":"做过项目甲"}
        assert "不能评价语速" in payload["messages"][0]["content"]
        assert "private-" not in json.dumps(payload)
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["codex", "claude"])
async def test_review_uses_isolated_cli_and_always_closes(monkeypatch, backend_name):
    instances = []
    class Backend:
        def __init__(self, **kwargs):
            self.instructions = kwargs.get("instructions")
            self.closed = False
            instances.append(self)
        async def answer(self, prompt, *args):
            assert json.loads(prompt)["实际回答"] == "实际回答"
            yield "片段"
            await asyncio.sleep(100)
        async def close(self):
            self.closed = True
    monkeypatch.setattr(provider_module, "CodexBackend" if backend_name == "codex" else "ClaudeBackend", Backend)
    async with httpx.AsyncClient() as client:
        provider = Providers(client)
        existing = list(instances)
        stream = provider.review("问题", "实际回答", configured().model_copy(update={"answer_backend":backend_name}))
        assert await anext(stream) == "片段"
        await stream.aclose()
        assert instances[-1].closed and instances[-1].instructions
        assert all(not backend.closed for backend in existing)
        await provider.close()


def test_authenticated_practice_routes_validation_and_revocation(tmp_path):
    store = ConfigStore(tmp_path / "settings.json"); store.save(configured())
    app = create_app(store)
    with TestClient(app, base_url="http://192.168.1.10", client=("192.168.1.20", 5000)) as client:
        engine = app.state.engine
        item = add_question(engine)
        body = {"item_id":item["id"], "answer":"我的回答"}
        for path in ("practice/start", "practice/finish", "practice/answer", "practice/review"):
            assert client.post("/api/" + path, json=body).status_code == 403
        assert client.get("/api/microphones").status_code == 403
        headers = {"Authorization":"Bearer " + engine.controller_token}
        assert client.post("/api/practice/question", json={"question":"练习下一题"}, headers=headers).status_code == 200
        assert client.post("/api/practice/answer", json=body, headers=headers).status_code == 200
        assert client.post("/api/practice/answer", json={**body,"answer":"x"*12001}, headers=headers).status_code == 422
        assert client.post("/api/practice/answer", json={**body,"item_id":"missing"}, headers=headers).status_code == 400
        assert client.post("/api/practice/answer", json=body, headers={**headers,"Origin":"https://evil.example"}).status_code == 403
        engine.controller_token = "changed"
        assert client.post("/api/practice/answer", json=body, headers=headers).status_code == 403
