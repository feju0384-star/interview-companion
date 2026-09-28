"""Optional real model test, uses the signed-in Codex account and its quota."""
import asyncio
import json
import time

from backend.codex_backend import CodexBackend
from backend.config import Settings


async def main():
    backend = CodexBackend()
    settings = Settings(answer_backend="codex", codex_timeout_seconds=120)
    try:
        async def ask(question):
            began = time.monotonic()
            text, chunks, first = "", 0, None
            async for part in backend.answer(question, settings):
                first = first or time.monotonic()
                text += part
                chunks += 1
            print(json.dumps({"answer":text, "chunks":chunks, "first_ms":round((first-began)*1000),
                              "context":backend.context}, ensure_ascii=False), flush=True)
            return text
        await ask("这是连通测试。请记住本次虚构项目的代号是蓝桥-7319，只回复已记住。")
        session = backend.thread_id
        answer = await ask("刚才虚构项目的代号是什么？只回复代号。")
        assert "7319" in answer and backend.thread_id == session
        await backend.compact()
        answer = await ask("压缩上下文后，之前的虚构项目代号是什么？只回复代号。")
        assert "7319" in answer and backend.thread_id == session
        assert backend.context["compactions"] >= 1
        await backend.close()
        assert backend.thread_id is None and not backend.context["active"]
        print("Codex session, streaming, compaction, reset: OK", flush=True)
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
