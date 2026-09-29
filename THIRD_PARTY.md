# 第三方组件

本项目自身的代码与文档采用根目录的 MIT 协议，版权所有者为刘顺洋。依赖、外部 CLI 与模型服务适用各自的许可证或服务条款；项目 MIT 协议不会替代它们的条款。

源码包不包含 Python、虚拟环境、第三方库、Codex 或 Claude Code 可执行文件。首次安装通过 `requirements.txt` 下载依赖；可用版本记录在 `requirements.lock`。后续如果发布包含这些组件的安装包，应随包保留其许可证与要求的声明，包括传递依赖。

直接依赖的上游项目：

- [FastAPI](https://github.com/fastapi/fastapi)
- [Uvicorn](https://github.com/encode/uvicorn)
- [HTTPX](https://github.com/encode/httpx)
- [NumPy](https://github.com/numpy/numpy)
- [PyAudioWPatch 0.2.12.8](https://github.com/s0d3s/PyAudioWPatch/tree/v0.2.12.8)：此固定版本的 [LICENSE.txt](https://github.com/s0d3s/PyAudioWPatch/blob/v0.2.12.8/LICENSE.txt) 为 Apache-2.0。不要用 PyAudio 原项目或其他版本的许可证替代它。
- [python-qrcode](https://github.com/lincolnloop/python-qrcode)
- [Pillow](https://github.com/python-pillow/Pillow)

页面使用本机系统字体，没有打包商业字体、远程字体或第三方图片。模型和语音识别账户由使用者自行准备，相关调用费用由使用者向对应服务商支付。
