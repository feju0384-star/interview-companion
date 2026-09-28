"""Text-only local Claude Code calls, with credentials kept in the CLI's config."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from .config import Settings
from .prompts import interview_instructions, question_with_context


class ClaudeError(RuntimeError):
    pass


def find_claude(configured_path="") -> list[str]:
    candidates = [configured_path] if configured_path else [
        shutil.which("claude.exe" if os.name == "nt" else "claude"),
        str(Path.home() / ".local" / "bin" / "claude.exe"),
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"),
        shutil.which("claude.cmd"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        path = Path(candidate).resolve()
        if os.name != "nt" or path.suffix.lower() == ".exe":
            return [str(path)]
        # Resolve the standard npm layout without executing a shell wrapper.
        package = path.parent / "node_modules" / "@anthropic-ai" / "claude-code"
        native = package / "bin" / "claude.exe"
        if native.is_file():
            return [str(native)]
        script = package / "cli.js"
        node = shutil.which("node.exe")
        if script.is_file() and node:
            return [node, str(script)]
    raise ClaudeError("未找到 Claude Code，请先在电脑安装；支持原生 claude.exe 和标准 npm 安装")


def local_configuration(settings: Settings):
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    path = folder / "settings.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        configured_env = config.get("env", {})
        if not isinstance(configured_env, dict):
            raise ValueError()
    except (OSError, ValueError, AttributeError) as exc:
        raise ClaudeError("无法读取本机 Claude Code 设置，请检查 .claude/settings.json") from exc
    env = dict(os.environ)
    # Only import provider credentials and model settings, never hooks or commands.
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        if isinstance(configured_env.get(key), str):
            env[key] = configured_env[key]
    endpoint = env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    parsed = urlsplit(endpoint)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ClaudeError("Claude Code 的服务地址无效，云端接口必须使用 HTTPS")
    # Claude Code uses the Anthropic endpoint, not the OpenAI-compatible root.
    if endpoint == "https://api.deepseek.com":
        endpoint += "/anthropic"
    env["ANTHROPIC_BASE_URL"] = endpoint
    model = settings.claude_model or env.get("ANTHROPIC_MODEL") or config.get("model") or ""
    if not isinstance(model, str):
        raise ClaudeError("Claude Code 的模型配置无效")
    if model:
        env["ANTHROPIC_MODEL"] = model
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_USE_OPENAI"):
        env.pop(key, None)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(settings.max_tokens)
    if settings.claude_direct:
        for key in list(env):
            if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
                env.pop(key)
        env["NO_PROXY"] = "*"
    configured = bool(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"))
    return env, {"model": model, "base_url": endpoint, "configured": configured, "direct": settings.claude_direct}


class ClaudeBackend:
    def __init__(self, command_factory=None, instructions=None):
        self.command_factory = command_factory
        self.instructions = instructions
        self.process = None
        self.lock = asyncio.Lock()

    def command(self, settings):
        return self.command_factory(settings) if self.command_factory else find_claude(settings.claude_executable)

    async def probe(self, settings):
        env, info = local_configuration(settings)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(settings), "--version", env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            output, _ = await asyncio.wait_for(process.communicate(), 10)
            if process.returncode:
                raise ClaudeError("Claude Code 无法启动，请先在电脑确认 claude --version 能运行")
            return {**info, "installed": True, "version": output.decode("utf-8", errors="replace").strip()[:100]}
        except (OSError, TimeoutError) as exc:
            raise ClaudeError("Claude Code 检查失败，请检查本机安装") from exc
        finally:
            await self.terminate(process)

    @staticmethod
    async def terminate(process):
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def close(self):
        await self.terminate(self.process)

    async def answer(self, question: str, settings: Settings, history: list[dict]):
        async with self.lock:
            env, info = local_configuration(settings)
            if not info["configured"] and not self.command_factory:
                raise ClaudeError("请先在电脑的 Claude Code 中配置 API Key 或认证令牌；听答不会向手机返回该密钥")
            root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "InterviewCompanion" / "claude-workspace"
            root.mkdir(parents=True, exist_ok=True)
            # A dedicated short-lived folder avoids loading project configuration.
            with tempfile.TemporaryDirectory(prefix="answer-", dir=root) as folder:
                prompt_file = Path(folder) / "system.txt"
                prompt_file.write_text(self.instructions or interview_instructions(settings), encoding="utf-8")
                args = self.command(settings) + [
                    "--print", "--bare", "--output-format", "stream-json", "--verbose",
                    "--include-partial-messages", "--no-session-persistence", "--tools", "",
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                    "--disable-slash-commands", "--no-chrome", "--permission-mode", "dontAsk",
                    "--system-prompt-file", str(prompt_file),
                ]
                if info["model"]:
                    args.extend(["--model", info["model"]])
                recent = [{"question": item["question"], "answer": item["answer"]}
                          for item in history[-4:] if item["status"] == "done" and not item.get("demo")]
                prompt = ("历史拟答稿（仅作上下文，不代表本人实际说过）：\n" + json.dumps(recent, ensure_ascii=False)
                          + "\n当前问题：\n" + question_with_context(question, history))
                if self.instructions:
                    prompt = question
                process = None
                text_seen = False
                result_seen = False
                message_streamed = False
                try:
                    async with asyncio.timeout(settings.claude_timeout_seconds):
                        process = await asyncio.create_subprocess_exec(
                            *args, cwd=folder, env=env, stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                            limit=4 * 1024 * 1024,
                        )
                        self.process = process
                        process.stdin.write(prompt.encode("utf-8"))
                        await process.stdin.drain()
                        process.stdin.close()
                        while line := await process.stdout.readline():
                            message = json.loads(line)
                            kind = message.get("type")
                            if kind == "stream_event":
                                event = message.get("event", {})
                                if event.get("type") == "message_start":
                                    message_streamed = False
                                if event.get("type") == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use":
                                    raise ClaudeError("Claude Code 请求了工具操作，本应用只接受文字回答")
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta" and delta.get("text"):
                                    text_seen = message_streamed = True
                                    yield delta["text"]
                            elif kind == "assistant":
                                if message.get("error"):
                                    raise ClaudeError("Claude Code 返回服务错误，请检查本机密钥、余额、模型与网络")
                                blocks = message.get("message", {}).get("content", [])
                                if any(block.get("type") == "tool_use" for block in blocks):
                                    raise ClaudeError("Claude Code 请求了工具操作，本应用只接受文字回答")
                                if not message_streamed:
                                    for block in blocks:
                                        if block.get("type") == "text" and block.get("text"):
                                            text_seen = True
                                            yield block["text"]
                                message_streamed = False
                            elif kind == "result":
                                if message.get("is_error") or message.get("subtype") != "success":
                                    raise ClaudeError("Claude Code 未完成回答，请检查本机密钥、余额、模型与网络")
                                if not text_seen and message.get("result"):
                                    text_seen = True
                                    yield message["result"]
                                result_seen = True
                        await process.wait()
                        if process.returncode or not result_seen:
                            raise ClaudeError("Claude Code 连接提前中断或启动失败，请检查 CLI 版本、模型和网络")
                        if not text_seen:
                            raise ClaudeError("Claude Code 未返回回答正文")
                except TimeoutError as exc:
                    raise ClaudeError("Claude Code 回答超时，已保留收到的内容") from exc
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    raise ClaudeError("Claude Code 无法启动或返回格式不兼容，请检查本机 CLI 安装") from exc
                finally:
                    await self.terminate(process)
                    self.process = None
