# F1 Race Engineer

> 一个安静运行在后台的 **AI 赛车工程师**：读取 F1 游戏广播的遥测数据，把它压缩成结构化的信息，然后通过**语音或文字**回答车手关于当前比赛状况的问题。

**[ 读遥测 → 结构化总结 → AI 问答 → 语音播报 ]**

只给情报和建议，**不碰车辆操控**。

---

## 特性

- 🏎 **实时遥测解析** — 支持 F1 2023–2026 全部 16 种 UDP packet，适配 2026 Season Pack（24 车）
- 📊 **全场位置表** — 位置 / 车手 / 圈数 / 轮胎 / 胎龄 / 差距
- ⏱ **圈速分析** — 圈速历史、分段计时、vs 最快圈 delta、无效圈过滤
- ⛽ **油耗策略** — 消耗率、剩余圈数、完赛油量预测
- 🌡 **轮胎 + 损伤** — 胎温/胎压/磨损、前后翼/底板/引擎车损、进站状态
- ⚔ **战况感知** — 位置变化事件，由游戏官方 OVTK 超车事件驱动
- 🎙 **语音问答** — 按键说话 → 本地语音识别 → AI 回答 → 语音播报
- 🎧 **观赛模式** — 焦点自动跟随被观看的车辆
- 📝 **会话录制** — 自动生成 JSON（原始）+ TXT（可读）报告
- 🖥 **双界面** — 网页面板 + 终端面板

---

## 快速开始

### 1. 环境

- **Windows**
- **Python 3.12 / 3.13**

### 2. 配置

复制 `.env.example` 为 `.env`，填入你的 DeepSeek key：

```
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

### 3. 游戏设置

F1 游戏内 **设置 → UDP 遥测**：

| 项 | 值 |
|---|---|
| UDP 遥测 | **开启** |
| UDP IP | `127.0.0.1` |
| UDP 端口 | `20777` |
| UDP 赛制 | **2026**（或与你游戏版本对应）|
| 你的遥测 | **受限** ⚠️ |

> ⚠️ **重要**：不要改动"你的遥测"设置。实测改动后会导致 UDP 广播失效（改回来也无法恢复），必须**重启游戏**才能恢复。

### 4. 运行

```
# 网页模式（文字问答）
py -3.12 run.py --web
# 浏览器打开 http://127.0.0.1:8765

# 终端面板
py -3.12 run.py

# 语音模式（见下方"语音功能"）
py -3.12 voice_main.py

# 一键启动（Windows，网页模式）
双击 启动.bat
```

---

## 语音功能（可选）

语音采用**全本地**方案，按键说话，无需常驻监听。

**触发**：游戏中按 **小键盘 0**（按一下开始录音，再按一下结束）

**流程**：按键 → 录音 → 本地语音识别 → AI 问答 → 语音播报

### 启用步骤

**1. 安装本地语音依赖**（装到项目内的 `stt_lib/`，不污染全局）

```
py -3.12 -m pip install faster-whisper sounddevice --target stt_lib
```

**2. 下载语音模型**（下载到项目内的 `stt_models/`）

```
py -3.12 download_stt_model.py small
```

> 国内网络若下载失败，先设置镜像：
> `set HF_ENDPOINT=https://hf-mirror.com`

**3. 运行**

```
py -3.12 voice_main.py
```

**可选参数**：`--input G733 --output G733`（按设备名片段选麦克风/输出）
修改触发键：编辑 `voice_trigger.py` 的 `TRIGGER_VK`。

> `stt_lib/` 和 `stt_models/` 体积较大，已在 `.gitignore` 中排除，需按上述步骤自行下载。

### 为什么用 Raw Input / 本地识别？

- **按键检测用 Windows Raw Input**（标准 API，只读，不注入、不挂钩、不映射），全屏游戏时仍可工作。手柄走 XInput 会被游戏独占，键盘不会。
- **语音识别用本地 faster-whisper**，数据不出本机。

---

## 架构

