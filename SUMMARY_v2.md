# F1 Race Engineer — v2 主动工程师 · 项目总结

> 生成日期：2026-09-26
> 基线提交：`c59fabf`（GitHub = 本地 = `G:\AI WORK\F1_TR`）
> 当前提交：`2f233ae`
> 本轮范围：T0–T10b（作业书 `PLAN_v2_总工执行规划.md`），设备输入端（手柄/方向盘）按用户指示最后处理。

---

## 1. 一句话结论

项目已从"被动问答"升级为"持续观测 + 关键时刻主动呼叫"的工程师。
**数值全部本地计算，AI 只负责措辞；主动播报完全不调用 LLM。**
全部 29 个 v2 提交已完成，测试 **166 passed / 4 skipped**（4 个为需 `RUN_NETWORK_TESTS=1` 的网络/音频测试）。

---

## 2. 交付状态总览

| 任务 | 内容 | 状态 |
|---|---|---|
| T0 | 测试整理（收进 `tests/`，脚本式→pytest） | ✅ |
| T1 | 修 6 个 bug + 原始 UDP 录制/回放（`.f1rec`） | ✅ |
| T2 | 数据补全（Session/Lap/排行榜/事件）+ flashback 回滚 + Final Classification | ✅ |
| T3 | 骨架：契约/配置 schema/ticker/统一音频出口/语音包/build_app | ✅ |
| T10a | 分发基础：manifest、去 exclude、zipfile、start.bat、selftest、可写性 | ✅ |
| T4 | 推演层：Stint/配速衰退/GapTrend/PitWindow/天气（perf <2ms） | ✅ |
| T5 | 主动播报：规则表 + 模板 + director（无 LLM）+ 安静模式权限 | ✅ |
| T6 | 语音交互：PTT 状态机 + 输入源抽象 + DualSense HID probe + 车手名 | ✅（HID 未闭环，见 §6） |
| T7 | 配置页：独立进程 `FI.py --config`（端口 8766） | ✅ |
| T8 | 赛后复盘：本地 TXT（不调 LLM） | ✅ |
| T9 | 问答增强：命名空间 facts + 意图选择 + 禁进站指令硬规则 | ✅ |
| T10b | 分发收尾 + 文档对齐 | ✅ |
| 附加 | 网页告警条（T5.1）、edge 移除、Piper 接入、SAPI 中文音色、PTT bug 修复、端口防呆 | ✅ |

---

## 3. 关键数据

- **v2 提交数**：29（`c59fabf..HEAD`）
- **测试**：166 passed, 4 skipped（`RUN_NETWORK_TESTS=1` 时全量运行，实测 159→全绿）
- **新增根目录 .py**：41（含 `tests/`、`tools/`）
- **允许的依赖**：faster-whisper、sounddevice、numpy、piper-tts[zh]+onnxruntime（全量包）、pyinstaller（构建）。**edge-tts 已移除**，**禁 audioop**。

---

## 4. 新增/修改文件（核心）

### 新增（运行时）
`contracts.py`（数据契约）、`config_schema.py`（配置单一真源）、`ticker.py`（2Hz 分发）、
`speech.py`（SpeechArbiter + AudioPlayer，唯一语音出口）、`radio_fx.py`（提示音+滤波）、
`voices.py`（语音包注册表）、`race_model.py`（推演层）、`radio_rules.py` / `radio_templates.py` /
`radio_director.py`（主动播报）、`names.py`（车手名）、`input_sources.py` + `ptt_controller.py`（输入）、
`config_ui.py`（配置页）、`debrief.py`（复盘）、`build_manifest.py`（打包清单）。

### 新增（工具）
`tools/udp_record.py` / `tools/replay.py`（`.f1rec`）、`tools/make_zip.py`、`tools/fix_embedded_pth.py`、
`tools/probe_dualsense.py`、`tools/download_piper_voice.py`。

### 新增（数据/脚本）
`data/driver_names.json`（车手名种子）、`start.bat` / `voice.bat` / `语音模式.bat` / `网页模式.bat`。

### 修改
`state.py`（补数据/每圈快照/事件时间轴/flashback/可注入 clock）、`summariser.py`（命名空间 facts + 修 bug）、
`profiles.py`（路由扩充）、`prompts.py`（意图选 fact + 禁进站指令）、`engineer.py`（channel 提示）、
`config.py`（原子写/热加载/校验）、`receiver.py`（raw_sink）、`app.py`（装配 pipeline）、
`webui.py`（告警条 + alerts/race_model）、`voice_main.py`（走 build_app + PTT）、`voice_tts.py`（shim）、
`tts_client.py`（SAPI/Piper，去 edge）、`FI.py`（--config/--selftest/--force/端口防呆）、
`build_release.ps1` + `release.yml`（无漂移）、README/DESIGN/KNOWN_ISSUES。

---

## 5. 本轮修复的真 bug（实测发现）

