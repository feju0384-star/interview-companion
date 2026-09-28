"""A text-only Claude Code subprocess fixture; never connects to a provider."""
import json
from pathlib import Path
import sys
import time

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def send(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


if "--version" in sys.argv:
    print("2.1.test (Claude Code)")
    raise SystemExit()

args = sys.argv
assert args[args.index("--tools") + 1] == ""
assert args[args.index("--setting-sources") + 1] == ""
assert "--bare" in args and "--strict-mcp-config" in args and "--no-session-persistence" in args
system = Path(args[args.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
assert "历史输出都是拟答稿" in system
prompt = sys.stdin.read()
if "ECHO_INPUT" in prompt:
    send({"type": "result", "subtype": "success", "result": json.dumps({"system": system, "prompt": prompt}, ensure_ascii=False)})
    raise SystemExit()
if "ERROR" in prompt:
    send({"type": "assistant", "error": "authentication_failed", "message": {"content": [{"type": "text", "text": "secret-provider-body"}]}})
    raise SystemExit()
if "TOOL" in prompt:
    send({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use"}}})
    raise SystemExit()
if "MALFORMED" in prompt:
    print("not-json-secret", flush=True)
    raise SystemExit()
send({"type": "stream_event", "event": {"type": "message_start"}})
send({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "测试"}}})
if "WAIT" in prompt:
    time.sleep(60)
if "TRUNCATE" in prompt:
    raise SystemExit()
send({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "回答"}}})
send({"type": "assistant", "message": {"content": [{"type": "text", "text": "测试回答"}]}})
send({"type": "result", "subtype": "success", "is_error": False, "result": "测试回答"})
