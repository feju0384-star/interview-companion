import asyncio
import json

from backend.codex_backend import CodexBackend
from backend.config import Settings


async def main():
    backend = CodexBackend()
    try:
        info = await backend.probe(Settings(answer_backend="codex"))
        print(json.dumps(info, ensure_ascii=False))
        if not info.get("logged_in"):
            raise SystemExit(1)
        data = await backend.rpc("config/read", {"includeLayers": False})
        config = data.get("config", {})
        print(json.dumps({"model": config.get("model"), "enabled_mcp_names": [name for name, value in (config.get("mcp_servers") or {}).items() if value.get("enabled", True)],
                          "shell_tool": (config.get("features") or {}).get("shell_tool"),
                          "web_search": config.get("web_search"), "sandbox_mode": config.get("sandbox_mode")}, ensure_ascii=False))
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
