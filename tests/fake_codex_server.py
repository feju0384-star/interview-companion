"""A deterministic stdio protocol fixture. Never invokes a model or external tools."""
import json
import sys


count = 0
thread_id = ""
turn_id = ""
memory = ""
service_tier = "default"


def send(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


def reply(request, result):
    send({"id": request["id"], "result": result})


def event(method, **params):
    send({"method": method, "params": {"threadId": thread_id, **params}})


for line in sys.stdin:
    request = json.loads(line)
    method, params = request.get("method"), request.get("params", {})
    if "id" not in request:
        continue
    if method == "initialize": reply(request, {"userAgent": "fake"})
    elif method == "config/read": reply(request, {"config": {"mcp_servers": {}}})
    elif method == "account/read": reply(request, {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True})
    elif method == "model/list":
        assert params["includeHidden"] is False
        first = {"id": "catalog-entry-a", "model": "test-gpt-a", "displayName": "Test GPT A", "isDefault": True}
        if params.get("cursor") == "second-page":
            reply(request, {"data": [first, {"model": "test-gpt-b", "displayName": "Test GPT B"}], "nextCursor": None})
        else:
            reply(request, {"data": [first, {"model": "hidden-model", "hidden": True},
                                      {"model": "audio-only", "inputModalities": ["audio"]}], "nextCursor": "second-page"})
    elif method == "thread/start":
        service_tier = params.get("serviceTier", "default")
        assert params["ephemeral"] and params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
        assert "历史输出都是拟答稿" in params["baseInstructions"] or "屏幕题目的解题助手" in params["baseInstructions"]
        thread_id = "isolated-fake-thread"
        reply(request, {"thread": {"id": thread_id, "ephemeral": True}, "model": "fake-model"})
    elif method == "turn/start":
        assert params["threadId"] == thread_id
        assert params["serviceTierForTurn"] == service_tier
        count += 1; turn_id = f"turn-{count}"
        text = params["input"][0]["text"]
        if "REMEMBER:" in text: memory = text.split("REMEMBER:", 1)[1]
        event("turn/started", turn={"id": turn_id, "status": "inProgress"})
        reply(request, {"turn": {"id": turn_id, "status": "inProgress"}})
        if "WAIT" in text:
            event("item/agentMessage/delta", turnId=turn_id, itemId="answer", delta="部分")
            continue
        if "CRASH" in text:
            sys.exit(2)
        if "TOOL" in text:
            event("item/started", turnId=turn_id, item={"id":"tool", "type":"commandExecution"})
            continue
        result = memory if "RECALL" in text else "测试回答"
        if len(params["input"]) > 1:
            assert params["input"][1]["type"] == "image"
            assert params["input"][1]["url"].startswith("data:image/png;base64,")
            result = "图片答案：B，解析示例。"
        for part in [result[:2], result[2:]]:
            event("item/agentMessage/delta", turnId=turn_id, itemId="answer", delta=part)
        event("item/completed", turnId=turn_id, item={"id":"answer", "type":"agentMessage", "text":result})
        event("thread/tokenUsage/updated", turnId=turn_id, tokenUsage={"last":{"totalTokens":100+count},"modelContextWindow":1000})
        event("turn/completed", turn={"id":turn_id,"status":"completed"})
    elif method == "turn/interrupt":
        assert params["turnId"] == turn_id
        reply(request, {})
        event("turn/completed", turn={"id":turn_id,"status":"interrupted"})
    elif method == "thread/compact/start":
        turn_id = "compact-turn"
        reply(request, {})
        event("turn/started", turn={"id":turn_id,"status":"inProgress"})
        event("item/started", turnId=turn_id, item={"id":"compact","type":"contextCompaction"})
        event("item/completed", turnId=turn_id, item={"id":"compact","type":"contextCompaction"})
        event("turn/completed", turn={"id":turn_id,"status":"completed"})
    else:
        send({"id":request["id"],"error":{"code":-32601,"message":"unknown method"}})
