import json

import httpx
import pytest

from backend.config import Settings
from backend.providers import Providers
from tests.test_codex import fake_backend


@pytest.mark.asyncio
async def test_resume_reaches_system_context_for_both_answer_backends():
    # Synthetic biography only; the user's resume is never stored in test sources.
    biography = "候选人：测试同学。参与蓝桥光学项目，工作距离提升4倍。"
    settings = Settings(llm_model="test", llm_api_key="test-key", background=biography)
    api_requests = []

    def handler(request):
        api_requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"我参与了蓝桥项目。"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Providers(client)
        provider.codex = fake_backend()
        rpc_requests = []
        original_rpc = provider.codex.rpc

        async def record_rpc(method, params, **kwargs):
            rpc_requests.append((method, params))
            return await original_rpc(method, params, **kwargs)

        provider.codex.rpc = record_rpc
        try:
            _ = [part async for part in provider.answer("介绍项目", settings, [])]
            system_message = api_requests[0]["messages"][0]
            assert system_message["role"] == "system" and biography in system_message["content"]
            _ = [part async for part in provider.answer("介绍项目", settings.model_copy(update={"answer_backend": "codex"}), [])]
            thread_params = next(params for method, params in rpc_requests if method == "thread/start")
            assert thread_params["baseInstructions"] == system_message["content"]
            turn_params = next(params for method, params in rpc_requests if method == "turn/start")
            assert biography not in turn_params["input"][0]["text"]
        finally:
            await provider.close()
