"""One paid/authenticated Codex image request using a synthetic image, never the desktop."""
import asyncio

import httpx

from backend.config import ConfigStore
from backend.providers import Providers
from backend.screen import ScreenRequest, encode_image
from tests.preview_screen import synthetic_question


async def main():
    settings = ConfigStore().load()
    async with httpx.AsyncClient() as client:
        providers = Providers(client)
        try:
            request = ScreenRequest(monitor_id="synthetic-only", instruction="只输出图片中算式的结果，不加其他文字。")
            image = encode_image(synthetic_question())["image"]
            answer = "".join([part async for part in providers.answer_screen(image, request, settings)])
            print("Synthetic image result:", answer)
            assert "42" in answer, "The synthetic image answer should contain 42"
            assert providers.screen_codex.process is None
            print("PASS: synthetic image input, streamed answer, screen context cleanup")
        finally:
            await providers.close()


if __name__ == "__main__":
    asyncio.run(main())
