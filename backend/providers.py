import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from .config import Settings
from .codex_backend import CodexBackend, CodexError
from .claude_backend import ClaudeBackend, ClaudeError
from .prompts import REVIEW_INSTRUCTIONS, SCREEN_INSTRUCTIONS, interview_instructions, question_with_context, screen_question


class ProviderError(RuntimeError):
    pass


def response_error(status: int, service: str) -> ProviderError:
    advice = {
        400: "模型名称或请求参数不被支持，请核对服务商文档",
        401: "API Key 无效或已过期",
        403: "API Key 无权使用该模型，或服务限制了访问地区",
        404: "找不到接口或模型，请检查 Base URL 和模型名称",
        413: "音频过大，请缩短音频分段",
        429: "额度不足或请求过于频繁，请检查账户余额与限流",
    }.get(status, "服务暂时不可用，请稍后重试")
    # Do not relay provider response bodies: they can contain credentials or audio text.
    return ProviderError(f"{service}返回 HTTP {status}：{advice}")


class Providers:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.codex = CodexBackend()
        self.screen_codex = CodexBackend(instructions=SCREEN_INSTRUCTIONS)
        self.claude = ClaudeBackend()

    def set_context_callback(self, callback):
        self.codex.on_context = callback

    async def reset_context(self):
        await self.codex.close()
        await self.screen_codex.close()
        await self.claude.close()

    async def compact_context(self):
        try:
            await self.codex.compact()
        except CodexError as exc:
            raise ProviderError(str(exc)) from exc

    async def close(self):
        await self.codex.close()
        await self.screen_codex.close()
        await self.claude.close()

    async def prepare_answer(self, settings: Settings):
        if settings.answer_backend == "claude":
            try:
                result = await self.claude.probe(settings)
                if not result["configured"]:
                    raise ProviderError("请先在电脑的 Claude Code 中配置 API Key 或认证令牌")
            except ClaudeError as exc:
                raise ProviderError(str(exc)) from exc
        if settings.answer_backend == "codex":
            try:
                result = await self.codex.probe(settings)
                if not result.get("logged_in"):
                    raise ProviderError("Codex 尚未登录，请在电脑完成登录后再开始监听")
                await self.codex.ensure_thread(settings)
            except CodexError as exc:
                raise ProviderError(str(exc)) from exc

    async def transcribe(self, wav: bytes, settings: Settings) -> str:
        fields = {"model": settings.asr_model}
        headers = {"Authorization": f"Bearer {settings.asr_api_key}"}
        minimax = settings.asr_provider == "minimax"
        if minimax:
            fields["response_format"] = "json"
        if settings.asr_language:
            if minimax:
                headers["language"] = settings.asr_language
            else:
                fields["language"] = settings.asr_language
        try:
            response = await self.client.post(
                settings.asr_base_url + ("/speech_to_text" if minimax else "/audio/transcriptions"),
                headers=headers,
                data=fields,
                files={"file": ("speech.wav", wav, "audio/wav")},
                timeout=httpx.Timeout(40, connect=10),
            )
            if response.is_error:
                raise response_error(response.status_code, "语音识别服务")
            data = response.json()
            if minimax and isinstance(data, dict):
                status = data.get("base_resp") or {}
                if isinstance(status, dict) and status.get("status_code") not in (None, 0):
                    code = status.get("status_code")
                    label = str(code) if isinstance(code, int) else "未知"
                    raise ProviderError(f"MiniMax 语音识别失败（错误码 {label}），请检查密钥、余额与模型权限")
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise ProviderError("语音识别响应缺少 text 字段，请确认使用兼容的转写接口")
            return data["text"].strip()[:12000]
        except httpx.TimeoutException as exc:
            raise ProviderError("语音识别超时，请检查网络或更换识别服务") from exc
        except httpx.RequestError as exc:
            raise ProviderError("无法连接语音识别服务，请检查网络和 Base URL") from exc
        except (ValueError, TypeError) as exc:
            raise ProviderError("语音识别服务返回了无法解析的数据") from exc

    async def answer(self, question: str, settings: Settings, history: list[dict]) -> AsyncIterator[str]:
        if settings.answer_backend == "claude":
            stream = self.claude.answer(question, settings, [item for item in history if item.get("source") != "screen"])
            try:
                async for text in stream:
                    yield text
            except ClaudeError as exc:
                raise ProviderError(str(exc)) from exc
            finally:
                await stream.aclose()
            return
        contextual_question = question_with_context(question, history)
        if settings.answer_backend == "codex":
            stream = self.codex.answer(contextual_question, settings)
            try:
                async for text in stream:
                    yield text
            except CodexError as exc:
                raise ProviderError(str(exc)) from exc
            finally:
                await stream.aclose()
            return
        system = interview_instructions(settings)
        messages = [{"role": "system", "content": system}]
        for item in history[-4:]:
            if item["status"] == "done" and not item.get("demo") and item.get("source") != "screen":
                messages.extend([
                    {"role": "user", "content": item["question"]},
                    {"role": "assistant", "content": item["answer"]},
                ])
        messages.append({"role": "user", "content": contextual_question})
        payload = {"model": settings.llm_model, "messages": messages, "stream": True, "max_tokens": settings.max_tokens}
        stream = self.stream_api(payload, settings)
        try:
            async for text in stream:
                yield text
        finally:
            await stream.aclose()

    async def review(self, question, actual_answer, settings):
        prompt = json.dumps({"问题": question, "实际回答": actual_answer,
                             "目标岗位": settings.role, "简历": settings.background}, ensure_ascii=False)
        backend = None
        if settings.answer_backend == "codex":
            backend = CodexBackend(instructions=REVIEW_INSTRUCTIONS)
            stream = backend.answer(prompt, settings)
        elif settings.answer_backend == "claude":
            backend = ClaudeBackend(instructions=REVIEW_INSTRUCTIONS)
            stream = backend.answer(prompt, settings, [])
        else:
            payload = {"model": settings.llm_model, "stream": True, "max_tokens": settings.max_tokens,
                       "messages": [{"role": "system", "content": REVIEW_INSTRUCTIONS},
                                    {"role": "user", "content": prompt}]}
            stream = self.stream_api(payload, settings)
        try:
            async for text in stream:
                yield text
        except (CodexError, ClaudeError) as exc:
            raise ProviderError(str(exc)) from exc
        finally:
            try:
                await stream.aclose()
            finally:
                if backend:
                    await backend.close()

    async def answer_screen(self, image_url, request, settings):
        question = screen_question(request.style, request.instruction)
        if request.backend == "codex":
            stream = self.screen_codex.answer(question, settings, image_url=image_url)
            try:
                async for text in stream:
                    yield text
            except CodexError as exc:
                raise ProviderError(str(exc)) from exc
            finally:
                # Every screenshot is independent; do not accumulate images or resume facts.
                try:
                    await stream.aclose()
                finally:
                    await self.screen_codex.close()
            return
        payload = {"model": settings.llm_model, "stream": True, "max_tokens": settings.max_tokens,
                   "messages": [{"role": "system", "content": SCREEN_INSTRUCTIONS},
                                {"role": "user", "content": [{"type": "text", "text": question},
                                 {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}}]}]}
        stream = self.stream_api(payload, settings, screen=True)
        try:
            async for text in stream:
                yield text
        finally:
            await stream.aclose()

    async def stream_api(self, payload, settings, *, screen=False):
        finished = False
        content_seen = False
        try:
            async with asyncio.timeout(100):
                async with self.client.stream(
                    "POST", settings.llm_base_url + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json=payload, timeout=httpx.Timeout(40, connect=10),
                ) as response:
                    if response.is_error:
                        if screen and response.status_code in (400, 413, 415, 422):
                            raise ProviderError("图片请求被模型拒绝，请确认兼容 API 的模型支持图片输入，或改用 Codex；图片过大时请缩小框选区域")
                        raise response_error(response.status_code, "回答模型")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            finished = True
                            break
                        if not raw:
                            continue
                        data = json.loads(raw)
                        if data.get("error"):
                            raise ProviderError("回答模型在生成过程中返回错误，请检查账户额度和模型设置")
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        content = choice.get("delta", {}).get("content")
                        if isinstance(content, str) and content:
                            content_seen = True
                            yield content
                        reason = choice.get("finish_reason")
                        if reason == "length":
                            raise ProviderError("回答达到输出上限，已保留现有内容；可在设置中提高输出上限")
                        if reason == "content_filter":
                            raise ProviderError("回答被模型服务的内容过滤器中止")
                        if reason == "stop":
                            finished = True
            if not content_seen:
                raise ProviderError("模型未返回正文，请使用支持文本输出和流式响应的对话模型")
            if not finished:
                raise ProviderError("模型连接提前中断，已保留收到的部分回答")
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderError("回答模型超时，已保留收到的内容；可改用更快的模型") from exc
        except httpx.RequestError as exc:
            raise ProviderError("回答模型连接中断，请检查网络") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("模型返回格式不兼容，需支持 Chat Completions 的 SSE 流式接口") from exc


async def demo_answer() -> AsyncIterator[str]:
    text = (
        "数值孔径反映光学系统接收光线的能力，通常写成NA = n sin θ，"
        "其中n是介质折射率，θ是最大接收光锥的半角。\n\n"
        "在相同条件下，数值孔径越大，系统通常能够收集更大角度范围的光。"
        "具体设计时，我还会结合像差、工作距离和后续光纤的接收能力综合考虑。\n\n"
        "（固定演示，用于检查手机同步，未调用大模型。）"
    )
    for index in range(0, len(text), 5):
        await asyncio.sleep(0.025)
        yield text[index:index + 5]
