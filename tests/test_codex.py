import asyncio
from pathlib import Path
import sys

import httpx
import pytest

from backend.codex_backend import CodexBackend, CodexError
from backend.config import Settings
from backend.engine import Engine
from backend.providers import Providers


def fake_backend():
    fixture = Path(__file__).with_name("fake_codex_server.py")
    return CodexBackend(command_factory=lambda _: [sys.executable, "-u", "-X", "utf8", str(fixture)])


async def answer(backend, text):
    return "".join([part async for part in backend.answer(text, Settings(answer_backend="codex"))])


@pytest.mark.asyncio
async def test_model_catalog_paginates_filters_and_keeps_model_ids_without_starting_turn():
    backend = fake_backend()
    try:
        models = await backend.list_models(Settings(answer_backend="codex"))
        assert models == [{"id": "test-gpt-a", "name": "Test GPT A", "is_default": True},
                          {"id": "test-gpt-b", "name": "Test GPT B", "is_default": False}]
        assert backend.thread_id is None and backend.context["turns"] == 0
        await answer(backend, "REMEMBER:keep-interview")
        thread_id = backend.thread_id
        assert await backend.list_models(Settings(answer_backend="codex")) == models
        assert backend.thread_id == thread_id and backend.context["turns"] == 1
        assert "keep-interview" in await answer(backend, "RECALL")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_model_catalog_login_failure_and_bad_pagination_are_actionable(monkeypatch):
    from unittest.mock import AsyncMock
    backend = CodexBackend()
    monkeypatch.setattr(backend, "probe", AsyncMock(return_value={"logged_in": False}))
    rpc = AsyncMock()
    monkeypatch.setattr(backend, "rpc", rpc)
    with pytest.raises(CodexError, match="登录"):
        await backend.list_models(Settings())
    rpc.assert_not_called()
    backend.probe.return_value = {"logged_in": True}
    rpc.return_value = {"data": [], "nextCursor": "repeated"}
    with pytest.raises(CodexError, match="分页"):
        await backend.list_models(Settings())
    assert rpc.await_count == 2
    rpc.return_value = {"bad": "private-diagnostic"}
    with pytest.raises(CodexError, match="格式不兼容") as error:
        await backend.list_models(Settings())
    assert "private-diagnostic" not in str(error.value)


@pytest.mark.asyncio
async def test_codex_persistent_context_streams_without_duplicate_final_text():
    backend = fake_backend()
    try:
        assert (await backend.probe(Settings(answer_backend="codex")))["logged_in"]
        assert await answer(backend, "REMEMBER:unique-code-72") == "测试回答"
        original = backend.thread_id
        assert await answer(backend, "RECALL") == "unique-code-72"
        assert backend.thread_id == original
        assert backend.context["turns"] == 2
        assert backend.context["context_tokens"] == 102
        assert backend.context["context_window"] == 1000
        await backend.compact()
        assert backend.context["compactions"] == 1
        assert await answer(backend, "RECALL") == "unique-code-72"
        process = backend.process
        await backend.close()
        assert process.returncode is not None and backend.thread_id is None
        assert backend.context["turns"] == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_codex_cancellation_interrupts_actual_turn_and_allows_followup():
    backend = fake_backend()
    received = asyncio.Event()
    async def slow():
        async for text in backend.answer("WAIT", Settings(answer_backend="codex")):
            received.set()
    try:
        task = asyncio.create_task(slow())
        await asyncio.wait_for(received.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not backend.context["lost"]
        assert await answer(backend, "下一题") == "测试回答"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_codex_crash_requires_explicit_reset_instead_of_silent_context_loss():
    backend = fake_backend()
    try:
        await answer(backend, "REMEMBER:keep-me")
        with pytest.raises(CodexError, match="中断"):
            await answer(backend, "CRASH")
        assert backend.context["lost"]
        with pytest.raises(CodexError, match="新建上下文"):
            await answer(backend, "下一题")
        await backend.close()
        assert await answer(backend, "全新问题") == "测试回答"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_codex_non_text_activity_is_rejected():
    backend = fake_backend()
    try:
        with pytest.raises(CodexError, match="非文字任务"):
            await answer(backend, "TOOL")
        assert await answer(backend, "正常问题") == "测试回答"
    finally:
        await backend.close()


def test_codex_does_not_require_llm_key_but_audio_still_requires_asr_key():
    settings = Settings(answer_backend="codex")
    settings.check_ready()
    with pytest.raises(ValueError, match="语音识别"):
        settings.check_ready(audio=True)


@pytest.mark.asyncio
async def test_fast_tier_and_prewarm_do_not_generate_or_lose_thread():
    async with httpx.AsyncClient() as client:
        providers = Providers(client)
        providers.codex = fake_backend()
        settings = Settings(answer_backend="codex", codex_fast=True, codex_model="gpt-5.6-luna")
        try:
            await providers.prepare_answer(settings)
            original = providers.codex.thread_id
            assert original and providers.codex.context["turns"] == 0
            assert "".join([part async for part in providers.answer("问题", settings, [])]) == "测试回答"
            assert providers.codex.thread_id == original
            with pytest.raises(CodexError, match="新建上下文"):
                await providers.codex.ensure_thread(settings.model_copy(update={"codex_fast": False}))
        finally:
            await providers.close()


@pytest.mark.asyncio
async def test_engine_clear_resets_codex_and_changing_identity_starts_new_context():
    async with httpx.AsyncClient() as client:
        providers = Providers(client)
        providers.codex = fake_backend()
        engine = Engine(Settings(answer_backend="codex"), providers)
        try:
            await engine.ask("第一题")
            await engine.answer_task
            assert engine.state["context"]["active"]
            assert engine.items[-1]["answer"] == "测试回答"
            await engine.compact_context()
            await engine.compact_task
            assert engine.state["context"]["compactions"] == 1
            await engine.apply_settings(Settings(answer_backend="codex", role="新岗位"))
            assert not engine.items and providers.codex.thread_id is None
            assert not engine.state["context"]["active"]
        finally:
            await engine.close()
