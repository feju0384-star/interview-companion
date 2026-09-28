import io
import json
import queue
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np


@dataclass
class AudioSegment:
    wav: bytes
    final: bool
    started_at: float | None = None
    ended_at: float | None = None


def rms(pcm: np.ndarray) -> float:
    return float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768) ** 2))) if pcm.size else 0.0


def mix_to_mono(raw: bytes, channels: int) -> np.ndarray:
    """Keep silent surround channels and phase cancellation from swallowing speech."""
    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
    if not samples.size:
        return np.zeros(0, dtype=np.int16)
    if channels == 1:
        return samples[:, 0].copy()
    values = samples.astype(np.float32)
    powers = np.mean(values ** 2, axis=0)
    strongest = int(np.argmax(powers))
    if powers[strongest] == 0:
        return samples[:, 0].copy()
    # Exclude empty/near-empty channels (over 20 dB below the strongest).
    mono = values[:, powers >= powers[strongest] * 0.01].mean(axis=1)
    # Opposite-phase channels can cancel even when both are individually audible.
    if np.mean(mono ** 2) < powers[strongest] * 0.25:
        mono = values[:, strongest]
    return np.rint(mono).astype(np.int16)


def amplify(pcm: np.ndarray, gain: float) -> np.ndarray:
    """Apply bounded input gain before detection, with peak headroom and no int16 wrap."""
    if not pcm.size or gain == 1:
        return pcm
    values = pcm.astype(np.float32)
    peak = float(np.max(np.abs(values)))
    if peak == 0:
        return pcm
    safe_gain = min(gain, max(1.0, 0.95 * 32767 / peak))
    return np.clip(np.rint(values * safe_gain), -32768, 32767).astype(np.int16)