```mermaid
flowchart TD
    GAME["F1 游戏<br/>UDP 广播 127.0.0.1:20777"]

    subgraph L1["1. 接收层 receiver.py"]
        UDP["UdpTransport<br/>异步收包"]
        PARSE["PacketParserFactory<br/>16 种 packet 解析"]
        GATE["SessionFrameGate<br/>去重/防回退"]
        UDP --> PARSE --> GATE
    end

    subgraph L2["2. 状态层 state.py (TelemetryState)"]
        SNAP["实时快照 / 全场位置表<br/>位置变化事件 / 趋势"]
    end

    subgraph L3["3. 总结层 summariser.py"]
        SUM["facts · notes · leaderboard · events<br/>★ 纯计算，把 60Hz 压成几行摘要"]
    end

    subgraph L4["4. AI 层"]
        PROMPT["prompts.py"]
        CLIENT["ai_client.py (DeepSeek)"]
        ENG["engineer.py"]
    end

    subgraph L5["5. 输出层"]
        WEB["webui.py 网页面板（文字）"]
        CONSOLE["console_ui.py 终端面板"]
        VOICE["voice_*.py 语音（触发/STT/TTS）"]
        REC["recorder.py + report_txt.py"]
    end

    GAME -->|"UDP 20~60Hz"| UDP
    GATE --> SNAP --> SUM
    SUM --> ENG
    PROMPT --> ENG
    CLIENT --> ENG
    ENG --> WEB
    SUM --> CONSOLE
    SUM --> VOICE
    SUM --> REC
```

详见 [DESIGN.md](DESIGN.md)。代码评审见 [CODE_REVIEW.md](CODE_REVIEW.md)，已知问题见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。

---

## 项目结构

```
F1_TR/
├── run.py              主入口（--web 网页 / 终端面板）
├── voice_main.py       语音入口（遥测 + 语音问答，单进程）
├── receiver.py         单进程 UDP 收包 + 解析调度
├── state.py            遥测状态聚合（快照/位置表/事件/趋势）
├── summariser.py       总结层：把状态压成 facts + notes
├── ai_client.py        DeepSeek API 客户端（标准库）
├── prompts.py          系统提示词 + 快照文本构造
├── engineer.py         问答引擎
├── webui.py            网页 UI
├── console_ui.py       终端 UI
├── recorder.py         会话录制
├── report_txt.py       TXT 报告生成
├── voice_trigger.py    Raw Input 按键触发（不注入/不挂钩）
├── voice_stt.py        本地 faster-whisper 语音识别
├── voice_tts.py        Windows SAPI 语音合成 + 播放
├── download_stt_model.py  下载语音模型到项目内
├── 启动.bat            一键启动（网页模式）
├── lib/                核心库
│   ├── f1_types/           16 种 F1 packet 解析（2023–2026）
│   ├── socket_receiver/    UDP 传输
│   ├── telemetry_manager/  解析工厂 + 帧门
│   ├── delta/              圈速 delta
│   └── fuel_rate_recommender.py / rolling_history.py
└── sessions/           自动生成的会话记录（运行时产生）
```

---

## 非目标

明确**不做**：

- ❌ 车辆操控（按键模拟 / 手柄注入 / 内存修改）
- ❌ 读取或修改游戏进程内存
- ❌ 绕过反作弊
- ❌ 提供游戏本身不广播的数据

本项目**只被动接收**游戏官方广播的 UDP 数据；语音按键用 Raw Input **只订阅、不干预**输入。

---

## 技术栈

- **Python 3.12**，接收/解析/HTTP 服务/语音播放全部基于 **标准库 + 少量可选本地依赖**
- **DeepSeek API** 提供语言能力
- **faster-whisper** 提供本地语音识别（可选）
- 遥测解析复用 [pits-n-giggles](https://github.com/ashwin-nat/pits-n-giggles)（MIT）

---

## License

MIT。复用 pits-n-giggles 的模块遵循其 MIT 许可，详见 [LICENSE](LICENSE)。

## 致谢

- [pits-n-giggles](https://github.com/ashwin-nat/pits-n-giggles) — F1 遥测解析库
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 本地语音识别
- [DeepSeek](https://deepseek.com) — AI 语言能力
