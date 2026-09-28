import asyncio
import io
import json
import wave

import httpx
import numpy as np
import pytest

from backend.audio import AudioSegment, Segmenter, to_wav
from backend.config import Settings
from backend.engine import Engine
from backend.providers import ProviderError, Providers


def configured():
    return Settings(llm_model="test-chat", llm_api_key="test-llm-secret", asr_api_key="test-asr-secret", question_merge_seconds=0)


def test_silence_never_transcribes():
    detector = Segmenter(1000)
    for _ in range(500):
        _, segment = detector.feed(np.zeros(50, dtype=np.int16))
        assert segment is None
    assert detector.pre_samples <= 300


def test_short_noise_is_rejected_and_real_turn_is_wav():
    detector = Segmenter(1000, silence=0.5)
    detector.feed(np.full(50, 8000, dtype=np.int16))
    results = [detector.feed(np.zeros(50, dtype=np.int16))[1] for _ in range(10)]
    assert not any(results)
    for _ in range(8):
        detector.feed(np.full(50, 8000, dtype=np.int16))
    results = [detector.feed(np.zeros(50, dtype=np.int16))[1] for _ in range(10)]
    segment = next(result for result in results if result)
    assert segment.final
    with wave.open(io.BytesIO(segment.wav)) as stream:
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getframerate() == 1000
        assert stream.getnframes() >= 900


def test_long_question_has_partial_then_silent_final_marker():
    detector = Segmenter(1000, silence=0.5, max_seconds=1)
    segments = []
    for _ in range(40):
        _, segment = detector.feed(np.full(50, 8000, dtype=np.int16))
        if segment:
            segments.append(segment)
    for _ in range(12):
        _, segment = detector.feed(np.zeros(50, dtype=np.int16))
        if segment:
            segments.append(segment)
    assert [segment.final for segment in segments] == [False, False, True]
    assert segments[-1].wav == b""


@pytest.mark.asyncio
async def test_minimax_multipart_and_business_errors():
    settings = configured().model_copy(update={"asr_provider": "minimax", "asr_base_url": "https://api.minimaxi.com/v1", "asr_model": "asr-1.0", "asr_language": "zh"})
    responses = [{"text": " 数值孔径 ", "duration": 4.0, "trace_id": "test"},
                 {"base_resp": {"status_code": 1004, "status_msg": "secret-upstream-data"}},
                 {"duration": 4.0}]
    def handler(request):
        assert request.url.path == "/v1/speech_to_text"
        assert request.headers["authorization"] == "Bearer test-asr-secret"
        assert request.headers["language"] == "zh"
        assert b'name="language"' not in request.content
        assert b'name="response_format"\r\n\r\njson' in request.content
        assert b'asr-1.0' in request.content and b'RIFF' in request.content
        return httpx.Response(200, json=responses.pop(0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Providers(client)
        wav = to_wav([np.zeros(100, dtype=np.int16)], 1000)
        assert await provider.transcribe(wav, settings) == "数值孔径"
        with pytest.raises(ProviderError, match="1004") as error:
            await provider.transcribe(wav, settings)
        assert "secret-upstream-data" not in str(error.value)
        with pytest.raises(ProviderError, match="text"):
            await provider.transcribe(wav, settings)


@pytest.mark.asyncio
async def test_provider_stream_and_asr_wire_contract():
    captured = []
    def handler(request):
        captured.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            assert request.headers["authorization"] == "Bearer test-asr-secret"
            assert b'name="model"' in request.content and b'RIFF' in request.content
            return httpx.Response(200, json={"text": "面试问题"})
        data = json.loads(request.content)
        assert data["stream"] is True
        assert data["messages"][-1]["content"] == "测试问题"
        assert "test-llm-secret" not in request.content.decode()
        events = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"reasoning_content": "不能出现在正文"}}]},
            {"choices": [{"delta": {"content": "这是"}}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]},
        ]
        text = ": keepalive\n\n" + "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Providers(client)
        assert await provider.transcribe(to_wav([np.zeros(100, dtype=np.int16)], 1000), configured()) == "面试问题"
        answer = "".join([chunk async for chunk in provider.answer("测试问题", configured(), [])])
        assert answer == "这是答案"
        assert len(captured) == 2


@pytest.mark.asyncio
async def test_provider_truncated_answer_is_error():
    body = 'data: {"choices":[{"delta":{"content":"部分回答"}}]}\n\n'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as client:
        chunks = []
        with pytest.raises(ProviderError, match="提前中断"):
            async for text in Providers(client).answer("测试", configured(), []):
                chunks.append(text)
        assert chunks == ["部分回答"]


@pytest.mark.asyncio
async def test_provider_error_does_not_leak_raw_response():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="test-asr-secret"))) as client:
        with pytest.raises(ProviderError) as error:
            await Providers(client).transcribe(b"audio", configured())
        assert "test-asr-secret" not in str(error.value)
        assert "401" in str(error.value)


@pytest.mark.asyncio
async def test_engine_merges_audio_parts_before_answering():
    class Fake:
        def __init__(self): self.asked = []
        async def transcribe(self, wav, settings): return wav.decode()
        async def answer(self, question, settings, history):
            self.asked.append(question)
            yield "完整答案"
    fake = Fake()
    engine = Engine(configured(), fake)
    engine.epoch = 1
    engine.asr_task = asyncio.create_task(engine.transcription_worker(1, configured()))
    engine.audio_queue.put_nowait(AudioSegment("介绍项目".encode(), False))
    await asyncio.sleep(0.01)
    assert not engine.items
    engine.audio_queue.put_nowait(AudioSegment("最难的地方".encode(), True))
    await asyncio.sleep(0.02)
    assert fake.asked == ["介绍项目 最难的地方"]
    assert engine.items[-1]["answer"] == "完整答案"
    await engine.stop()


@pytest.mark.asyncio
async def test_stop_cancels_pending_asr_and_never_creates_late_answer():
    started = asyncio.Event()
    class Fake:
        async def transcribe(self, wav, settings):
            started.set()
            await asyncio.sleep(100)
            return "不应出现"
    engine = Engine(configured(), Fake())
    engine.epoch = 1
    engine.asr_task = asyncio.create_task(engine.transcription_worker(1, configured()))
    engine.audio_queue.put_nowait(AudioSegment(b"audio", True))
    await started.wait()
    await engine.stop()
    assert not engine.items and not engine.state["listening"]
    assert engine.asr_task is None and engine.audio_queue.empty()


@pytest.mark.asyncio
async def test_new_question_cancels_old_answer_without_mixing_content():
    class Fake:
        async def answer(self, question, settings, history):
            yield question
            await asyncio.sleep(100)
    engine = Engine(configured(), Fake())
    await engine.ask("第一题")
    await asyncio.sleep(0)
    await engine.ask("第二题")
    await asyncio.sleep(0)
    assert engine.items[0]["status"] == "cancelled"
    assert engine.items[0]["answer"] == "第一题"
    assert engine.items[1]["answer"] == "第二题"
    await engine.stop()


def test_slow_phone_gets_snapshot_and_rotation_revokes_it():
    engine = Engine(Settings(), None)
    reader = engine.subscribe("controller")
    for index in range(100):
        engine.status(level=index / 100)
    events = []
    while not reader.empty(): events.append(reader.get_nowait())
    assert any(event["type"] == "snapshot" for event in events)
    old = engine.controller_token
    engine.rotate_controller()
    assert reader.get_nowait()["type"] == "revoked"
    assert old != engine.controller_token and engine.state["controller_count"] == 0
