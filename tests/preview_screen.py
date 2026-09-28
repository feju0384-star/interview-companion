"""Local UI fixture with a synthetic screen; no desktop capture or model calls.

Run: python -m tests.preview_screen, then open http://127.0.0.1:8766/phone#screen-preview-controller-token
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw, ImageFont
import uvicorn

import backend.app as app_module
import backend.engine as engine_module
from backend.config import ConfigStore
from backend.screen import ScreenChangeDetector, encode_image


class FixtureAnswers:
    async def answer_screen(self, image, request, settings):
        for part in ["答案：B（42）。\n\n", "解析：题目中两个数相加得到 42。（本机模拟回答，用于检查自动监测与手机同步。）"]:
            yield part
            await asyncio.sleep(.25)


class FixtureHotkey:
    def __init__(self, callback):
        pass
    def start(self):
        pass
    def stop(self):
        pass


def synthetic_question(number=1):
    image = Image.new("RGB", (1440, 900), "#f6f5f0")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 42)
    draw.text((80, 80), f"Practice question {number:02}", font=font, fill="#203d35")
    draw.text((80, 200), "17 + 25 = ?" if number % 2 else "18 + 24 = ?", font=font, fill="#203d35")
    draw.text((80, 320), "A. 32       B. 42       C. 52       D. 62", font=font, fill="#203d35")
    return image


if __name__ == "__main__":
    monitor = {"id": "fixture-monitor", "name": "test", "bounds": [0, 0, 1440, 900], "width": 1440, "height": 900, "primary": True}
    fixture = {"number": 1, "image": synthetic_question()}
    app_module.list_monitors = lambda: [monitor]
    app_module.list_devices = lambda: []
    app_module.capture_screen = engine_module.capture_screen = lambda request, **kwargs: {**encode_image(fixture["image"]), "monitor": monitor}
    engine_module.sample_screen = lambda request: (fixture["image"].copy(), ScreenChangeDetector.signature(fixture["image"]))
    engine_module.ScreenHotkey = FixtureHotkey
    with tempfile.TemporaryDirectory() as folder:
        app = app_module.create_app(ConfigStore(Path(folder) / "settings.json"))
        original = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with original(app):
                app.state.engine.controller_token = "screen-preview-controller-token"
                await app.state.engine.providers.close()
                app.state.engine.providers = FixtureAnswers()
                yield

        app.router.lifespan_context = lifespan

        @app.post("/test/next-screen")
        async def next_screen():
            fixture["number"] += 1
            fixture["image"] = synthetic_question(fixture["number"])
            return {"question": fixture["number"]}

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8766, access_log=False, log_level="warning"))
        app.state.shutdown_hook = lambda: setattr(server, "should_exit", True)
        server.run()