1. **`fuel_per_lap` / `tyre_wear_per_lap` 从未写入** → 每圈过线时 push。
2. **语音模式无 key 时绕过本地快答** → 统一走 `Engineer.ask(channel="voice")`。
3. **"还剩几圈"答成"第 X 圈"** → 拆路由 + `facts.laps_remaining`。
4. **胎温误报**（瞬时表面温度）→ 改 ~3s 内温中位数（阈值可配，待标定）。
5. **2026 规则下仍报 DRS** → 按 `regulations_2026` 隐藏，用 Overtake 措辞。
6. **`voice_main` 不走 build_app** → 语音模式不录会话 → 已统一。
7. **voice 模式主动播报完全不出声**：radio sink 在 arbiter 创建前捕获 `None`，退化成网页日志 sink。改为**调用时惰性解析** `app.speech`。
8. **PTT 松键双触发**：`RawKeyTrigger` 同时传 `on_tap` 和 `on_press/on_release`，松键先"开始录音"再被 `on_tap` **秒停** → 感觉"STT 没接上麦克风"。改为只用 press/release。
9. **PTT 双击误判**：toggle 模式"按一下开始、再按一下停止"被当双击静音（B1 决定）→ **取消局内双击静音**，静音只在配置页调。
10. **GBK 控制台崩溃**：`FI.py` 打印 `⚠` 抛 `UnicodeEncodeError` → 启动即强制 UTF-8 stdout/stderr。
11. **两个模式抢 UDP 20777** → 加单实例端口防呆（占用者识别 + 转发指引 + `--force`）。
12. **core 包 `data/` 未打包** → 加 `--add-data`；**full 包顶层目录名** + **`tool_selftest` 未入包**（导致 `--selftest` 崩溃）→ 已修。
13. **HF 缓存垃圾入包** → `make_zip` + `robocopy` 双保险排除（保留模型 `blobs/`+`snapshots/`）。

---

## 6. 停止点 / 未闭环项

| # | 项 | 状态 |
|---|---|---|
| 1 | lib 字段名核实 | ✅ 已核实 |
| 2 | `isQualiTypeSession` 含冲刺排位 | ✅ 已核实（含） |
| 3 | edge MP3 解码 | ✅ 按用户决定**彻底移除 edge**，本地只用 SAPI/Piper（均 WAV） |
| 4 | Piper API | ✅ 已核实（`import piper` + `synthesize_wav`→WAV）；**pinyin/g2pW 在本机初始化卡死**，故 Piper 改显式 opt-in，默认不用 |
| 5 | DualSense HID 偏移 | ⚠️ **未闭环**（本机按键不经 Raw Input gamepad usage 上报）；已记录，**最后处理** |
| 6 | 胎温阈值标定 | ⚠️ 需回放真实数据（默认 110 为占位） |
| 7 | 新增依赖 | ✅ 无未批准依赖 |
| 8 | embedded `._pth` | ✅ 已在真实 full 包（0.3.4）验证 `import config/lib` |

---

## 7. 分发验证

| 包 | 构建 | 真机验证 |
|---|---|---|
| **core**（PyInstaller onedir，~20.5MB） | ✅（0.3.5） | ✅ selftest 12/15（3 个 FAIL 预期：无 key、core 无本地 whisper） |
| **full**（embedded Python ~945MB） | ✅（0.3.4） | ✅ embedded python `import config/lib`、模型 `model.bin`(461MB) 在包、HF 垃圾零残留、`._pth` 生效、selftest **14/15**（唯一 FAIL 为未配 key） |
| **干净机器**（沙盒 §12） | — | ⏳ **待用户执行** |

---

## 8. 中文 TTS 现状

- **SAPI（默认）**：自动挑选中文音色 `Microsoft Huihui Desktop`（本机已装），零依赖、即时、WAV。
- **Piper（opt-in，实验性）**：`piper-tts 1.8.0` + `zh_CN-huayan-x_low` 模型已装/已下；但中文标准普通话需 `pinyin/g2pW`，g2pW ONNX 在本机**初始化卡死**，故不作默认。代码/模型保留。
- **edge**：已移除。

---

## 9. 待办（用户侧）

1. **游戏内实跑**（一次只开一个模式）：`语音模式.bat` 或 `网页模式.bat`
   - 遥测数字核对、主动播报时机/话量、进站类只有信息无指令、语音问答、赛后复盘 TXT。
2. **胎温阈值标定**：录制含重刹弯数据 → 回放 → 报内温范围。
3. **Windows Sandbox 干净机器清单（§12）**：无 Python、中文/空格路径、只读目录、覆盖升级不丢 `.env`/`sessions/`。
4. **手柄/方向盘输入**（最后处理）：DualSense probe 未闭环；有屏幕方向盘的厂家软件可能与 SimHub 争 UDP 20777。

---

## 10. 使用速查

```powershell
cd "G:\AI WORK\F1_TR"

# 网页模式（推荐：面板 + 告警条）
网页模式.bat            # 或 py -3.12 FI.py --web

# 语音模式（游戏内小键盘 + 说话）
语音模式.bat            # 或 py -3.12 FI.py --voice

# 设置页（独立进程，端口 8766）
py -3.12 FI.py --config

# 自检
py -3.12 FI.py --selftest

# 指定转发端口（与 SimHub 共存：SimHub UDP Forward -> 20778）
py -3.12 FI.py --port 20778 --voice

# 录制 / 回放原始 UDP
py -3.12 -m tools.udp_record --out sessions\race.f1rec
py -3.12 -m tools.replay sessions\race.f1rec --port 20777 --speed 2

# 测试
py -3.12 -m pytest -q                        # 166 passed, 4 skipped
$env:RUN_NETWORK_TESTS=1; py -3.12 -m pytest # 含网络/音频
```

> **重要**：网页模式与语音模式**只能开一个**（否则抢 UDP 20777，数据错乱）。
> 与 SimHub / 方向盘厂家软件共存的唯一办法是让占用者转发到另一端口。

---

## 11. 架构要点（不变原则）

- AI 不进实时回路：数值全本地，AI 只措辞。
- 先压缩再喂模型。
- 被动只读：UDP 收包 + Raw Input 只订阅，不注入/不挂钩/不驱动。
- 单包异常不致命：包容 + 计数。
- stdlib 优先。
- 扩展点工厂化：`make_llm` / `make_stt` / `make_tts` 同构，配置驱动。
- 表达层与计算层分离。

线程模型：接收线程（轻量）→ ticker（2Hz，推演+播报+热加载+复盘）→ Arbiter（~20Hz，时机闸门+播放）→ 合成线程池；主线程跑 Raw Input PTT。
