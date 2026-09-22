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
import webbrowser


def _port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """True when something is already listening on ``port`` (e.g. SimHub)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _preflight(udp_port: int) -> None:
    if _port_busy(udp_port):
        print(f"⚠ 端口 {udp_port} 已被占用 —— 很可能是 SimHub / CrewChief 等"
              f"也在收遥测。F1 的 UDP 流只能被一个程序接收，")
        print("  请先关闭其他遥测工具，游戏数据才能被本程序收到。\n")


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


def _run_web(port: int, web_port: int, open_browser: bool) -> None:
    from webui import serve
    _preflight(port)
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


def _run_voice(port: int, argv: list) -> None:
    import voice_main
    _preflight(port)
    if not _voice_deps_present():
        print("⚠ 未检测到本地语音依赖 (faster-whisper / sounddevice)。")
        print("   本程序仍会启动，但语音识别需改用云端：在 .env 设")
        print("       STT_PROVIDER=cloud")
        print("       STT_BASE_URL=...  STT_API_KEY=...  STT_MODEL=whisper-1")
        print("   若要完全离线的本地语音，请下载【全量语音包】。\n")
    sys.argv = ["voice_main.py", *argv]
    voice_main.main()


def _ask_mode(port: int) -> str:
    _preflight(port)
    print("=" * 46)
    print("  F1 Race Engineer")
    print("=" * 46)
    print("  1) 网页模式  （推荐：浏览器面板 + 文字/语音问答）")
    print("  2) 语音模式  （全屏游戏内按键说话，需要本地语音依赖）")
    print("  q) 退出")
    try:
        choice = input("\n请选择 [1]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"
    if choice in ("", "1", "web", "w"):
        return "web"
    if choice in ("2", "voice", "v"):
        return "voice"
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


def main() -> None:
    _ensure_env_file()
    p = argparse.ArgumentParser(description="F1 Race Engineer launcher")
    p.add_argument("--web", action="store_true", help="直接进入网页模式")
    p.add_argument("--voice", action="store_true", help="直接进入语音模式")
    p.add_argument("--port", type=int, default=20777, help="游戏 UDP 端口")
    p.add_argument("--web-port", type=int, default=8765, help="网页面板端口")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args, rest = p.parse_known_args()

    if args.web:
        _run_web(args.port, args.web_port, not args.no_browser)
        return
    if args.voice:
        _run_voice(args.port, rest)
        return

    mode = _ask_mode(args.port)
    if mode == "web":
        _run_web(args.port, args.web_port, not args.no_browser)
    elif mode == "voice":
        _run_voice(args.port, rest)


if __name__ == "__main__":
    main()
