# R6 设计方案：语音链路（按键说话 → STT → engineer 问答 → TTS 播报）

> 状态：**仅设计方案，未写任何代码**。请审阅后圈定实现范围。
> 需求：按键触发 → 录音 → STT → 复用 `engineer.py` 问答 → TTS 播报；
> 与现有架构解耦，STT/TTS 为可替换的独立模块。
> 对应 DESIGN.md"规划中"的 STT/TTS 两项。

## 0. 核心设计决策（一句话版）

**录音、按键、播报全部放在浏览器里做，Python 侧只加一个"音频进、音频出"的 HTTP 端点；
STT/TTS 做成与 `ai_client.py` 同构的可替换客户端，全部为可选依赖，未安装则优雅降级。**

这样做的根本理由：本项目设计原则是"被动接收、不注入、不挂钩"。原生全局键盘钩子
（`keyboard`/`pynput`）在带反作弊的游戏上属于灰色地带，且与全屏游戏抢焦点；
而 webui 已经存在，浏览器有现成的 `getUserMedia` + `MediaRecorder` + `Audio`，
零新增原生依赖就能完成 录音/按键/播放 三件事。

## 1. 模块划分与文件命名（遵守现有平铺命名风格）

| 新文件 | 职责 | 依赖 |
|---|---|---|
| `stt_client.py` | `STTEngine` 基类 + `CloudSTT`（OpenAI 兼容 `/audio/transcriptions`，stdlib urllib multipart，风格对齐 `ai_client.py`）+ `LocalWhisperSTT`（faster-whisper，**可选导入**，未安装则该类不可用） | 标准库 / 可选 faster-whisper |
| `tts_client.py` | `TTSEngine` 基类 + `EdgeTTS`（可选导入）+ `SapiTTS`（Windows 内置语音，ctypes 调 COM `SpVoice`，零依赖、离线）+ `NullTTS`（直接降级为只显示文字） | 可选 edge-tts |
| `voice.py` | 编排器 `VoiceLink`：`process(audio_bytes) -> {question, answer, audio}`；内部就是 `stt.transcribe → engineer.ask(snapshot) → tts.synthesize` 三步串联 | 上面两个 + 现有 engineer |

- 接口形态（描述，非实现）：`STTEngine.transcribe(audio: bytes, mime: str) -> str`；
  `TTSEngine.synthesize(text: str) -> bytes(mp3/wav)`。两个基类各约 10 行。
- 配置进 `.env`（沿用现有惯例）：`STT_PROVIDER=cloud|local|off`、`STT_BASE_URL/STT_API_KEY/STT_MODEL`、
  `TTS_PROVIDER=edge|sapi|off`、`TTS_VOICE=zh-CN-XiaoxiaoNeural`。
- `webui.py` 是唯一集成点：新增 `POST /api/ask_voice`；`run.py` 无需改动
  （语音只属于 web 模式；终端模式语音不在 v1 范围）。
- `engineer.py` **零改动**——语音只是 ask() 的另一个外壳，这满足"与现有架构解耦"。

## 2. STT 选型：云端 API vs 本地 faster-whisper

| 维度 | 云端（OpenAI 兼容 whisper 端点） | 本地 faster-whisper |
|---|---|---|
| 中文+赛车术语准确率 | 高（large-v3 级） | 中（small/base 明显误听"胎温/DRS"类术语；large 才接近云端） |
| 延迟 | 0.5–1.5s（含上传，本地 LAN/国内可达 1s 内） | small-int8 约 0.3–1s（取决于 CPU） |
| 成本 | 按音频量计费（车手问答量很小，每月几毛~几块钱级） | 0 |
| 资源占用 | 无本地占用 | **与游戏抢 CPU**（推理线程若不限制会顶满小核） |
| 部署 | 需要第二个 API key（DeepSeek **没有**语音端点，需另配厂商） | 首次下模型 ~500MB–3GB |
| 离线可用 | 否（比赛中断网即失效） | 是 |

**推荐**：v1 以云端为默认（`STT_PROVIDER=cloud`，OpenAI 兼容协议可指向任意厂商），
准确率和"不与游戏抢资源"权重最高；`LocalWhisperSTT` 同接口实现、列为可选，
给离线/隐私敏感用户。两者可运行期切换，接口不变。

## 3. TTS 选型：pyttsx3 / edge-tts / 本地神经网络

| 维度 | edge-tts | pyttsx3（SAPI5） | piper（本地神经网络） |
|---|---|---|---|
| 中文音质 | **最好**（晓晓等神经音色，接近真人） | 机械感明显 | 好 |
| 延迟 | 0.5–1s（流式可更低） | 近乎即时 | 0.2–0.5s |
| 网络 | 需要 | 不需要 | 不需要 |
| 依赖 | `pip install edge-tts`（会带 aiohttp 等，属"中重"可选依赖） | pywin32（Windows） | onnxruntime + 模型文件 |
| 维护状态 | 活跃（非官方 API，可能失效） | 基本停更 | 活跃 |

