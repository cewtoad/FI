"""Audio device helper for 测试设备.bat (and manual use).

Usage:
    py -3.12 tool_audio.py show          # current resolved input/output + mode
    py -3.12 tool_audio.py list          # all input/output devices
    py -3.12 tool_audio.py pin IN OUT    # pin by name fragment ('' = leave as-is)
    py -3.12 tool_audio.py unpin         # go back to following the system default
    py -3.12 tool_audio.py speak         # speak a test phrase on the active output
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio  # noqa: E402


def _mode(source: str) -> str:
    return {"pinned": "已指定", "default": "跟随系统当前设备", "none": "无可用设备"}.get(
        source, source)


def cmd_show() -> None:
    d = audio.describe()
    for kind, label in (("input", "麦克风"), ("output", "播放")):
        v = d["active"][kind]
        print(f"{label}: id={v['id']}  名称={v['name']}  模式={_mode(str(v['source']))}"
              + (f"  ({v['note']})" if v.get("note") else ""))
    pins = {k: v for k, v in d["configured"].items()}
    print(f"固定配置(.env): {pins if any(pins.values()) else '（无，全部跟随系统）'}")


def cmd_list() -> None:
    for kind, label in (("input", "输入(麦克风)"), ("output", "输出(播放)")):
        print(f"--- {label} ---")
        for x in audio.list_devices(kind):
            mark = " (默认)" if x["default"] else ""
            print(f"  [{x['id']}] {x['name']} ({x['channels']}ch){mark}")


def cmd_pin(argv: list) -> None:
    inp = argv[0].strip() if len(argv) > 0 else ""
    outp = argv[1].strip() if len(argv) > 1 else ""
    if inp:
        audio.set_device("input", inp)
    if outp:
        audio.set_device("output", outp)
    print("已更新固定设备:", audio.current())


def cmd_unpin() -> None:
    audio.set_device("input", "")
    audio.set_device("output", "")
    print("已解除固定，回到【跟随系统当前设备】")


def cmd_speak() -> None:
    from voice_tts import LocalTTS
    t = LocalTTS()
    print("播放测试语音…")
    t.speak("音频设备测试，一二三四五")
    print("last_error:", t.last_error)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        cmd_show()
    elif args[0] == "show":
        cmd_show()
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "pin":
        cmd_pin(args[1:])
    elif args[0] == "unpin":
        cmd_unpin()
    elif args[0] == "speak":
        cmd_speak()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
