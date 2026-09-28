import asyncio
import contextlib
import re
import secrets
import time
from collections import deque
from datetime import datetime, timezone

from .audio import AudioSegment, Capture, list_devices
from .config import Settings
from .providers import ProviderError, Providers, demo_answer
from .practice import Practice
from .screen import (ScreenChangeDetector, ScreenError, ScreenHotkey, ScreenMonitorOptions,
                     ScreenRequest, capture_screen, encode_image, sample_screen)


class Engine:
    def __init__(self, settings: Settings, providers: Providers):
        self.settings, self.providers = settings, providers
        self.admin_token = secrets.token_urlsafe(32)
        self.controller_token = secrets.token_urlsafe(32)
        self.clients: dict[asyncio.Queue, str] = {}
        self.items = deque(maxlen=40)
        self.transcripts = deque(maxlen=60)
        self.capture = None
        self.answer_task = None
        self.screen_hotkey = None
        self.screen_hotkey_request = None
        self.screen_hotkey_task = None
        self.screen_monitor_task = None
        self.screen_monitor_options = None
        self.screen_monitor_item = None
        self.compact_task = None
        self.asr_task = None
        self.device_task = None
        self.auto_task = None
        self.pending_audio = None
        self.last_audio = None
        self.voice_active = False
        self.last_voice_at = None
        self.audio_queue = asyncio.Queue(maxsize=4)
        self.command_lock = asyncio.Lock()
        self.epoch = 0
        self.state = {"listening": False, "transcribing": False, "device": "", "level": 0,
                      "device_info": {}, "follow_default": True, "audio_notice": "",
                      "error": "", "draft": "", "controller_count": 0, "asr_ms": None,
                      "screen_capturing": False, "screen_hotkey": False, "screen_notice": "",
                      "screen_monitoring": False, "screen_monitor_count": 0,
                      "screen_monitor_interval": 2, "screen_monitor_phase": "off",
                      "context": {"backend": settings.answer_backend, "active": False, "turns": 0}}
        if hasattr(providers, "set_context_callback"):
            providers.set_context_callback(self.context_changed)
        self.practice = Practice(self)

    def context_changed(self, context):
        if self.settings.answer_backend == "codex":
            self.status(context=context)

    def busy(self):
        return self.practice.busy() or any(task and not task.done() for task in (self.answer_task, self.compact_task, self.auto_task))

    def snapshot(self) -> dict:
        return {"type": "snapshot", "state": dict(self.state), "items": list(self.items), "transcripts": list(self.transcripts)}

    def publish(self, event: dict):
        for queue in list(self.clients):
            if queue.full():
                # A slow/reconnecting phone receives a fresh state instead of losing answer characters.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot())
            else:
                queue.put_nowait(event)

    def status(self, **updates):
        self.state.update(updates)
        self.publish({"type": "status", "state": dict(self.state)})

    def subscribe(self, role: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=24)
        self.clients[queue] = role
        self.status(controller_count=sum(value == "controller" for value in self.clients.values()))
        queue.put_nowait(self.snapshot())
        return queue

    def unsubscribe(self, queue):
        self.clients.pop(queue, None)
        self.status(controller_count=sum(value == "controller" for value in self.clients.values()))

    async def start(self, device_id: int | None):
        if self.screen_monitor_options:
            raise ValueError("请先停止屏幕自动监测，再开始声音监听")
        if self.screen_hotkey_request or self.state["screen_capturing"]:
            raise ValueError("请先关闭屏幕快捷键并等待截图完成，再开始监听")
        if self.answer_task and not self.answer_task.done() and self.items and self.items[-1].get("source") == "screen":
            raise ValueError("请先停止屏幕解题或等待完成，再开始监听")
        if self.compact_task and not self.compact_task.done():
            raise ValueError("上下文正在压缩，请等待完成后再开始监听")
        if self.capture is not None:
            raise ValueError("采集已启动；切换设备请先停止")
        self.settings.check_ready(audio=True)
        if hasattr(self.providers, "prepare_answer"):
            await self.providers.prepare_answer(self.settings)
        self.epoch += 1
        await self.open_capture(device_id)
        self.asr_task = asyncio.create_task(self.transcription_worker(self.epoch, self.settings.model_copy()))
        self.device_task = asyncio.create_task(self.watch_devices(self.epoch))

    async def open_capture(self, device_id: int | None):
        epoch = self.epoch
        loop = asyncio.get_running_loop()

        def enqueue(segment):
            if epoch != self.epoch or not self.state["listening"]:
                return
            try:
                self.audio_queue.put_nowait(segment)
            except asyncio.QueueFull:
                self.status(error="语音识别跟不上采集速度，已停止监听。请检查网络或更换更快的识别模型。")
                asyncio.create_task(self.fail_capture(epoch, self.state["error"]))

        def level(value):
            if epoch == self.epoch and self.state["listening"]:
                self.status(level=round(value, 4))

        def error(message):
            asyncio.create_task(self.fail_capture(epoch, message))

        def activity(active, last_voice_at):
            if epoch == self.epoch:
                self.voice_active = active
                self.last_voice_at = last_voice_at

        capture = Capture(lambda segment: loop.call_soon_threadsafe(enqueue, segment),
                          lambda value: loop.call_soon_threadsafe(level, value),
                          lambda message: loop.call_soon_threadsafe(error, message),
                          lambda active, at: loop.call_soon_threadsafe(activity, active, at))
        self.capture = capture
        try:
            name = await asyncio.to_thread(capture.start, device_id, self.settings.energy_threshold, self.settings.silence_seconds, self.settings.asr_chunk_seconds, self.settings.audio_gain)
            self.status(listening=True, device=name, error="", audio_notice="",
                        device_info=getattr(capture, "device_info", {}), follow_default=device_id is None)
        except Exception:
            await asyncio.to_thread(capture.stop)
            self.capture = None
            raise

    async def switch_source(self, device_id: int | None):
        """Reopen just capture; keep ASR work, answers, pairing and model context."""
        if not self.capture:
            raise ValueError("请先开始监听")
        capture = self.capture
        await asyncio.to_thread(capture.stop)
        self.capture = None
        self.voice_active = False
        try:
            await self.open_capture(device_id)
        except Exception as exc:
            await self.stop()
            self.status(error=str(exc) if isinstance(exc, RuntimeError) else "声音来源切换失败，请重新选择后开始监听")
            raise

    async def watch_devices(self, epoch: int):
        while epoch == self.epoch:
            await asyncio.sleep(2)
            try:
                devices = await asyncio.to_thread(list_devices)
                async with self.command_lock:
                    if epoch != self.epoch or not self.capture:
                        return
                    info = self.state["device_info"]
                    if self.state["follow_default"]:
                        selected = next((device for device in devices if device["default"]), None)
                    else:
                        selected = next((device for device in devices if device["name"] == self.state["device"]), None)
                    if selected is None:
                        self.status(audio_notice="声音来源已不可用，请连接设备或在手机上选择其他来源")
                        continue
                    changed = any(selected.get(key) != info.get(key) for key in ("name", "channels", "rate"))
                    if changed:
                        await self.switch_source(None if self.state["follow_default"] else selected["id"])
                    elif self.state["audio_notice"]:
                        self.status(audio_notice="")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if epoch != self.epoch:
                    return
                self.status(audio_notice=str(exc) if isinstance(exc, RuntimeError) else "自动刷新声音来源失败，请点击刷新后重试")

    async def fail_capture(self, epoch, message):
        async with self.command_lock:
            if epoch == self.epoch:
                await self.stop()
                self.status(error=message)

    async def cancel_answer(self):
        if self.answer_task and not self.answer_task.done():
            self.answer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.answer_task
        self.answer_task = None

    async def stop(self):
        await self.practice.stop()
        await self.set_screen_monitor(None)
        await self.set_screen_hotkey(None)
        self.epoch += 1
        watcher, self.device_task = self.device_task, None
        if watcher and watcher is not asyncio.current_task():
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await self.cancel_pending_audio()
        self.last_audio = None
        self.voice_active = False
        self.last_voice_at = None
        self.status(listening=False, transcribing=False, level=0, draft="", audio_notice="")
        if self.asr_task:
            self.asr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.asr_task
            self.asr_task = None
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
        await self.cancel_answer()
        if self.compact_task and not self.compact_task.done():
            self.compact_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.compact_task
        self.compact_task = None
        if self.capture:
            capture = self.capture
            await asyncio.to_thread(capture.stop)
            self.capture = None

    async def clear(self):
        await self.stop()
        if hasattr(self.providers, "reset_context"):
            await self.providers.reset_context()
        self.items.clear()
        self.transcripts.clear()
        self.status(error="", device="", asr_ms=None, context={"backend": self.settings.answer_backend, "active": False, "turns": 0})
        self.publish(self.snapshot())

    async def transcription_worker(self, epoch: int, settings: Settings):
        parts = []
        poisoned = False
        while True:
            segment: AudioSegment = await self.audio_queue.get()
            self.status(transcribing=True)
            try:
                if poisoned:
                    if segment.final:
                        poisoned = False
                    continue
                if segment.wav:
                    began = time.monotonic()
                    text = await self.providers.transcribe(segment.wav, settings)
                    if epoch != self.epoch:
                        continue
                    self.status(asr_ms=round((time.monotonic() - began) * 1000))
                    if text:
                        parts.append(text)
                        self.status(draft=" ".join(parts)[-12000:])
                if segment.final:
                    question = " ".join(parts).strip()[:12000]
                    parts.clear()
                    self.status(draft="")
                    if len(question) >= 4:
                        transcript = {"id": secrets.token_hex(6), "text": question,
                                      "at": datetime.now(timezone.utc).isoformat()}
                        self.transcripts.append(transcript)
                        self.publish({"type": "transcript", "transcript": transcript})
                        if settings.auto_answer:
                            async with self.command_lock:
                                if epoch == self.epoch:
                                    await self.collect_audio_question(question, segment, epoch)
            except ProviderError as exc:
                parts.clear()
                poisoned = not segment.final
                await self.cancel_pending_audio()
                self.last_audio = None
                self.status(error=str(exc), draft="")
            except asyncio.CancelledError:
                raise
            except Exception:
                parts.clear()
                poisoned = not segment.final
                await self.cancel_pending_audio()
                self.last_audio = None
                self.status(error="处理语音时发生异常，请停止后重新开始", draft="")
            finally:
                self.status(transcribing=False)

    async def cancel_pending_audio(self, mark_cancelled=True):
        task, group = self.auto_task, self.pending_audio
        self.auto_task = self.pending_audio = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if mark_cancelled and group and group["item"]["status"] == "collecting":
            group["item"]["status"] = "cancelled"
            self.publish({"type": "item", "item": dict(group["item"])})

    async def collect_audio_question(self, question, segment, epoch):
        """Join by capture time, independently of ASR/network turnaround time."""
        started = segment.started_at
        ended = segment.ended_at if segment.ended_at is not None else time.monotonic()
        previous = self.last_audio
        explicit_new = bool(re.match(r"^(?:下一个问题|下一题|换一个问题|换个问题|换个话题|另外一个问题)", question))
        merge = bool(previous and started is not None and self.settings.question_merge_seconds > 0
                     and not previous["item"].get("candidate_answer")
                     and self.practice.state["item_id"] != previous["item"].get("id")
                     and 0 <= started - previous["ended_at"] <= self.settings.question_merge_seconds
                     and self.items and self.items[-1] is previous["item"] and not explicit_new
                     and len(previous["item"]["question"]) + len(question) + 1 <= 12000)
        await self.cancel_pending_audio(mark_cancelled=not merge)
        await self.cancel_answer()
        if merge:
            item = previous["item"]
            item.update(question=item["question"] + " " + question, answer="", status="collecting",
                        error="", first_token_ms=None, fragments=item.get("fragments", 1) + 1)
        else:
            item = {"id": secrets.token_hex(8), "question": question, "answer": "", "status": "collecting",
                    "error": "", "demo": False, "source": "audio", "fragments": 1,
                    "at": datetime.now(timezone.utc).isoformat(), "first_token_ms": None}
            self.items.append(item)
        group = {"item": item, "ended_at": ended, "epoch": epoch}
        self.pending_audio = self.last_audio = group
        self.publish({"type": "item", "item": dict(item)})
        self.auto_task = asyncio.create_task(self.answer_after_pause(group))

    async def answer_after_pause(self, group):
        while self.pending_audio is group and group["epoch"] == self.epoch:
            last_voice = max(group["ended_at"], self.last_voice_at or group["ended_at"])
            quiet = time.monotonic() - last_voice >= self.settings.question_merge_seconds
            if quiet and not self.voice_active and not self.state["transcribing"] and self.audio_queue.empty():
                async with self.command_lock:
                    if self.pending_audio is not group or group["epoch"] != self.epoch:
                        return
                    last_voice = max(group["ended_at"], self.last_voice_at or group["ended_at"])
                    if (self.voice_active or self.state["transcribing"] or not self.audio_queue.empty()
                            or time.monotonic() - last_voice < self.settings.question_merge_seconds):
                        continue
                    self.pending_audio = None
                    await self.ask(group["item"]["question"], source="audio", replace_item=group["item"])
                    return
            await asyncio.sleep(0.05)

    async def ask(self, question: str, demo=False, *, source="manual", replace_item=None, screen=None):
        if self.screen_monitor_options and source != "screen":
            raise ValueError("请先停止屏幕自动监测，再手动提问或运行演示")
        if self.compact_task and not self.compact_task.done():
            raise ValueError("上下文正在压缩，请等待完成后再提交问题")
        if not demo and screen is None:
            self.settings.check_ready()
        if demo and self.state["listening"]:
            raise ValueError("请先停止监听，再运行演示")
        if source != "audio":
            await self.cancel_pending_audio()
            self.last_audio = None
        await self.cancel_answer()
        item = replace_item or {"id": secrets.token_hex(8), "at": datetime.now(timezone.utc).isoformat(),
                                "source": source, "fragments": 1}
        item.update(question=question, answer="", status="generating", error="", demo=demo, first_token_ms=None)
        history = [entry for entry in self.items if entry is not item]
        if replace_item is None:
            self.items.append(item)
        self.status(error="")
        self.publish({"type": "item", "item": dict(item)})
        self.answer_task = asyncio.create_task(self.generate(item, history, self.settings.model_copy(), screen=screen))
        return item["id"]

    def check_screen_ready(self, request):
        if self.practice.busy():
            raise ValueError("请先结束录音、转写和复盘，再使用屏幕解题")
        if self.state["listening"]:
            raise ValueError("请先停止声音监听，再使用屏幕解题")
        if self.compact_task and not self.compact_task.done():
            raise ValueError("请等待上下文压缩完成，再使用屏幕解题")
        if request.backend == "api" and (not self.settings.llm_model or not self.settings.llm_api_key):
            raise ValueError("请先在设置中填写支持图片输入的兼容 API 模型和密钥，或选择 Codex")

    async def ask_screen(self, request: ScreenRequest):
        if self.screen_monitor_options:
            raise ValueError("自动监测正在运行，请先停止后再手动截图")
        self.check_screen_ready(request)
        self.status(screen_capturing=True, screen_notice="正在截取电脑屏幕…")
        try:
            shot = await asyncio.to_thread(capture_screen, request)
            item_id = await self.submit_screen(request, shot["image"])
            self.status(screen_notice="截图已提交，答案会显示在回答列表")
            return item_id
        finally:
            self.status(screen_capturing=False)

    async def submit_screen(self, request, image_url, *, automatic=False):
        title = ("自动识题 · " if automatic else "屏幕题目 · ") + ("框选区域" if request.region else "整块屏幕")
        if request.instruction:
            title += " · " + request.instruction
        return await self.ask(title, source="screen", screen=(image_url, request))

    async def set_screen_monitor(self, options: ScreenMonitorOptions | None):
        self.screen_monitor_options = None
        worker, self.screen_monitor_task = self.screen_monitor_task, None
        if worker and worker is not asyncio.current_task():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        if self.screen_monitor_item and self.screen_monitor_item["status"] == "generating":
            await self.cancel_answer()
        self.screen_monitor_item = None
        if self.state["screen_monitoring"]:
            self.status(screen_monitoring=False, screen_monitor_phase="off", screen_notice="自动监测已停止")
        if options is None:
            return
        self.check_screen_ready(options.request)
        await self.set_screen_hotkey(None)
        await self.cancel_pending_audio()
        options = options.model_copy(deep=True)
        self.screen_monitor_options = options
        self.status(screen_monitoring=True, screen_monitor_count=0,
                    screen_monitor_interval=options.interval_seconds, screen_monitor_phase="watching",
                    screen_notice="自动监测已开启，画面稳定后解答当前题目")
        self.screen_monitor_task = asyncio.create_task(self.watch_screen(options))

    def screen_monitor_status(self, phase, message):
        if self.state["screen_monitor_phase"] != phase or self.state["screen_notice"] != message:
            self.status(screen_monitor_phase=phase, screen_notice=message)

    async def watch_screen(self, options):
        detector = ScreenChangeDetector(options.sensitivity)
        try:
            while self.screen_monitor_options is options:
                if self.screen_monitor_item and self.screen_monitor_item["status"] == "error":
                    self.screen_monitor_status("error", "解题失败，自动监测已停止；请查看回答卡片中的错误后重新开启")
                    return
                # Capture/comparison/encoding stay outside command_lock, so stop can invalidate a slow sample.
                image, signature = await asyncio.to_thread(sample_screen, options.request)
                if self.screen_monitor_options is not options:
                    return
                ready = detector.observe(signature)
                if ready and not self.busy():
                    shot = await asyncio.to_thread(encode_image, image)
                    async with self.command_lock:
                        if self.screen_monitor_options is not options:
                            return
                        if self.screen_monitor_item and self.screen_monitor_item["status"] == "error":
                            raise ScreenError("上一次解题失败，请查看回答卡片中的错误后重新开启")
                        if not self.busy():
                            self.check_screen_ready(options.request)
                            await self.submit_screen(options.request, shot["image"], automatic=True)
                            self.screen_monitor_item = self.items[-1]
                            detector.mark_submitted(signature)
                            self.status(screen_monitor_count=self.state["screen_monitor_count"] + 1)
                            self.screen_monitor_status("answering", "画面已稳定，正在自动解答；答案会显示在回答页")
                    del shot
                elif self.busy():
                    self.screen_monitor_status("answering", "正在解答；完成后继续检查最新画面")
                elif detector.stable_samples == 1:
                    self.screen_monitor_status("settling", "检测到画面变化，等待画面稳定")
                else:
                    self.screen_monitor_status("watching", "正在监测，近期已提交的相似画面不会重复解答")
                del image, signature
                await asyncio.sleep(options.interval_seconds)
        except asyncio.CancelledError:
            raise
        except (ScreenError, ValueError) as exc:
            self.screen_monitor_status("error", f"自动监测已停止：{exc}")
        except Exception:
            self.screen_monitor_status("error", "自动监测发生异常并已停止，请刷新屏幕列表后重试")
        finally:
            if self.screen_monitor_options is options:
                if self.screen_monitor_item and self.screen_monitor_item["status"] == "generating":
                    await self.cancel_answer()
                self.screen_monitor_options = None
                self.status(screen_monitoring=False)

    async def set_screen_hotkey(self, request: ScreenRequest | None):
        if request is not None and self.screen_monitor_options:
            raise ValueError("自动监测已接管屏幕，请先停止后再启用快捷键")
        self.screen_hotkey_request = None
        if self.screen_hotkey:
            listener, self.screen_hotkey = self.screen_hotkey, None
            await asyncio.to_thread(listener.stop)
        task, self.screen_hotkey_task = self.screen_hotkey_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.status(screen_hotkey=False, screen_notice="快捷键已关闭" if self.state["screen_hotkey"] else self.state["screen_notice"])
        if request is None:
            return
        self.check_screen_ready(request)
        loop = asyncio.get_running_loop()
        listener = ScreenHotkey(lambda: loop.call_soon_threadsafe(self.screen_hotkey_pressed))
        self.screen_hotkey = listener
        try:
            await asyncio.to_thread(listener.start)
        except BaseException:
            await asyncio.to_thread(listener.stop)
            self.screen_hotkey = None
            raise
        self.screen_hotkey_request = request.model_copy(deep=True)
        self.status(screen_hotkey=True, screen_notice="Ctrl + Alt + S 已启用；按一次截取并解答一次")

    def screen_hotkey_pressed(self):
        if not self.screen_hotkey_request or (self.screen_hotkey_task and not self.screen_hotkey_task.done()):
            return

        async def run():
            async with self.command_lock:
                if self.screen_hotkey_request is None:
                    return
                if self.busy():
                    self.status(screen_notice="正在回答，已忽略重复快捷键；完成后可再次截图")
                    return
                try:
                    await self.ask_screen(self.screen_hotkey_request)
                except (ValueError, ScreenError) as exc:
                    self.status(screen_notice=str(exc))
                except Exception:
                    self.status(screen_notice="快捷键截图失败，请在手机上重试")
        self.screen_hotkey_task = asyncio.create_task(run())

    async def compact_context(self):
        if self.screen_monitor_options:
            raise ValueError("请先停止屏幕自动监测，再压缩上下文")
        if self.settings.answer_backend != "codex":
            raise ValueError("压缩上下文仅适用于 Codex 会话模式")
        if self.state["listening"] or self.busy():
            raise ValueError("请先停止监听并等待回答完成，再压缩上下文")
        if not self.state["context"].get("active"):
            raise ValueError("请先完成一次 Codex 回答，建立会话后再压缩")

        async def run():
            try:
                await self.providers.compact_context()
            except ProviderError as exc:
                self.status(error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status(error="上下文压缩失败，请新建会话后重试")
        self.compact_task = asyncio.create_task(run())

    async def apply_settings(self, settings):
        await self.practice.stop()
        await self.set_screen_monitor(None)
        await self.set_screen_hotkey(None)
        old = self.settings
        keys = ("answer_backend", "codex_executable", "codex_model", "codex_effort", "codex_fast",
                "claude_executable", "claude_model", "claude_direct", "role", "background")
        changed = any(getattr(old, key) != getattr(settings, key) for key in keys)
        self.settings = settings
        if changed:
            await self.clear()
        self.status(context={**self.state["context"], "backend": settings.answer_backend})

    async def close(self):
        await self.stop()
        if hasattr(self.providers, "close"):
            await self.providers.close()

    async def generate(self, item, history, settings, *, screen=None):
        start = last_push = time.monotonic()
        stream = (self.providers.answer_screen(screen[0], screen[1], settings) if screen else
                  demo_answer() if item["demo"] else self.providers.answer(item["question"], settings, history))
        try:
            async for text in stream:
                if item["first_token_ms"] is None:
                    item["first_token_ms"] = round((time.monotonic() - start) * 1000)
                item["answer"] += text
                if len(item["answer"]) > 24000:
                    raise ProviderError("回答过长，已停止生成并保留现有内容")
                if time.monotonic() - last_push > 0.075:
                    self.publish({"type": "item", "item": dict(item)})
                    last_push = time.monotonic()
            item["status"] = "done"
        except asyncio.CancelledError:
            item["status"] = "cancelled"
            raise
        except ProviderError as exc:
            item.update(status="error", error=str(exc))
        except Exception:
            item.update(status="error", error="生成回答时发生异常，请检查模型配置后重试")
        finally:
            await stream.aclose()
            self.publish({"type": "item", "item": dict(item)})

    def rotate_controller(self):
        self.practice.revoke()
        # Invalidate queued callbacks even if revocation is called without stop().
        self.screen_monitor_options = None
        if self.screen_monitor_task:
            self.screen_monitor_task.cancel()
        if self.screen_monitor_item and self.screen_monitor_item["status"] == "generating" and self.answer_task:
            self.answer_task.cancel()
        self.state["screen_monitoring"] = False
        self.state["screen_monitor_phase"] = "off"
        self.screen_hotkey_request = None
        if self.screen_hotkey:
            self.screen_hotkey.stop()
            self.screen_hotkey = None
        self.state["screen_hotkey"] = False
        self.controller_token = secrets.token_urlsafe(32)
        for queue, role in list(self.clients.items()):
            if role == "controller":
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "revoked"})
                self.clients.pop(queue, None)
        self.status(controller_count=0)
