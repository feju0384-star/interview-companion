"""Read-only onboarding checks; never open a capture stream or call a model."""
import asyncio
import sys

from .audio import list_devices, list_microphones
from .codex_backend import find_codex
from .claude_backend import find_claude, local_configuration


async def diagnose(settings, addresses, port, controller_count):
    checks = []

    def add(name, status, detail, action=""):
        checks.append(dict(name=name, status=status, detail=detail, action=action))

    add("电脑后台", "ok", f"服务已响应 · Python {sys.version_info.major}.{sys.version_info.minor} · 端口 {port}")
    devices, microphones = await asyncio.gather(asyncio.to_thread(list_devices), asyncio.to_thread(list_microphones), return_exceptions=True)
    for label, result, advice in (("系统声音", devices, "连接面试使用的耳机或扬声器，在手机的监听设置中刷新设备。"),
                                  ("电脑麦克风", microphones, "连接麦克风，并在 Windows 设置中允许桌面应用访问麦克风。")):
        if isinstance(result, BaseException):
            add(label, "warning", "设备枚举未完成，尚未验证采集。", advice)
        elif not result:
            add(label, "warning", "未找到可用设备。", advice)
        else:
            preferred = next((device for device in result if device.get("default")), result[0])
            add(label, "ok", f"发现 {len(result)} 个设备 · {preferred['name']}。尚未开启采集，设备权限需开始录音时验证。")
    if settings.answer_backend == "api":
        ready = bool(settings.llm_model and settings.llm_api_key)
        add("回答与复盘模型", "ok" if ready else "warning", "模型名称和密钥已填写，尚未验证联网和额度。" if ready else "还没有完整填写模型名称和密钥。", "在手机设置中配置兼容 API，再手动提问验证。")
    else:
        try:
            if settings.answer_backend == "codex":
                await asyncio.to_thread(find_codex, settings.codex_executable)
                detail = "已找到 Codex，登录与额度尚未验证。"
                ready = True
            else:
                await asyncio.to_thread(find_claude, settings.claude_executable)
                _, config = await asyncio.to_thread(local_configuration, settings)
                ready = config["configured"]
                detail = "已找到 Claude Code；" + ("已检测到凭据，尚未验证联网。" if ready else "未检测到凭据。")
            add("回答与复盘模型", "ok" if ready else "warning", detail, "在手机设置中检查本机模型配置，再手动提问验证。")
        except (RuntimeError, OSError):
            add("回答与复盘模型", "warning", "未找到所选模型的可用本机配置。", "安装并配置所选 CLI，或在手机设置中改用兼容 API。")
    asr_ready = bool(settings.asr_model and settings.asr_api_key)
    add("语音识别", "ok" if asr_ready else "warning", "识别配置已填写，尚未验证密钥和服务连通性。" if asr_ready else "语音识别密钥或模型未填写；暂时可手动输入回答。", "在手机设置中填写识别服务，再录一小段话验证。")
    add("局域网地址", "ok" if addresses else "warning", "、".join(f"{address}:{port}" for address in addresses) if addresses else "未发现可用的局域网 IPv4 地址。", "电脑和手机连接同一个非访客 Wi-Fi；有多个地址时逐个尝试二维码。")
    add("控制台连接", "ok" if controller_count else "warning", f"当前有 {controller_count} 个控制台连接（包含本机预览）。" if controller_count else "尚无控制台连接。电脑自检不能证明手机可达。", "手机扫码验证。若本机预览正常、手机打不开，运行项目中的“修复手机连接.cmd”，然后检查 Wi-Fi 客户端隔离。")
    return {"checks": checks, "ready": all(check["status"] == "ok" for check in checks),
            "note": "这是只读配置与设备检查，不会打开麦克风、截图或请求模型。防火墙与手机链路需由手机实际连接验证。"}
