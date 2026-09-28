"""Isolated UI fixture: synthetic microphone, ASR and model; no real capture or API calls."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading

import uvicorn
import backend.app as app_module
import backend.diagnostics as diagnostics
import backend.practice as practice_module
from backend.audio import AudioSegment
from backend.config import ConfigStore, Settings


class FixtureProvider:
    async def transcribe(self, wav, settings):
        await asyncio.sleep(.2)
        return wav.decode()

    async def answer(self, question, settings, history):
        yield "这是 AI 参考回答，不代表你实际说过的话。"

    async def review(self, question, answer, settings):
        for part in [f"1. 已经说清楚的点\n你提到：“{answer}”。\n\n", "2. 遗漏与表达问题\n还需要说明验证结果。\n\n",
                     "3. 改进示例\n说明如何复现、验证修复，并补充真实结果。\n\n", "4. 下一轮追问\n如何设计回归测试？\n如果无法复现，下一步怎么排查？\n\n（模拟复盘，用于界面验证。）"]:
            yield part
            await asyncio.sleep(.2)


class FixtureMicrophone:
    def __init__(self, on_segment, on_level, on_error):
        self.on_segment, self.on_level = on_segment, on_level
        self.timer = None
        self.stopped = False

    def start(self, *args):
        def emit():
            if not self.stopped:
                self.on_level(.04)
                self.on_segment(AudioSegment("我负责接口测试，先复现问题。".encode(), False))
        self.timer = threading.Timer(.4, emit)
        self.timer.start()
        return "合成测试麦克风"

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.timer:
            self.timer.cancel()
        self.on_segment(AudioSegment("然后编写回归用例。".encode(), True))


def fixture_app():
    root = Path(tempfile.mkdtemp(prefix="interview-practice-preview-"))
    store = ConfigStore(root / "settings.json")
    store.save(Settings(llm_model="fixture", llm_api_key="fixture", asr_api_key="fixture"))
    device = dict(id=1, name="合成测试麦克风", default=True, channels=1, rate=16000)
    practice_module.MicrophoneCapture = FixtureMicrophone
    app_module.list_microphones = diagnostics.list_microphones = lambda: [device]
    app_module.list_devices = diagnostics.list_devices = lambda: [dict(device, name="合成耳机")]
    app = app_module.create_app(store)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            engine = app.state.engine
            engine.controller_token = "practice-preview-controller-token"
            engine.providers = FixtureProvider()
            engine.items.append(dict(id="practice-q1", question="请介绍你在项目中如何定位并解决问题。", answer="这是 AI 参考稿，仅用于对照。",
                                     demo=False, source="manual", status="done", first_token_ms=None,
                                     at=datetime.now(timezone.utc).isoformat()))
            yield
    app.router.lifespan_context = lifespan
    return app


if __name__ == "__main__":
    uvicorn.run(fixture_app(), host="127.0.0.1", port=8766, access_log=False)
