"""Windows screen capture, local change detection and an optional registered hotkey."""
import base64
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import io
import math
import os
import threading
from collections import deque
from typing import Literal

import numpy as np
from PIL import Image, ImageGrab
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScreenError(RuntimeError):
    pass


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_screen(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("框选区域超出屏幕")
        return self


class ScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    monitor_id: str = Field(min_length=1, max_length=200)
    region: Region | None = None
    backend: Literal["codex", "api"] = "codex"
    style: Literal["brief", "steps", "code"] = "brief"
    instruction: str = Field(default="", max_length=2000)


class ScreenMonitorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request: ScreenRequest
    interval_seconds: float = Field(default=2, ge=1, le=15)
    sensitivity: Literal["high", "normal", "low"] = "normal"


class ScreenChangeDetector:
    """Compare small local signatures; require two stable samples, remember 12 submissions."""
    def __init__(self, sensitivity="normal"):
        self.threshold = {"high": .0001, "normal": .0005, "low": .002}[sensitivity]
        self.candidate = None
        self.stable_samples = 0
        self.submitted = deque(maxlen=12)

    @staticmethod
    def signature(image):
        reduced = image.convert("L")
        reduced.thumbnail((640, 640), Image.Resampling.BILINEAR)
        return np.array(reduced, dtype=np.int16)

    def changed(self, first, second):
        if first.shape != second.shape:
            return True
        return np.count_nonzero(np.abs(first - second) > 28) / first.size >= self.threshold

    def observe(self, signature):
        if self.candidate is None or self.changed(signature, self.candidate):
            self.candidate = signature
            self.stable_samples = 1
            return False
        self.stable_samples += 1
        return not any(not self.changed(signature, previous) for previous in self.submitted)

    def mark_submitted(self, signature):
        self.submitted.append(signature.copy())


@contextmanager
def physical_pixels():
    if os.name != "nt":
        raise ScreenError("屏幕采集目前仅支持 Windows 10/11")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    setter = user32.SetThreadDpiAwarenessContext
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_void_p
    previous = setter(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2, scoped to this worker.
    if not previous:
        raise ScreenError("无法获取屏幕物理像素坐标，请检查 Windows 显示设置")
    try:
        yield user32
    finally:
        setter(previous)


def _monitors(user32):
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    monitors = []

    @callback_type
    def collect(handle, dc, rect, data):
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return False
        r = info.rcMonitor
        bounds = [r.left, r.top, r.right, r.bottom]
        monitors.append({"id": info.szDevice + ":" + ",".join(map(str, bounds)),
                         "name": info.szDevice, "bounds": bounds,
                         "width": r.right - r.left, "height": r.bottom - r.top,
                         "primary": bool(info.dwFlags & 1)})
        return True

    if not user32.EnumDisplayMonitors(None, None, collect, 0) or not monitors:
        raise ScreenError("无法读取显示器，请确认电脑已登录且屏幕可用")
    return sorted(monitors, key=lambda item: (not item["primary"], item["name"]))


def list_monitors():
    with physical_pixels() as user32:
        return _monitors(user32)


def region_bounds(monitor, region):
    left, top, right, bottom = monitor["bounds"]
    if region:
        width, height = right - left, bottom - top
        right = left + min(width, math.ceil((region.x + region.width) * width))
        bottom = top + min(height, math.ceil((region.y + region.height) * height))
        left += math.floor(region.x * width)
        top += math.floor(region.y * height)
    if right - left < 20 or bottom - top < 20:
        raise ScreenError("框选区域太小，请至少选择 20 × 20 像素的题目区域")
    return left, top, right, bottom


def encode_image(image, max_edge=2560):
    image = image.convert("RGB")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG")
    if output.tell() > 8 * 1024 * 1024:
        raise ScreenError("截图内容过大，请框选更小的题目区域")
    return {"image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
            "width": image.width, "height": image.height}


def capture_frame(request: ScreenRequest, *, preview=False):
    try:
        with physical_pixels() as user32:
            monitor = next((item for item in _monitors(user32) if item["id"] == request.monitor_id), None)
            if monitor is None:
                raise ScreenError("显示器或分辨率已变化，请刷新屏幕列表并重新选择")
            bounds = region_bounds(monitor, None if preview else request.region)
            image = ImageGrab.grab(bbox=bounds, all_screens=True)
            if image.size != (bounds[2] - bounds[0], bounds[3] - bounds[1]):
                raise ScreenError("截图尺寸与显示器不一致，请刷新屏幕列表")
            return image, monitor
    except ScreenError:
        raise
    except Exception as exc:
        raise ScreenError("截图失败，请确认电脑未锁屏、显示器已连接且内容允许截图") from exc


def capture_screen(request: ScreenRequest, *, preview=False):
    image, monitor = capture_frame(request, preview=preview)
    return {**encode_image(image, 1280 if preview else 2560), "monitor": monitor}


def sample_screen(request: ScreenRequest):
    image, _ = capture_frame(request)
    return image, ScreenChangeDetector.signature(image)


class ScreenHotkey:
    """RegisterHotKey sees only Ctrl+Alt+S; no global keyboard hook or key logging."""
    def __init__(self, callback):
        self.callback = callback
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error = ""
        self.thread = None

    def start(self):
        if os.name != "nt":
            raise ScreenError("全局快捷键目前仅支持 Windows")
        self.thread = threading.Thread(target=self._run, daemon=True, name="screen-hotkey")
        self.thread.start()
        if not self.ready.wait(3):
            self.stop()
            raise ScreenError("注册快捷键超时，请使用手机截图按钮")
        if self.error:
            self.stop()
            raise ScreenError(self.error)

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        registered = False
        try:
            registered = bool(user32.RegisterHotKey(None, 1, 0x4000 | 0x0002 | 0x0001, ord("S")))
            if not registered:
                self.error = "Ctrl + Alt + S 已被其他程序占用，请使用手机截图按钮"
                return
            self.ready.set()
            message = wintypes.MSG()
            while not self.stopped.wait(0.05):
                while user32.PeekMessageW(ctypes.byref(message), None, 0x0312, 0x0312, 1):
                    if message.wParam == 1 and not self.stopped.is_set():
                        self.callback()
        except Exception:
            self.error = "快捷键注册失败，请使用手机截图按钮"
        finally:
            if registered:
                user32.UnregisterHotKey(None, 1)
            self.ready.set()

    def stop(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
