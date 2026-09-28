"""Create the environment once; run the service without a visible console on Windows."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("INTERVIEW_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
# Local management must not go through a user's HTTP proxy.
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def running():
    try:
        with HTTP.open(URL + "/health", timeout=1) as response:
            return json.load(response).get("app") == "interview-companion"
    except (OSError, ValueError):
        return False


def python_works(python: Path) -> bool:
    """A copied virtual environment may exist but reference a missing base Python."""
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", "pass"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_environment() -> Path:
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = ROOT / ".venv" / ".requirements-installed"
    if not python_works(python):
        print("正在创建或修复 Python 虚拟环境……", flush=True)
        # Clear the cache before repair so a failed install is retried next time.
        marker.unlink(missing_ok=True)
        # Recreate interpreter/configuration in place, preserving installed packages.
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
        if not python_works(python):
            raise RuntimeError("虚拟环境修复失败，请使用本机 Python 3.12 运行 launcher.py")
    requirements = (ROOT / "requirements.txt").read_bytes()
    if not marker.exists() or marker.read_bytes() != requirements:
        print("正在安装依赖；首次使用需要联网，失败后可重新双击启动脚本重试……", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--retries", "2", "--timeout", "30", "-r", str(ROOT / "requirements.txt")], check=True)
        marker.write_bytes(requirements)
    return python


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true", help="Do not open the control page")
    parser.add_argument("--stop", action="store_true", help="Stop capture and terminate the service")
    args = parser.parse_args()
    if args.stop:
        if running():
            req = urllib.request.Request(URL + "/api/shutdown", data=b"{}", headers={"Content-Type": "application/json"})
            HTTP.open(req, timeout=10).close()
        return
    if not running():
        python = ensure_environment()
        log_folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "InterviewCompanion"
        log_folder.mkdir(parents=True, exist_ok=True)
        log_path = log_folder / "service.log"
        # CREATE_NO_WINDOW hides the console while keeping errors in service.log.
        executable = python
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with log_path.open("w", encoding="utf-8") as log:
            child = subprocess.Popen([str(executable), str(ROOT / "run.py")], cwd=ROOT, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, creationflags=flags)
        for _ in range(80):
            if running():
                break
            if child.poll() is not None:
                raise RuntimeError(f"后台启动失败，请查看 {log_path}；端口 {PORT} 可能被占用")
            time.sleep(0.15)
        else:
            raise RuntimeError(f"后台启动超时，请查看 {log_path}")
    if not args.background:
        webbrowser.open(URL)


if __name__ == "__main__":
    main()
