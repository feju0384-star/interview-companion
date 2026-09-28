"""Explicit, question-bound microphone recording and evidence-based review."""
import asyncio
import contextlib
import copy
import secrets
from datetime import datetime, timezone

from .audio import MicrophoneCapture
from .providers import ProviderError


class Practice:
    def __init__(self, engine):
        self.engine = engine
        self.session = None
        self.review_task = None
        self.state = dict(recording=False, processing=False, reviewing=False, item_id=None,
                          device="", level=0, notice="")
        engine.state["practice"] = dict(self.state)

    def status(self, **updates):
        self.state.update(updates)
        self.engine.status(practice=dict(self.state))

    def busy(self):
        return self.session is not None or self.state["recording"] or self.state["processing"] or bool(self.review_task and not self.review_task.done())

    def item(self, item_id):
        item = next((item for item in self.engine.items if item.get("id") == item_id), None)
        if not item or item.get("demo") or item.get("source") == "screen":
            raise ValueError("请选择当前会话中的面试问题；演示和屏幕题不参与复盘")
        if item.get("status") == "collecting":
            raise ValueError("请等待问题收集完整，再录制回答")
        return item

    def publish(self, item):
        self.engine.publish({"type": "item", "item": copy.deepcopy(item)})

    def save(self, item_id, answer):
        if self.busy():
            raise ValueError("请先结束录音并等待转写或复盘完成，再修改回答")
        item = self.item(item_id)
        answer = answer.strip()
        if answer != item.get("candidate_answer", ""):
            item["review"] = None
        item.update(candidate_answer=answer, candidate_status="saved", candidate_error="")
        self.publish(item)
        return item

    def add_question(self, question):
        question = question.strip()
        if not question:
            raise ValueError("请输入练习问题")
        if self.busy():
            raise ValueError("请先结束当前录音、转写或复盘，再添加练习题")
        item = dict(id=secrets.token_hex(8), question=question, answer="", status="ready", source="practice",
                    demo=False, first_token_ms=None, at=datetime.now(timezone.utc).isoformat())
        self.engine.items.append(item)
        self.publish(item)
        return item

    async def start(self, item_id, device_id=None):
        if self.busy():
            raise ValueError("已有录音、转写或复盘正在进行，请先结束")
        if self.engine.screen_monitor_options or self.engine.screen_hotkey_request or self.engine.state["screen_capturing"]:
            raise ValueError("请先停止屏幕解题，再录制面试回答")
        settings = self.engine.settings.model_copy()
        if not settings.asr_model or not settings.asr_api_key:
            raise ValueError("请先在设置中配置语音识别模型和 API Key")
        item = self.item(item_id)
        loop = asyncio.get_running_loop()
        session = dict(item=item, queue=asyncio.Queue(maxsize=8), capture=None, worker=None, finish=None, timer=None)
        self.session = session

        def enqueue(segment):
            if self.session is not session or session.get("revoked"):
                return
            try:
                session["queue"].put_nowait(segment)
            except asyncio.QueueFull:
                asyncio.create_task(self.fail(session, "语音识别跟不上录音，已结束采集；请核对保留的文字后重试"))

        def level(value):
            if self.session is session and not session.get("revoked"):
                self.status(level=round(value, 4))

        capture = MicrophoneCapture(
            lambda segment: loop.call_soon_threadsafe(enqueue, segment),
            lambda value: loop.call_soon_threadsafe(level, value),
            lambda message: loop.call_soon_threadsafe(lambda: asyncio.create_task(self.fail(session, message))))
        session["capture"] = capture
        try:
            name = await asyncio.to_thread(capture.start, device_id, settings.energy_threshold,
                                           settings.silence_seconds, settings.asr_chunk_seconds, settings.audio_gain)
        except BaseException:
            self.session = None
            await asyncio.to_thread(capture.stop)
            raise
        item.update(candidate_status="recording", candidate_error="", review=None)
        item.setdefault("candidate_answer", "")
        self.publish(item)
        self.status(recording=True, processing=False, item_id=item_id, device=name, level=0,
                    notice="正在录制电脑麦克风，文字追加到当前题；单次最多 5 分钟")
        session["worker"] = asyncio.create_task(self.transcribe(session, settings))
        session["timer"] = asyncio.create_task(self.limit_recording(session))

    async def limit_recording(self, session):
        await asyncio.sleep(300)
        async with self.engine.command_lock:
            if self.session is session and self.state["recording"]:
                await self.finish_recording()

    async def transcribe(self, session, settings):
        try:
            while self.session is session:
                segment = await session["queue"].get()
                if segment is None:
                    return
                if not segment.wav:
                    continue
                text = await self.engine.providers.transcribe(segment.wav, settings)
                if self.session is not session or session.get("revoked"):
                    return
                item = self.item(session["item"]["id"])
                if text:
                    combined = " ".join(filter(None, (item.get("candidate_answer", ""), text))).strip()
                    if len(combined) > 12000:
                        raise ValueError("回答达到 12000 字上限，已保留此前文字；请校正后复盘")
                    item["candidate_answer"] = combined
                    self.publish(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ProviderError, ValueError)) else "回答转写失败，请检查识别配置后重试"
            session["failed"] = True
            # The worker must exit before cleanup waits for it.
            asyncio.create_task(self.fail(session, message))

    async def finish_recording(self):
        session = self.session
        if not session or not self.state["recording"]:
            return
        capture, session["capture"] = session["capture"], None
        try:
            await asyncio.to_thread(capture.stop)
        except Exception:
            session["capture"] = capture
            raise
        await asyncio.sleep(0)  # Deliver the capture thread's final segment before the sentinel.
        session["item"]["candidate_status"] = "transcribing"
        self.publish(session["item"])
        self.status(recording=False, processing=True, level=0, notice="录音已结束，正在完成最后的转写…")
        session["finish"] = asyncio.create_task(self.drain(session))

    async def drain(self, session):
        try:
            async with asyncio.timeout(90):
                await session["queue"].put(None)
                await session["worker"]
            async with self.engine.command_lock:
                if self.session is not session:
                    return
                item = session["item"]
                # An ASR error schedules fail(); never replace it with a success state.
                if session.get("failed"):
                    return
                await self.end_session(session)
                item["candidate_status"] = "saved"
                self.publish(item)
                self.status(notice="转写完成，请核对文字后生成复盘" if item.get("candidate_answer") else "未识别到语音，请检查麦克风和电平，也可直接输入回答")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self.fail(session, "最后的转写超时，已保留收到的文字；请核对后再复盘")

    async def end_session(self, session):
        self.session = None  # Late callbacks and in-flight ASR can no longer append.
        for key in ("timer", "worker", "finish"):
            task = session[key]
            if task and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if session["capture"]:
            await asyncio.to_thread(session["capture"].stop)
            session["capture"] = None
        while not session["queue"].empty():
            session["queue"].get_nowait()
        self.status(recording=False, processing=False, item_id=None, level=0)

    def revoke(self):
        if self.session:
            self.session["revoked"] = True
            asyncio.create_task(self.fail(self.session, "配对已撤销，录音已停止"))
        if self.review_task:
            self.review_task.cancel()

    async def fail(self, session, message):
        async with self.engine.command_lock:
            if self.session is session:
                item = session["item"]
                await self.end_session(session)
                item.update(candidate_status="error", candidate_error=message)
                self.publish(item)
                self.status(notice=message)

    async def stop(self):
        if self.session:
            session = self.session
            await self.end_session(session)
            session["item"].update(candidate_status="cancelled", candidate_error="已停止，保留已转写文字；未完成片段已取消")
            self.publish(session["item"])
        if self.review_task and not self.review_task.done():
            self.review_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.review_task
        self.review_task = None
        self.status(recording=False, processing=False, reviewing=False, item_id=None, level=0, notice="")

    async def review(self, item_id, answer):
        self.engine.settings.check_ready()
        if not answer.strip():
            raise ValueError("请先录制或填写你实际说过的回答")
        item = self.save(item_id, answer)
        review = dict(status="generating", text="", error="", question=item["question"],
                      answer=item["candidate_answer"], at=datetime.now(timezone.utc).isoformat())
        item["review"] = review
        self.publish(item)
        self.status(reviewing=True, item_id=item_id, notice="正在根据你的实际回答生成复盘…")
        self.review_task = asyncio.create_task(self.generate_review(item, review, self.engine.settings.model_copy()))

    async def generate_review(self, item, review, settings):
        stream = self.engine.providers.review(review["question"], review["answer"], settings)
        try:
            async for text in stream:
                if self.item(item["id"]) is not item or item["question"] != review["question"]:
                    raise ValueError("问题已变化，请重新选择并生成复盘")
                review["text"] += text
                if len(review["text"]) > 24000:
                    raise ProviderError("复盘超过长度上限，已保留现有内容")
                self.publish(item)
            review["status"] = "done"
        except asyncio.CancelledError:
            review["status"] = "cancelled"
            raise
        except Exception as exc:
            review.update(status="error", error=str(exc) if isinstance(exc, (ProviderError, ValueError)) else "复盘生成失败，请检查模型配置后重试")
        finally:
            try:
                await stream.aclose()
            finally:
                self.publish(item)
                self.status(reviewing=False, item_id=None, notice="复盘已生成" if review["status"] == "done" else "复盘未完成，已保留收到的内容")