**推荐**：`TTS_PROVIDER=edge` 为默认（工程师角色对"人味"要求高，一句两句话的量，
延迟可接受）；**零依赖兜底**：新增 `SapiTTS`（ctypes 直接调 Windows 内置
`SAPI.SpVoice`，不用 pyttsx3、不用 pywin32，约 20 行，中文音色取决于系统语言包）；
`off` 则只显示文字。edge-tts 失效/无网时自动落回 sapi。

## 4. 按键触发选型（项目原则：零/少依赖）

| 方案 | 依赖 | 问题 |
|---|---|---|
| **浏览器按键（推荐）** | 0 | 需浏览器获得焦点（见 §7 风险） |
| `keyboard` 库全局热键 | 小（含 C 扩展） | 低级键盘钩子：与全屏游戏抢焦点、部分反作弊环境敏感——**触碰本项目"不挂钩"红线** |
| `pynput` | 小 | 同上 |

**推荐**：v1 只做浏览器内 PTT（Push-To-Talk）：按住空格/鼠标长按"说话"按钮录音，
松开发送，Esc 取消；按住 <200ms 视为误触丢弃。原生全局热键只有在"终端模式也要语音"
成为需求时再议，且需用户明确接受钩子类依赖。

## 5. 录音选型与"不与游戏抢资源"

- **v1 浏览器录音（推荐）**：`getUserMedia` + `MediaRecorder`，产出 webm/opus 码流
  （16kHz 单声道即可）。录音发生在浏览器进程，麦克风以共享模式打开，
  **不触碰游戏正在用的音频设备，CPU 占用可忽略**——这是把录音放浏览器的最大理由。
- 原生录音（仅当未来做终端模式）：`sounddevice` 回调线程 + 环形缓冲，
  强制共享模式（WASAPI shared）、16k/mono/小缓冲；绝不用独占模式。
  本方案 v1 不引入。

## 6. 集成方式（webui.py / run.py）

数据流（全部相对路径，浏览器→本地服务，无跨域问题）：

```
[浏览器] 按住PTT ──MediaRecorder──▶ 音频Blob
   POST /api/ask_voice (audio blob)
        │
[webui] voice.py: STT转写 ──▶ engineer.ask(question, state.snapshot())
        │                            │（现有文字问答链路，零改动）
        │                        文字回答
        ▼
   TTS 合成 mp3 ──▶ JSON {question, answer, audio_base64}
   │
[浏览器] 显示问答 + <audio> 播放；识别文本一并展示（便于发现误听）
```

- `webui.py`：+1 个端点（`/api/ask_voice`，`do_POST` 分支）；`/api/state` 响应里
  附带 `voice: {stt: bool, tts: bool}` 供前端决定是否显示麦克风按钮。
- 前端：`PAGE` 内加麦克风按钮与状态机（待机→录音→识别中→回答中→播报中），
  播放时自动禁用新录音避免叠音。
- `run.py`：不改（`--web` 已是推荐入口）。
- 降级矩阵：STT 未配置 → 前端不显示麦克风；TTS 未配置 → 只返回文字；
  edge-tts 运行失败 → 单次落回 sapi 并提示。
- 依赖政策：**核心零依赖不变**。语音全部是可选安装
  （README 增加一小节：`pip install edge-tts`、`pip install faster-whisper`）。

## 7. 风险与缓解

| 风险 | 严重度 | 缓解 |
|---|---|---|
| **全屏独占时浏览器拿不到焦点，PTT 无效** | 高 | README 明确建议无边框窗口/副屏；提供屏幕上"按住说话"按钮（鼠标可触发）；这是浏览器方案唯一硬伤，需用户确认接受 |
| 端到端延迟 3–8s（STT 0.5–1.5 + DeepSeek 推理 2–6 + TTS 0.5–1） | 中 | 回答本来就限 1–2 句；TTS 流式（按句合成先播）；AI 层换非推理模型；长回答跳过 TTS |
| 误触发 | 中 | 纯 PTT 不做 VAD；<200ms 丢弃；Esc 取消；页面失焦自动取消录音 |
| 中文术语误识别（"胎温""DRS""前车"） | 中 | 云端 whisper 的 prompt 参数注入术语表；UI 显示识别文本供纠错；保留现有快捷按钮作为一键替代 |
| edge-tts 非官方接口失效 | 中 | 自动落回 sapi；接口抽象使替换成本低 |
| 比赛中断网 | 低 | STT/TTS 均可切 local/off；问答本身也依赖网络（现状即如此） |

## 8. 待确认的范围问题（请圈选）

1. 按键触发：接受"浏览器内 PTT"作为 v1 方案？（不做全局键盘钩子）
2. STT：默认云端 OpenAI 兼容端点，本地 faster-whisper 做可选？
3. TTS：默认 edge-tts（需可选依赖）+ sapi 零依赖兜底？
4. v1 范围：仅 web 模式、不做终端模式语音？
5. 端点形态：单端点 `/api/ask_voice`（推荐，简单）还是拆 `/api/stt`+`/api/ask`？
