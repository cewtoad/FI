"""One-click launcher for the packaged build.

Double-clicking the exe (or running ``python FI.py``) asks how to run:

    1) 网页模式  - opens the browser panel (recommended for first run)
    2) 语音模式  - Raw Input push-to-talk, no browser (needs stt deps)

Non-interactive launches can skip the prompt:
    FI.py --web        # straight to the web panel
    FI.py --voice      # straight to voice mode

This is the entry point PyInstaller bundles. It is intentionally a thin shell
around webui.serve() / voice_main.main().
"""

from __future__ import annotations

import argparse
import socket
import sys


def _force_utf8_stdout() -> None:
    """Make stdout/stderr UTF-8 so Chinese/emoji never crash on a GBK console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


_force_utf8_stdout()
import webbrowser


def _port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """True when something is already listening on ``port`` (e.g. SimHub)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _web_panel_running(web_port: int) -> bool:
    """True when our own web panel already answers on ``web_port``."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{web_port}/api/state", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _who_holds_udp_port(port: int) -> str:
    """Best-effort: name the process holding a UDP port (Windows netstat)."""
    try:
        import subprocess
        out = subprocess.run(["netstat", "-ano", "-p", "udp"],
                             capture_output=True, timeout=8)
        text = out.stdout.decode("utf-8", "replace")
        owners = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].upper() == "UDP" and parts[1].endswith(f":{port}"):
                pid = parts[-1]
                name = pid
                try:
                    q = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                        capture_output=True, timeout=8)
                    csv = q.stdout.decode("utf-8", "replace").strip()
                    if csv and csv.startswith('"'):
                        name = csv.split('","')[0].strip('"')
                    owners.append(f"{name}(PID {pid})")
                except Exception:
                    owners.append(f"PID {pid}")
        return ", ".join(sorted(set(owners)))
    except Exception:  # noqa: BLE001
        return ""


def _require_port_free(udp_port: int, force: bool) -> bool:
    """Refuse to start when another receiver already holds the UDP port.

    The F1 UDP stream can only be received by ONE process, so running web mode
    and voice mode at the same time makes them fight over the socket and the
    telemetry becomes intermittent/wrong. Returns True when it is safe to
    proceed.
    """
    if force or not _port_busy(udp_port):
        print(f">>> 遥测接收端口: UDP {udp_port}")
        return True
    who = _who_holds_udp_port(udp_port)
    print("=" * 52)
    print(f"[!] UDP 端口 {udp_port} 已被占用。")
    print("    F1 的遥测流同一时间只能被一个进程接收。")
    if who:
        print(f"    占用者: {who}")
    print("    常见原因：")
    print("      · 你已经在跑另一个模式（网页/语音只能开一个）；")
    print("      · SimHub / CrewChief 等遥测工具；")
    print("      · 方向盘/外设的厂家软件（FanaLab / Moza Pit House /")
    print("        Simagic / Thrustmaster 等）开了『屏幕遥测』功能。")
    print()
    print("    ── 想同时用（如 SimHub 做仪表 + 本程序做工程师）──")
    print("    F1 UDP 只能一个进程直接收。做法是让占用者『转发』到另一端口：")
    print("      · SimHub: Game → 打开 UDP Forward，把遥测转发到 20778；")
    print("      · 本程序指定那个端口启动：")
    print("          FI.py --port 20778 --voice   （或 --web）")
    print("      · 网页/语音模式同样加 --port 20778。")
    print()
    print("    或先关闭占用者，再启动本程序（加 --force 可强开但会互相抢包）。")
    print("=" * 52)
    return False


def _open_browser_later(url: str) -> None:
    import threading
    import time

    def _go():
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def _run_web(port: int, web_port: int, open_browser: bool,
             force: bool = False) -> None:
    from webui import serve
    if not _require_port_free(port, force):
        raise SystemExit(2)
    if open_browser:
        _open_browser_later(f"http://127.0.0.1:{web_port}")
    serve(port=port, web_port=web_port)


def _voice_deps_present() -> bool:
    """True when the local STT stack (faster-whisper + sounddevice) is available.

    The light core pack deliberately ships without it, so voice mode there can
    only work via cloud STT. This check lets us say so up front instead of
    letting the user hit a silent 'STT 不可用'.
    """
    try:
        import sys as _sys
        from pathlib import Path
        from paths import app_root
        lib = app_root() / "stt_lib"
        if lib.exists() and str(lib) not in _sys.path:
            _sys.path.insert(0, str(lib))
        import faster_whisper  # noqa: F401
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False


def _run_voice(port: int, argv: list, force: bool = False) -> None:
    import voice_main
    if not _require_port_free(port, force):
        raise SystemExit(2)
    if not _voice_deps_present():
        print("⚠ 未检测到本地语音依赖 (faster-whisper / sounddevice)。")
        print("   本程序仍会启动，但语音识别需改用云端：在 .env 设")
        print("       STT_PROVIDER=cloud")
        print("       STT_BASE_URL=...  STT_API_KEY=...  STT_MODEL=whisper-1")
        print("   若要完全离线的本地语音，请下载【全量语音包】。\n")
    sys.argv = ["voice_main.py", *argv]
    voice_main.main()


def _ask_mode(port: int) -> str:
    print("=" * 46)
    print("  F1 Race Engineer")
    print("=" * 46)
    print("  1) 网页模式  （推荐：浏览器面板 + 文字/语音问答）")
    print("  2) 语音模式  （全屏游戏内按键说话，需要本地语音依赖）")
    print("  3) 设置      （在浏览器里修改配置，独立进程）")
    print("  q) 退出")
    try:
        choice = input("\n请选择 [1]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"
    if choice in ("", "1", "web", "w"):
        return "web"
    if choice in ("2", "voice", "v"):
        return "voice"
    if choice in ("3", "config", "c", "设置"):
        return "config"
    return "q"


def _ensure_env_file() -> None:
    """Create a .env next to the exe on first run so there is something to edit."""
    from paths import app_root
    root = app_root()
    env = root / ".env"
    example = root / ".env.example"
    if not env.exists() and example.exists():
        try:
            env.write_text(example.read_text(encoding="utf-8-sig"), encoding="utf-8")
            print(f">>> 已生成配置文件：{env}（可填入你的 API key）")
        except OSError:
            pass


def _writable_check() -> bool:
    """T10a: warn loudly when app_root() is read-only (e.g. Program Files)."""
    from config import get_config
    try:
        writable = get_config().is_writable()
    except Exception:
        writable = False
    if not writable:
        print("⚠ 当前目录不可写（无法保存配置/会话记录）。")
        print("  请把程序解压到桌面或 D 盘等可写位置后再运行。\n")
    return writable


def _run_selftest() -> None:
    """T10a: run the whole-app self check (merged tool_selftest)."""
    import tool_selftest
    tool_selftest.main()


def _run_config() -> None:
    """T7: launch the standalone config page."""
    import config_ui
    config_ui.serve()


def main() -> None:
    _ensure_env_file()
    p = argparse.ArgumentParser(description="F1 Race Engineer launcher")
    p.add_argument("--web", action="store_true", help="直接进入网页模式")
    p.add_argument("--voice", action="store_true", help="直接进入语音模式")
    p.add_argument("--config", action="store_true", help="打开设置页（独立进程）")
    p.add_argument("--selftest", action="store_true", help="整体自检后退出")
    p.add_argument("--port", type=int, default=20777, help="游戏 UDP 端口")
    p.add_argument("--web-port", type=int, default=8765, help="网页面板端口")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--force", action="store_true",
                   help="UDP 端口被占用时仍强行启动（两个实例会互相抢包）")
    args, rest = p.parse_known_args()

    if args.selftest:
        _run_selftest()
        return
    if args.config:
        _run_config()
        return
    if args.web:
        _run_web(args.port, args.web_port, not args.no_browser, args.force)
        return
    if args.voice:
        _run_voice(args.port, rest, args.force)
        return

    _writable_check()
    mode = _ask_mode(args.port)
    if mode == "web":
        _run_web(args.port, args.web_port, not args.no_browser, args.force)
    elif mode == "voice":
        _run_voice(args.port, rest, args.force)
    elif mode == "config":
        _run_config()


if __name__ == "__main__":
    main()
