"""Optional local hardware check: no upload, no recording file, no ASR request.

This checks device capture/lifecycle only. Ambient system audio can prevent a
silence boundary, so endpoint detection is tested with deterministic PCM in pytest.
"""
import json
import time
import winsound

import numpy as np

import backend.audio as audio_module
from backend.audio import Capture, to_wav


if __name__ == "__main__":
    levels, segments, errors = [], [], []
    metrics = {"frames": 0, "seconds": 0, "voiced": 0, "silent": 0, "duration": 0}
    original = audio_module.Segmenter
    class MeteredSegmenter(original):
        def feed(self, pcm, now=None):
            result = super().feed(pcm, now=now)
            metrics["frames"] += 1
            metrics["seconds"] += len(pcm) / self.rate
            metrics.update(voiced=self.voiced, silent=self.silent, duration=self.duration)
            return result
    audio_module.Segmenter = MeteredSegmenter
    capture = Capture(segments.append, levels.append, errors.append)
    try:
        device = capture.start(None, 0.001, 0.5)
        rate = 48000
        time.sleep(0.35)
        positions = np.arange(int(rate * 1.0)) / rate
        # Soft synthetic tone to verify the output loopback. No microphone access.
        pcm = (np.sin(2 * np.pi * 440 * positions) * 5000).astype(np.int16)
        winsound.PlaySound(to_wav([pcm], rate), winsound.SND_MEMORY)
        time.sleep(1.5)
    finally:
        capture.stop()
    print(json.dumps({"device": device, "level_callbacks": len(levels), "peak": max(levels, default=0),
                      "segments": len(segments), "errors": errors, "thread_stopped": not capture.thread.is_alive(),
                      "metrics": metrics, "last_levels": levels[-8:]}, ensure_ascii=False))
    if errors or not levels or max(levels) <= 0 or capture.thread.is_alive():
        raise SystemExit(1)