def to_wav(frames: list[np.ndarray], rate: int, *, normalize: bool = False) -> bytes:
    pcm = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
    if normalize:
        # Normalize only accepted speech, never the input to silence detection.
        # A fixed gain per segment preserves pauses; the 8x cap avoids runaway noise.
        energy = rms(pcm)
        if energy > 0:
            pcm = amplify(pcm, min(8.0, max(1.0, 0.08 / energy)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm.astype("<i2").tobytes())
    return buffer.getvalue()


class Segmenter:
    """Energy endpoint detector. Long turns are transcribed in parts, answered only on silence."""
    def __init__(self, rate: int, threshold=0.008, silence=1.0, max_seconds=12.0):
        self.rate, self.threshold, self.silence, self.max_seconds = rate, threshold, silence, max_seconds
        self.pre = deque()
        self.pre_samples = 0
        self.frames = []
        self.active = False
        self.continued = False
        self.silent = self.voiced = self.duration = 0.0
        self.clock = time.monotonic()
        self.started_at = self.last_voice_at = None

    def feed(self, pcm: np.ndarray, now: float | None = None) -> tuple[float, AudioSegment | None]:
        seconds = len(pcm) / self.rate
        self.clock = self.clock + seconds if now is None else now
        energy = rms(pcm)
        loud = energy >= self.threshold
        if not self.active:
            if not loud:
                self.pre.append(pcm)
                self.pre_samples += len(pcm)
                while self.pre and self.pre_samples > self.rate * 0.3:
                    self.pre_samples -= len(self.pre.popleft())
                return energy, None
            self.active = True
            self.started_at = self.clock - seconds
            self.frames = list(self.pre)
            self.duration = self.pre_samples / self.rate
            self.pre.clear()
            self.pre_samples = 0
        self.frames.append(pcm)
        self.duration += seconds
        self.silent = 0 if loud else self.silent + seconds
        if loud:
            self.voiced += seconds
            self.last_voice_at = self.clock
        if self.silent + 1e-6 >= self.silence:
            segment = None
            if self.voiced >= 0.25 or self.continued:
                segment = AudioSegment(to_wav(self.frames, self.rate, normalize=True) if self.voiced >= 0.12 else b"", True, self.started_at, self.last_voice_at)
            self.frames = []
            self.active = self.continued = False
            self.silent = self.voiced = self.duration = 0.0
            return energy, segment
        if self.duration >= self.max_seconds:
            segment = AudioSegment(to_wav(self.frames, self.rate, normalize=True), False, self.started_at, self.last_voice_at)
            self.frames = []
            self.continued = True
            self.voiced = self.duration = 0.0
            return energy, segment
        return energy, None

    def finish(self) -> AudioSegment | None:
        """Flush an accepted unfinished turn when switching output devices."""
        if self.voiced >= 0.25 or self.continued:
            return AudioSegment(to_wav(self.frames, self.rate, normalize=True) if self.voiced >= 0.12 else b"",
                                True, self.started_at, self.last_voice_at)
        return None


def audio_library():
    if sys.platform != "win32":
        raise RuntimeError("当前系统声音采集仅支持 Windows；仍可使用手动输入和演示")
    try:
        import pyaudiowpatch
        return pyaudiowpatch
    except ImportError as exc:
        raise RuntimeError("未安装声音采集组件，请重新运行安装启动脚本") from exc


def _scan_devices(microphones=False) -> list[dict]:
    audio = audio_library()
    with audio.PyAudio() as manager:
        try:
            default = (manager.get_default_input_device_info() if microphones else manager.get_default_wasapi_loopback())["index"]
        except (OSError, LookupError):
            default = -1
        sources = (manager.get_device_info_by_index(i) for i in range(manager.get_device_count())) if microphones else manager.get_loopback_device_info_generator()
        return [{"id": int(device["index"]), "name": device["name"], "default": device["index"] == default,
                 "channels": int(device["maxInputChannels"]), "rate": int(device["defaultSampleRate"])}
                for device in sources if not microphones or (device["maxInputChannels"] > 0 and not device.get("isLoopbackDevice"))]


_devices_lock = threading.Lock()
_devices_cache: tuple[float, list[dict]] = (0.0, [])


def list_devices() -> list[dict]:
    # PortAudio caches devices/defaults process-wide while any stream is open.
    # A short, hidden probe gets a fresh list without interrupting that stream.
    global _devices_cache
    with _devices_lock:
        checked_at, devices = _devices_cache
        if time.monotonic() - checked_at >= 1.5:
            try:
                result = subprocess.run([sys.executable, "-m", "backend.audio"],
                                        cwd=Path(__file__).resolve().parent.parent,
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=6,
                                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                                        check=True)
                devices = json.loads(result.stdout)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise RuntimeError("无法刷新声音设备，请检查耳机连接后重试") from exc
            _devices_cache = (time.monotonic(), devices)
        return [dict(device) for device in devices]


def list_microphones() -> list[dict]:
    try:
        result = subprocess.run([sys.executable, "-m", "backend.audio", "--microphones"],
                                cwd=Path(__file__).resolve().parent.parent,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=6,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError("无法列出电脑麦克风，请检查设备连接和 Windows 麦克风权限") from exc


class Capture:
    microphone = False

    def __init__(self, on_segment: Callable, on_level: Callable, on_error: Callable, on_activity: Callable | None = None):
        self.on_segment, self.on_level, self.on_error = on_segment, on_level, on_error
        self.on_activity = on_activity or (lambda active, last_voice_at: None)
        self.thread = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.failure = None
        self.device_name = ""
        self.device_info = {}

    def start(self, device_id: int | None, threshold: float, silence: float, chunk_seconds: float = 4.0, gain: float = 1.0):
        self.thread = threading.Thread(target=self._run, args=(device_id, threshold, silence, chunk_seconds, gain), daemon=True)
        self.thread.start()
        if not self.ready.wait(8):
            self.stop_event.set()
            raise RuntimeError("音频设备启动超时，请检查输出设备是否可用")
        if self.failure:
            raise RuntimeError(self.failure)
        return self.device_name

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)
            if self.thread.is_alive():
                raise RuntimeError("音频设备未能及时释放，请关闭程序后重新启动")

    def _run(self, device_id, threshold, silence, chunk_seconds, gain):
        try:
            audio = audio_library()
            with audio.PyAudio() as manager:
                device = (manager.get_default_input_device_info() if self.microphone else manager.get_default_wasapi_loopback()) if device_id is None else manager.get_device_info_by_index(device_id)
                if self.microphone and (device.get("isLoopbackDevice") or device.get("maxInputChannels", 0) < 1):
                    raise RuntimeError("请选择电脑麦克风输入设备")
                if not self.microphone and not device.get("isLoopbackDevice"):
                    raise RuntimeError("请选择标有 Loopback 的系统声音设备")
                self.device_name = device["name"]
                rate, channels = int(device["defaultSampleRate"]), int(device["maxInputChannels"])
                self.device_info = {"id": int(device["index"]), "name": self.device_name,
                                    "channels": channels, "rate": rate}
                block = max(1, int(rate * 0.05))
                incoming = queue.Queue(maxsize=40)
                overflow = threading.Event()

                def callback(data, frame_count, timing, status):
                    try:
                        incoming.put_nowait(data)
                    except queue.Full:
                        overflow.set()
                    return None, audio.paContinue

                detector = Segmenter(rate, threshold, silence, max_seconds=chunk_seconds)
                with manager.open(format=audio.paInt16, channels=channels, rate=rate, input=True,
                                  input_device_index=int(device["index"]), frames_per_buffer=block,
                                  stream_callback=callback) as stream:
                    self.ready.set()
                    last_level = 0
                    while not self.stop_event.is_set():
                        if overflow.is_set():
                            raise RuntimeError("采集音频积压，请关闭占用 CPU 的程序后重新开始")
                        try:
                            raw = incoming.get(timeout=0.1)
                            pcm = mix_to_mono(raw, channels)
                        except queue.Empty:
                            if not stream.is_active():
                                raise RuntimeError("音频设备已断开，请重新选择输出设备")
                            # WASAPI may stop callbacks when the output is idle: wall-clock silence still ends a turn.
                            pcm = np.zeros(int(rate * 0.1), dtype=np.int16)
                        pcm = amplify(pcm, gain)
                        now = time.monotonic()
                        was_active = detector.active
                        level, segment = detector.feed(pcm, now=now)
                        if was_active != detector.active or now - last_level >= 0.15:
                            self.on_activity(detector.active, detector.last_voice_at)
                        if now - last_level >= 0.15:
                            self.on_level(level)
                            last_level = now
                        if segment:
                            self.on_segment(segment)
                    final = detector.finish()
                    if final:
                        self.on_segment(final)
        except Exception as exc:
            self.failure = str(exc) if isinstance(exc, RuntimeError) else ("无法打开电脑麦克风，请检查设备与 Windows 麦克风权限" if self.microphone else "无法采集该输出设备，请检查设备连接并重试")
            if self.ready.is_set() and not self.stop_event.is_set():
                self.on_error(self.failure)
            self.ready.set()


class MicrophoneCapture(Capture):
    microphone = True


if __name__ == "__main__":
    print(json.dumps(_scan_devices("--microphones" in sys.argv), ensure_ascii=True))
