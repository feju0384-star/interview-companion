"""Explicit live check: upload a synthetic WAV and use the saved model configuration.

Run manually with --audio PATH. Uses ASR billing and Codex account quota.
Does not open a microphone, start loopback capture, or print credentials.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

import httpx

from backend.audio import AudioSegment
from backend.config import ConfigStore
from backend.engine import Engine
from backend.providers import Providers


async def main(path):
    settings = ConfigStore().load()
    settings.check_ready(audio=True)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        providers = Providers(client)
        engine = Engine(settings, providers)
        queue = engine.subscribe("controller")
        try:
            await providers.prepare_answer(settings)
            engine.epoch = 1
            engine.asr_task = asyncio.create_task(engine.transcription_worker(1, settings))
            began = time.monotonic()
            engine.audio_queue.put_nowait(AudioSegment(Path(path).read_bytes(), True))
            first = None
            async with asyncio.timeout(120):
                while True:
                    event = await queue.get()
                    if event["type"] == "status" and event["state"].get("error"):
                        raise RuntimeError(event["state"]["error"])
                    if event["type"] != "item":
                        continue
                    item = event["item"]
                    if item["answer"] and first is None:
                        first = round((time.monotonic() - began) * 1000)
                    if item["status"] in {"done", "error", "cancelled"}:
                        assert item["status"] == "done", item.get("error")
                        print(json.dumps({"asr_ms": engine.state["asr_ms"],
                                          "model_first_ms": item["first_token_ms"],
                                          "audio_ready_to_first_ms": first,
                                          "total_ms": round((time.monotonic() - began) * 1000),
                                          "model": providers.codex.context["model"],
                                          "synthetic_transcript": item["question"],
                                          "answer": item["answer"]}, ensure_ascii=True))
                        break
        finally:
            engine.unsubscribe(queue)
            await engine.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    asyncio.run(main(parser.parse_args().audio))
