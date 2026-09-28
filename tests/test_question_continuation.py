import asyncio
import json
import time

import httpx
import numpy as np
import pytest

from backend.audio import AudioSegment, Segmenter
from backend.config import Settings
from backend.engine import Engine
from backend.providers import Providers
from tests.test_codex import fake_backend


class AnswerRecorder:
    def __init__(self):
        self.questions = []
        self.histories = []

    async def answer(self, question, settings, history):
        self.questions.append(question)
        self.histories.append(history)
        yield "合并后的直接回答"


def make_engine():
    provider = AnswerRecorder()
    return Engine(Settings(llm_model="test", llm_api_key="fake", question_merge_seconds=2), provider), provider


async def finish_auto(engine):
    await asyncio.wait_for(engine.auto_task, 2)
    if engine.answer_task:
        await asyncio.wait_for(engine.answer_task, 2)


@pytest.mark.asyncio
async def test_two_fragments_wait_for_audio_and_merge_using_capture_time():
    engine, provider = make_engine()
    now = time.monotonic()
    try:
        # Recognition arrives much later than capture. The speaker has already resumed.
        engine.voice_active = True
        await engine.collect_audio_question("安防摄像头的光学设计。", AudioSegment(b"", True, now - 20, now - 18), 0)
        await asyncio.sleep(0.06)
        assert not provider.questions
        original_id = engine.items[0]["id"]
        await engine.collect_audio_question("光学系统需要怎么设计？", AudioSegment(b"", True, now - 17, now - 15), 0)
        engine.voice_active = False
        await finish_auto(engine)
        assert provider.questions == ["安防摄像头的光学设计。 光学系统需要怎么设计？"]
        assert len(engine.items) == 1 and engine.items[0]["id"] == original_id
        assert engine.items[0]["fragments"] == 2 and engine.items[0]["status"] == "done"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_late_asr_continuation_rewrites_same_card_and_cancels_old_stream():
    engine, provider = make_engine()
    started = asyncio.Event()

    async def slow(question, settings, history):
        provider.questions.append(question)
        yield "旧回答" if len(provider.questions) == 1 else "新回答"
        started.set()
        if len(provider.questions) == 1:
            await asyncio.sleep(100)

    provider.answer = slow
    now = time.monotonic()
    try:
        await engine.collect_audio_question("安防摄像头。", AudioSegment(b"", True, now - 20, now - 18), 0)
        await asyncio.wait_for(started.wait(), 2)
        original_task = engine.answer_task
        original_id = engine.items[0]["id"]
        await engine.collect_audio_question("需要怎么设计？", AudioSegment(b"", True, now - 17, now - 15), 0)
        await finish_auto(engine)
        assert original_task.cancelled()
        assert len(engine.items) == 1 and engine.items[0]["id"] == original_id
        assert engine.items[0]["answer"] == "新回答"
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("gap,text", [(5, "光学系统需要怎么设计？"), (1, "下一个问题，介绍你的华为项目。")])
async def test_separate_questions_keep_prior_unanswered_text_in_history(gap, text):
    engine, provider = make_engine()
    now = time.monotonic()
    try:
        engine.voice_active = True
        await engine.collect_audio_question("安防摄像头。", AudioSegment(b"", True, now - 30, now - 28), 0)
        await engine.collect_audio_question(text, AudioSegment(b"", True, now - 28 + gap, now - 20), 0)
        engine.voice_active = False
        await finish_auto(engine)
        assert len(engine.items) == 2
        assert engine.items[0]["status"] == "cancelled"
        assert provider.histories[-1][0]["question"] == "安防摄像头。"
        assert provider.questions == [text]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_stop_and_manual_question_cancel_pending_auto_answer():
    engine, provider = make_engine()
    now = time.monotonic()
    try:
        engine.voice_active = True
        await engine.collect_audio_question("安防摄像头。", AudioSegment(b"", True, now - 4, now), 0)
        await engine.stop()
        assert engine.pending_audio is None and engine.auto_task is None
        assert not provider.questions and engine.items[0]["status"] == "cancelled"
        await engine.collect_audio_question("新的前半句。", AudioSegment(b"", True, now, now + 1), engine.epoch)
        await engine.ask("手动指定新问题")
        await engine.answer_task
        assert engine.pending_audio is None and engine.last_audio is None
        assert provider.questions == ["手动指定新问题"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_pending_asr_blocks_early_answer_even_after_quiet_window():
    engine, provider = make_engine()
    now = time.monotonic()
    try:
        engine.state["transcribing"] = True
        await engine.collect_audio_question("安防摄像头。", AudioSegment(b"", True, now - 6, now - 4), 0)
        await asyncio.sleep(0.06)
        assert not provider.questions
        engine.state["transcribing"] = False
        engine.audio_queue.put_nowait(AudioSegment(b"next", True))
        await asyncio.sleep(0.06)
        assert not provider.questions
        engine.audio_queue.get_nowait()
        await finish_auto(engine)
        assert provider.questions == ["安防摄像头。"]
    finally:
        await engine.close()


def test_audio_timestamps_measure_pause_before_asr():
    detector = Segmenter(1000, silence=0.5)
    start = detector.clock
    outputs = []
    for signal, blocks in [(8000, 10), (0, 20), (8000, 10), (0, 10)]:
        for _ in range(blocks):
            _, segment = detector.feed(np.full(50, signal, dtype=np.int16))
            if segment:
                outputs.append(segment)
    assert len(outputs) == 2
    assert outputs[0].started_at == pytest.approx(start)
    assert outputs[1].started_at - outputs[0].ended_at == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_cancelled_topic_reaches_both_models_without_using_old_answer():
    settings = Settings(llm_model="test", llm_api_key="fake")
    history = [{"question": "安防摄像头的光学设计。", "status": "cancelled", "answer": "不应沿用的草稿"}]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"回答"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = Providers(client)
        providers.codex = fake_backend()
        rpc = providers.codex.rpc
        turns = []

        async def record(method, params, **kwargs):
            if method == "turn/start":
                turns.append(params)
            return await rpc(method, params, **kwargs)

        providers.codex.rpc = record
        try:
            _ = [part async for part in providers.answer("光学系统需要怎么设计？", settings, history)]
            prompt = requests[0]["messages"][-1]["content"]
            assert "安防摄像头" in prompt and "光学系统需要怎么设计" in prompt
            assert "不应沿用的草稿" not in json.dumps(requests, ensure_ascii=False)
            _ = [part async for part in providers.answer("光学系统需要怎么设计？", settings.model_copy(update={"answer_backend": "codex"}), history)]
            assert prompt in turns[0]["input"][0]["text"]
        finally:
            await providers.close()
