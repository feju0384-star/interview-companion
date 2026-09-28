"""A private, in-memory Codex App Server conversation over stdio JSON-RPC.

No shell strings, no --last, no access to the desktop task's conversation.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .config import Settings
from .prompts import interview_instructions


class CodexError(RuntimeError):
    pass


DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "multi_agent", "multi_agent_v2", "goals", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "image_generation", "view_image", "skill_search",
    "code_mode", "code_mode_host", "workspace_dependencies",
)


def find_codex(configured_path: str = "") -> str:
    path = configured_path.strip() or shutil.which("codex.exe" if os.name == "nt" else "codex")
    if not path and not configured_path and os.name == "nt":
        bundled = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        candidates = sorted(bundled.glob("*/codex.exe"), key=lambda value: value.stat().st_mtime, reverse=True)
        path = str(candidates[0]) if candidates else None
    if not path or not Path(path).is_file():
        raise CodexError("未找到 Codex CLI，请安装并登录；也可以在设置中填写 codex.exe 的完整路径")
    if os.name == "nt" and Path(path).suffix.lower() != ".exe":
        raise CodexError("Windows 版本需要 codex.exe，请填写原生可执行文件路径，不支持 .cmd 或 PowerShell 命令")
    return str(Path(path).resolve())


class CodexBackend:
    def __init__(self, on_context=None, command_factory=None, instructions=None):
        self.on_context = on_context or (lambda _: None)
        self.instructions = instructions
        self.command_factory = command_factory
        self.process = None
        self.reader_task = None
        self.stderr_task = None
        self.pending = {}
        self.request_id = 0
        self.thread_id = None
        self.turn_id = None
        self.events = None
        self.turn_done = None
        self.active_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.fingerprint = None
        self.account_type = None
        self.last_executable = ""
        self.disabled_mcp_names = set()
        self.closing = False
        self.context = self.empty_context()

    @staticmethod
    def empty_context():
        return {"backend": "codex", "active": False, "turns": 0, "compactions": 0,
                "compacting": False, "context_tokens": None, "context_window": None,
                "model": "", "lost": False}

    def update_context(self, **changes):
        self.context.update(changes)
        self.on_context(dict(self.context))

    def command(self, settings: Settings):
        if self.command_factory:
            return self.command_factory(settings)
        executable = find_codex(settings.codex_executable)
        self.last_executable = executable
        args = [executable, "app-server", "--stdio"]
        for name in DISABLED_FEATURES:
            args.extend(["-c", f"features.{name}=false"])
        for name in sorted(self.disabled_mcp_names):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise CodexError("发现无法安全禁用的 MCP 配置名称，请使用不含特殊字符的 MCP 名称后重试")
            args.extend(["-c", f"mcp_servers.{name}.enabled=false"])
        for override in [
            'web_search="disabled"', 'approval_policy="never"', 'sandbox_mode="read-only"',
            'project_doc_max_bytes=0', 'features.skip_host_skill_discovery=true',
        ]:
            args.extend(["-c", override])
        return args

    async def ensure_process(self, settings: Settings):
        async with self.start_lock:
            if self.process and self.process.returncode is None and self.reader_task and not self.reader_task.done():
                return
            if self.context["lost"]:
                raise CodexError("Codex 会话连接已丢失，请点击“新建上下文”后继续；不会悄悄切到空白会话")
            folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "InterviewCompanion" / "codex-workspace"
            folder.mkdir(parents=True, exist_ok=True)
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            env = dict(os.environ)
            # Prevent inheriting a parent desktop task id or a forced API key from the host agent.
            for key in ["CODEX_THREAD_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_API_KEY", "OPENAI_API_KEY"]:
                env.pop(key, None)
            try:
                for attempt in range(2):
                    self.closing = False
                    self.process = await asyncio.create_subprocess_exec(
                        *self.command(settings), cwd=folder, env=env, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        creationflags=flags, limit=16 * 1024 * 1024,
                    )
                    self.reader_task = asyncio.create_task(self.read_loop())
                    self.stderr_task = asyncio.create_task(self.drain_stderr())
                    await self.rpc("initialize", {"clientInfo": {"name": "interview_companion", "title": "听答", "version": "0.2.0"}})
                    await self.write({"method": "initialized", "params": {}})
                    # Config table overrides are merged, so mcp_servers={} alone does not disable inherited MCPs.
                    config = (await self.rpc("config/read", {"includeLayers": False})).get("config", {})
                    enabled = {name for name, value in (config.get("mcp_servers") or {}).items()
                               if value.get("enabled", True)}
                    if not enabled:
                        break
                    self.disabled_mcp_names.update(enabled)
                    await self.close(reset=True)
                    if attempt:
                        raise CodexError("无法禁用继承的 MCP 工具，已停止 Codex 接入")
            except BaseException:
                await self.close(reset=True)
                raise

    async def drain_stderr(self):
        # Do not relay or persist CLI diagnostic bodies: they can include paths or account data.
        while await self.process.stderr.read(8192):
            pass

    async def write(self, message):
        if not self.process or self.process.returncode is not None:
            raise CodexError("Codex 后台已退出，请新建上下文后重试")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise CodexError("Codex 后台连接已断开") from exc

    async def rpc(self, method, params, timeout=20):
        self.request_id += 1
        request_id = self.request_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise CodexError(f"Codex 的 {method} 请求超时，请检查登录状态和 CLI 版本") from exc
        finally:
            self.pending.pop(request_id, None)

    async def read_loop(self):
        failure = CodexError("Codex 后台连接中断，请新建上下文后重试")
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            # Original response could include user material; surface only the protocol code.
                            code = message["error"].get("code", "unknown")
                            future.set_exception(CodexError(f"Codex 拒绝请求（{code}），请检查登录、模型与 CLI 版本"))
                        else:
                            future.set_result(message.get("result", {}))
                elif "method" in message and "id" in message:
                    # This app never grants tool approvals or executes client-side tools.
                    await self.write({"id": message["id"], "error": {"code": -32601, "message": "This client only supports text responses."}})
                    if self.events:
                        self.events.put_nowait({"local_error": "Codex 请求了工具或权限；本应用只接受文字回答，本轮已停止"})
                else:
                    self.notification(message)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            if not self.closing:
                if self.events:
                    with contextlib.suppress(asyncio.QueueFull):
                        self.events.put_nowait({"local_error": str(failure)})
                if self.thread_id:
                    self.update_context(active=False, lost=True, compacting=False)

    def notification(self, message):
        method, params = message.get("method"), message.get("params", {})
        if params.get("threadId") != self.thread_id:
            return
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            self.update_context(context_tokens=usage.get("last", {}).get("totalTokens"),
                                context_window=usage.get("modelContextWindow"))
        item = params.get("item", {})
        if item.get("type") == "contextCompaction":
            if method == "item/started":
                self.update_context(compacting=True)
            elif method == "item/completed":
                self.update_context(compacting=False, compactions=self.context["compactions"] + 1)
        if method == "turn/completed":
            completed_id = params.get("turn", {}).get("id")
            if self.turn_done and not self.turn_done.done() and (self.turn_id is None or self.turn_id == completed_id):
                self.turn_done.set_result(params.get("turn", {}))
        if method == "turn/started" and self.events is not None and self.turn_id is None:
            self.turn_id = params.get("turn", {}).get("id")
        if self.events is not None and method in {"turn/started", "turn/completed", "item/started", "item/completed", "item/agentMessage/delta", "error"}:
            if self.turn_id and params.get("turnId") and params["turnId"] != self.turn_id:
                return
            try:
                self.events.put_nowait(message)
            except asyncio.QueueFull:
                # Never silently lose answer text.
                while not self.events.empty():
                    self.events.get_nowait()
                self.events.put_nowait({"local_error": "Codex 输出积压，已中止本轮；请重试"})

    async def probe(self, settings: Settings):
        try:
            await self.ensure_process(settings)
            result = await self.rpc("account/read", {"refreshToken": False})
            account = result.get("account") or {}
            self.account_type = account.get("type")
            return {"installed": True, "logged_in": bool(account) or not result.get("requiresOpenaiAuth", True),
                    "auth_type": self.account_type, "context": dict(self.context)}
        except (CodexError, OSError) as exc:
            message = str(exc) if isinstance(exc, CodexError) else "无法启动 Codex CLI，请检查可执行文件路径"
            return {"installed": bool(self.process), "logged_in": False, "error": message, "context": dict(self.context)}

    async def list_models(self, settings: Settings) -> list[dict]:
        """Read the account's picker catalog without starting a model conversation."""
        account = await self.probe(settings)
        if not account["logged_in"]:
            raise CodexError(account.get("error") or "Codex 尚未登录，请先在电脑登录后刷新模型列表")
        models, seen_models, seen_cursors = [], set(), set()
        cursor = None
        for _ in range(20):
            params = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await self.rpc("model/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise CodexError("Codex 模型列表格式不兼容，请更新 CLI 后重试")
            for item in result["data"]:
                if not isinstance(item, dict) or item.get("hidden"):
                    continue
                model = item.get("model")
                if not isinstance(model, str) or not model.strip() or len(model) > 200 or model in seen_models:
                    continue
                if "text" not in (item.get("inputModalities") or ["text"]):
                    continue
                name = item.get("displayName")
                models.append({"id": model, "name": name[:200] if isinstance(name, str) and name else model,
                               "is_default": item.get("isDefault") is True})
                seen_models.add(model)
            cursor = result.get("nextCursor")
            if not cursor:
                return models
            if not isinstance(cursor, str) or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        raise CodexError("Codex 模型列表分页异常，请稍后刷新重试")

    async def ensure_thread(self, settings: Settings):
        await self.ensure_process(settings)
        fingerprint = (settings.codex_executable, settings.codex_model, settings.codex_effort, settings.codex_fast, settings.role, settings.background)
        if self.thread_id:
            if fingerprint != self.fingerprint:
                raise CodexError("模型或个人资料已改变，请新建上下文后继续")
            return
        result = await self.rpc("account/read", {"refreshToken": False})
        if not result.get("account") and result.get("requiresOpenaiAuth", True):
            raise CodexError("Codex CLI 尚未登录，请在终端运行 codex login 完成登录")
        folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "InterviewCompanion" / "codex-workspace"
        params = {"cwd": str(folder), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                  "baseInstructions": self.instructions or interview_instructions(settings),
                  "config": {"model_reasoning_effort": settings.codex_effort, "web_search": "disabled"}}
        if settings.codex_model:
            params["model"] = settings.codex_model
        if settings.codex_fast:
            params["serviceTier"] = "priority"
        result = await self.rpc("thread/start", params)
        thread = result.get("thread", {})
        if not thread.get("id") or thread.get("ephemeral") is not True:
            await self.close(reset=True)
            raise CodexError("当前 Codex 版本未创建仅驻留内存的独立会话，已停止接入")
        self.thread_id = thread["id"]
        self.fingerprint = fingerprint
        self.update_context(active=True, lost=False, model=result.get("model") or settings.codex_model or "CLI 默认模型")

    async def interrupt(self):
        if self.turn_done and self.turn_done.done():
            return
        if not self.thread_id or not self.process or self.process.returncode is not None:
            return
        if not self.turn_id:
            # A turn/start acknowledgement was lost: never leave an unknown request running.
            await self.close(reset=False)
            self.update_context(active=False, lost=True, compacting=False)
            return
        try:
            await self.rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=8)
            if self.turn_done:
                await asyncio.wait_for(asyncio.shield(self.turn_done), 8)
        except (CodexError, TimeoutError):
            await self.close(reset=False)
            self.update_context(active=False, lost=True, compacting=False)

    async def answer(self, question: str, settings: Settings, *, image_url=None):
        async with self.active_lock:
            try:
                await self.ensure_thread(settings)
            except asyncio.CancelledError:
                if not self.thread_id:
                    await self.close()
                raise
            self.events = asyncio.Queue(maxsize=512)
            self.turn_done = asyncio.get_running_loop().create_future()
            self.turn_id = None
            streamed = {}
            output_seen = False
            success = False
            try:
                async with asyncio.timeout(settings.codex_timeout_seconds):
                    inputs = [{"type": "text", "text": ("屏幕题目：\n" if image_url else "本轮面试问题（转写或手动输入）：\n") + question,
                               "text_elements": []}]
                    if image_url:
                        inputs.append({"type": "image", "url": image_url})
                    result = await self.rpc("turn/start", {
                        "threadId": self.thread_id, "effort": settings.codex_effort,
                        "serviceTierForTurn": "priority" if settings.codex_fast else "default",
                        "input": inputs,
                    })
                    self.turn_id = result["turn"]["id"]
                    while True:
                        message = await self.events.get()
                        if message.get("local_error"):
                            raise CodexError(message["local_error"])
                        method, params = message.get("method"), message.get("params", {})
                        item = params.get("item", {})
                        if method == "item/agentMessage/delta":
                            text = params.get("delta", "")
                            if text:
                                item_id = params.get("itemId", "")
                                streamed[item_id] = streamed.get(item_id, "") + text
                                output_seen = True
                                yield text
                        elif method == "item/completed" and item.get("type") == "agentMessage":
                            # Some CLI versions emit only a completed item. Avoid duplicating streamed deltas.
                            text = item.get("text", "")
                            previous = streamed.get(item.get("id"), "")
                            if text.startswith(previous) and len(text) > len(previous):
                                output_seen = True
                                yield text[len(previous):]
                        elif method == "item/started" and item.get("type") not in {"userMessage", "agentMessage", "reasoning", "contextCompaction"}:
                            raise CodexError("Codex 尝试执行非文字任务，本轮已停止；请新建上下文后重试")
                        elif method == "error" and not params.get("willRetry"):
                            error = params.get("error", {})
                            raise CodexError(self.turn_error(error))
                        elif method == "turn/completed":
                            turn = params.get("turn", {})
                            if turn.get("id") != self.turn_id:
                                continue
                            if turn.get("status") != "completed":
                                raise CodexError(self.turn_error(turn.get("error") or {}))
                            if not output_seen:
                                raise CodexError("Codex 本轮没有返回文字回答")
                            success = True
                            self.update_context(turns=self.context["turns"] + 1)
                            break
            except TimeoutError as exc:
                raise CodexError("Codex 回答超时，已保留收到的文字；可降低推理强度或检查额度与网络") from exc
            finally:
                if not success:
                    await self.interrupt()
                self.turn_id = None
                self.events = None
                self.turn_done = None

    @staticmethod
    def turn_error(error):
        info = error.get("codexErrorInfo") if isinstance(error, dict) else None
        if info == "usageLimitExceeded":
            return "Codex 使用额度不足，请检查当前账户用量"
        if info == "contextWindowExceeded":
            return "Codex 上下文已满，请压缩上下文或开始新会话"
        return "Codex 未完成回答，请检查登录、账户额度、模型权限和网络；已保留收到的正文"

    async def compact(self):
        if self.active_lock.locked():
            raise CodexError("请等待当前回答完成，再压缩上下文")
        if not self.thread_id or self.context["lost"]:
            raise CodexError("还没有可压缩的 Codex 会话，请先完成一次回答")
        async with self.active_lock:
            self.events = asyncio.Queue(maxsize=512)
            self.turn_done = asyncio.get_running_loop().create_future()
            self.turn_id = None
            self.update_context(compacting=True)
            success = False
            try:
                await self.rpc("thread/compact/start", {"threadId": self.thread_id})
                async with asyncio.timeout(120):
                    while True:
                        message = await self.events.get()
                        if message.get("local_error"):
                            raise CodexError(message["local_error"])
                        params = message.get("params", {})
                        if message.get("method") == "turn/started":
                            self.turn_id = params.get("turn", {}).get("id")
                        if message.get("method") == "turn/completed":
                            if params.get("turn", {}).get("status") != "completed":
                                raise CodexError("上下文压缩未完成，可稍后重试")
                            success = True
                            return
            except TimeoutError as exc:
                raise CodexError("上下文压缩超时，请检查网络和账户额度") from exc
            finally:
                if not success:
                    await self.interrupt()
                self.update_context(compacting=False)
                self.events = self.turn_done = self.turn_id = None

    async def close(self, *, reset=True):
        self.closing = True
        process = self.process
        if process and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        for task in [self.reader_task, self.stderr_task]:
            if task and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.process = self.reader_task = self.stderr_task = None
        self.thread_id = self.turn_id = self.fingerprint = None
        if reset:
            self.context = self.empty_context()
            self.on_context(dict(self.context))
