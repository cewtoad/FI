"""Quick TTS smoke test: speak a Chinese sentence on the G733."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from voice_tts import LocalTTS

t = LocalTTS(output_device="G733")
print("available:", t.available)
print("engine:", getattr(t.engine, "name", "?"))
t.speak("测试一下，我是你的赛车工程师。圈速比上一圈快零点三秒，继续保持。")
print("done. error:", t.last_error)
