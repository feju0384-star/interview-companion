"""Local-only UI fixture: no audio device, stored user configuration, or real models.

Run `python -m tests.preview_continuation`. POST /test/continue to merge a second
fragment while viewing the phone preview; POST /api/shutdown to stop the fixture.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import time

import uvicorn

from backend.app import create_app
from backend.audio import AudioSegment
from backend.config import ConfigStore, Settings


class FakeAnswers:
    async def answer(self, question, settings, history):
        yield "安防摄像头的光学系统设计，我会先明确监控距离、视场角和夜间成像要求，再确定焦距和光圈，评估像差、照度及红外焦点偏移。"


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(ConfigStore(Path(folder) / "settings.json"))
        original = app.router.lifespan_context
        began = time.monotonic()

        @asynccontextmanager
        async def lifespan(app):
            async with original(app):
                engine = app.state.engine
                await engine.providers.close()
                engine.providers = FakeAnswers()
                engine.settings = Settings(llm_model="UI fixture", llm_api_key="fake", question_merge_seconds=2)
                engine.voice_active = True
                await engine.collect_audio_question("安防摄像头的光学设计。", AudioSegment(b"", True, began - 20, began - 18), 0)
                yield

        app.router.lifespan_context = lifespan

        @app.post("/test/continue")
        async def continue_question():
            engine = app.state.engine
            async with engine.command_lock:
                await engine.collect_audio_question("光学系统需要怎么设计？", AudioSegment(b"", True, began - 17, began - 15), 0)
                engine.voice_active = False
            return {"ok": True}

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8766, access_log=False, log_level="warning"))
        app.state.shutdown_hook = lambda: setattr(server, "should_exit", True)
        server.run()
