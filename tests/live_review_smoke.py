"""One real review request using fictional text; never records audio or sends a real resume."""
import asyncio
import httpx
from backend.config import ConfigStore
from backend.providers import Providers


async def main():
    settings = ConfigStore().load().model_copy(update={"role":"软件测试工程师", "background":"虚构练习资料：参与过练习项目的接口测试。", "max_tokens":1400})
    settings.check_ready()
    async with httpx.AsyncClient() as client:
        providers = Providers(client)
        try:
            async with asyncio.timeout(150):
                result = "".join([chunk async for chunk in providers.review(
                    "请介绍你如何定位一个接口问题。",
                    "在练习项目里，我先复现接口错误，检查请求参数，再对照日志定位问题。修复后我补了回归用例。", settings)])
            assert len(result) > 80 and "追问" in result, "Review was incomplete"
            assert not providers.codex.thread_id, "Review must not create the interview's Codex context"
            print({"review_completed":True, "backend":settings.answer_backend, "characters":len(result), "fictional_input_only":True})
        finally:
            await providers.close()


if __name__ == "__main__":
    asyncio.run(main())
