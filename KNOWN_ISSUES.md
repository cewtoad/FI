# 已知问题 / 待办

> 记录测试中发现但暂未修复的问题，供后续处理。

## 已修复

### ✅ ISSUE-4：领先/落后的方向可能反了 —— 已定位并修复
- **核实结论**（用 `sessions/session_20260922_004903.json` 现场数据）：
  该局玩家 `car_index=19`、**P18、`gap_to_leader_ms=19982`**（落后领先者 19.982s）。
  数据方向**完全正确**（F1 UDP `deltaToRaceLeader` 正数=落后）。问题在**表达层**：
  - `summariser.py` 把 `gap_to_leader` 渲染成无符号裸数字 `19.982`，
    键名 `gap_to_leader` 会被模型脑补成"领先领先者"。
  - `_fmt_ms` 对 `<=0` 返回 `"-"`，进一步丢失语义。
- **修复**：
  - `summariser.py`：新增 `_fmt_gap_signed()`，输出 `落后 19.982s`；
    领跑者输出 `领先全场`；`gap_to_front` 同理。
  - `prompts.py`：排行榜表头写明"该车落后领先者的秒数"，每行 `领先全场` / `落后 X.Xs`；
    事实规则新增一条"gap 正数一律表示你落后，判断领先/落后以 position 为准"。
  - 新增 `test_gap_direction.py`：用真实现场数值锁定方向（4 项断言）。

### ✅ ISSUE-5：AI 回答啰嗦 —— 已处理
- `prompts.py` 增加禁止项：不要主动补充车手没问的信息。
- `profiles.py` 引入 `fast` 档（一句话、禁止展开、max_tokens=200）。
- 新增"本地快答路由"：高频确定性问题（P几/剩几圈/油/胎/损伤…）**不经过 LLM**，
  直接由 summary facts 回答，零延迟零成本、天然不啰嗦。

### ✅ ISSUE-1：超时停止时音频为空 —— 已加保护
- `voice_main.py` `_stop_and_answer_locked()`：停止前检查
  `StreamingRecorder.collected_samples()`，低于 `MIN_SAMPLES`（0.3s）时
  明确提示"录音太短（没收到音频）"，不再进入 STT 得"没听清"。
- `_auto_stop` 与 `start()` 仍共享同一把 `_lock`，`stop()` 内部加了
  `try/finally` 保证流一定关闭。

### ✅ ISSUE-3：STT 识别耗时偏慢 —— 配置已打通（待实测）
- `voice_main.py` 改为走 `make_stt()` / `LocalWhisperSTT`，
  `.env` 的 `STT_LOCAL_MODEL` / `STT_LOCAL_THREADS` 现在**真正生效**
  （此前本地语音栈硬编码 `small` 且不设 `cpu_threads`，与游戏抢 CPU）。
- 若仍慢：调 `STT_LOCAL_THREADS`（2 或 3）、或换 `STT_LOCAL_MODEL=base`。

## 语音相关（待实测）

### ISSUE-2：STT 识别偶有掉词
- **现象**：说"诺里斯的圈速比我快多少"，被识别为"我和圈速比我快多少"（丢了"诺里斯"）。
- **可能原因**：small 模型对专有名词（车手名）识别弱；或录音音质。
- **状态**：待实测（线程数配置打通后可能改善）。

## 架构 / 工程

### ✅ 双语音栈收敛
- 本地栈（`voice_stt`/`voice_tts`）与浏览器栈（`stt_client`/`tts_client`）现在共用：
  - `config.py` 唯一配置入口（不再各自 `load_dotenv`）
  - `audio.py` 唯一设备解析（删除 3 处 `_device_index()`）
- `voice_stt.LocalSTT` / `StreamingRecorder` / `voice_tts.LocalTTS` 均默认从
  `.env`（`AUDIO_INPUT`/`AUDIO_OUTPUT`）取设备，不再硬编码 `G733`。

### ✅ 冻结快照（线程安全）
- `state.py`：接收线程以 `SNAPSHOT_HZ=2` 产出深拷贝快照到单槽+锁；
  `snapshot()` 返回冻结件（脏时按需重建），消灭活引用竞态。

### ✅ webui 加固
- question 长度上限（500 字符）、请求体上限、全局 in-flight 信号量（429）、
  Host 头校验（防 DNS rebinding）。
- `bind_ip` 参数现在真正透传（`run.py --bind-ip` 在 `--web` 下生效）。

### ✅ 录制挂接收路径
- `app.py` 的 `on_packet` 钩子每包调用 `recorder.record_state`（加锁），
  不再依赖浏览器轮询。

### ✅ 工程化
- `pytest.ini` / `pyproject.toml`：`testpaths` + `norecursedirs`（不再扫 `stt_lib`）；
  `test_scripts_runner.py` 把原 `tests_*.py` 脚本纳入 pytest（35 项全绿）。
- `lib/f1_types/header.py` 补 `__hash__`（与 `__eq__` 一致）。
- README 16→17 种 packet。

## 本轮新增（分发 / 打包）

### ✅ 无 API key 兜底
- `engineer.py`：未命中快答且未配置 key 时，返回**可操作提示**
  （告知去设置页填 key + 列出无需 key 即可问的本地问题），不再是死路。
- 快答路由始终在 key 检查之前，无 key 也能答：名次/圈速/油量/胎温/轮胎/损伤/前车差距/进站。

### ✅ 网页 onboarding
- 无 key 时页面顶部显示设置面板：填 Base URL / 模型 / key →
  保存 → 自动 `GET /api/models` 测连通 → 内嵌游戏 UDP 设置图文。

### ✅ 冻结路径（打包前置）
- 新增 `paths.py` 的 `app_root()`：frozen 时指向 exe 旁，否则源码目录；
  支持 `F1TR_ROOT` 环境变量覆盖。
- `config.py`/`recorder.py`/`audio.py`/`voice_stt.py`/`voice_tts.py`/`download_stt_model.py`
  全部改用它，`.env` / `sessions/` / `stt_*` 在打包后落在解压目录（绿色软件语义）。

### ✅ 上轮遗留 4 小问题
- UDP `--udp-bind` 与网页面板 bind 分离（`webui.py:536` 硬编码已修）；
  主机广播到 PC 的场景现在可用。
- `/api/llm`、`/api/audio` 写接口**仅允许 127.0.0.1 来源**（防局域网改 key）。
- `app._on_packet` 降频（LAP_DATA 立即，其余最多 2Hz）。
- `test_stt_mic.py` 改名 `tool_stt_mic.py`（不再被 pytest 收集，CI 不会崩）；
  pytest 的 `norecursedirs` 加入 `build_lib/dist/build`。

### ✅ 打包与分发
- `FI.py`：启动二选一（网页 / 语音），网页模式自动开浏览器；含端口占用预检。
- `build_release.ps1`：一键产出两个包
  - `F1Engineer-core-<v>-win64.zip`：PyInstaller **onedir + --noupx**（实测 20.9MB）
  - `F1Engineer-full-<v>-win64.zip`：源码 + stt_lib + stt_models + embedded Python + 启动.bat
- `version_info.txt`：exe 版本元数据（CompanyName/ProductName），降低 AV 误报。
- `.github/workflows/release.yml`：**push tag `v*`** 才构建 → pytest → core zip + SHA256 → Release。
- `RELEASE_SIGNING.md`：SignPath OSS 免费签名申请指引。

### ✅ 体验
- 端口占用友好提示（20777 被 SimHub 等占用时给说明）。
- 版本更新检查横幅（`/api/version`，6 小时缓存，只提示不自动下载）。

## 待办（用户明确想做）

- 申请 SignPath 签名（见 RELEASE_SIGNING.md，需等审批）
- 全量包内置 embedded Python（需下载 python-3.12.x-embed-amd64.zip 到 python-embed/）
- 整理上传


## v2（主动工程师）— T0..T10b 已完成（2026-09）

### 已修 bug（本轮）
- `fuel_per_lap` / `tyre_wear_per_lap` 从未写入 → T1.4 已在每圈过线时 push。
- 语音模式无 key 时绕过本地快答 → T1.1 统一走 `Engineer.ask(channel="voice")`。
- “还剩几圈”答成“第 X 圈” → T1.2 拆路由 + facts.laps_remaining。
- 胎温误报（瞬时表面温度） → T1.3 改用 ~3s 内温中位数（阈值可配，待实测标定）。
- 2026 规则下仍报 DRS → T1.5 按 regulations_2026 隐藏 DRS，用 Overtake 措辞。
- `voice_main` 不走 build_app → 语音模式不录会话 → T3.7 已统一。

### 新增能力
- 原始 UDP 录制/回放：`tools/udp_record.py` / `tools/replay.py`（`.f1rec`）。
- 推演层：`race_model.py`（Stint/配速衰退/GapTrend/PitWindow/天气/排位支撑）。
- 主动播报：`radio_rules.py` / `radio_templates.py` / `radio_director.py`（**无 LLM**）。
- 统一语音出口：`speech.py`（SpeechArbiter + 非阻塞 AudioPlayer）。
- 语音交互：`ptt_controller.py`（hold/toggle/双击静音）、`input_sources.py`、DualSense HID probe。
- 配置页：`config_ui.py`（`FI.py --config`，端口 8766）。
- 赛后复盘：`debrief.py`（本地 TXT，不调 LLM）。
- 分发：`build_manifest.py`、`tools/make_zip.py`、`start.bat`、`tools/fix_embedded_pth.py`。

### 待实测 / 停止点
- **胎温阈值标定**：`TYRE_HOT_INNER_C` 默认 110 为占位值，需回放真实数据标定。
- **DualSense HID 报告偏移**：需跑 `py -3.12 -m tools.probe_dualsense` 实测后再启用 HID 源。
- **edge MP3 解码**：当前 AudioPlayer 只原生解码 WAV；edge 返回 MP3 需要新依赖（audioop 已被 3.13 删除，禁用），未决。
- **Piper**：import 名/API 未核实，provider 暂禁用。
- **embedded `._pth`**：已加规范化脚本，仍需在真实全量包上验证 `import config/lib`。

### 文档对齐（本轮）
- 核心包不再 exclude sounddevice/numpy → 语音模式在核心包可用。
- DS5 非 XInput 设备，按键检测用 Raw Input HID（README 原“XInput 被游戏独占”说法已过时）。
- STT/TTS 标记为已实现（DESIGN）。


## 输入触发方式（PTT）— 架构现状与待办

> 目标：支持三种物理输入作为"按键说话(PTT)"触发源：**键盘**、**手柄**、
> **方向盘/其他外设**。三者必须可共存、可切换，且互不影响主系统。

### 隔离设计（已落地，改动面被限制在一个文件）

- 抽象层 `input_sources.py`：
  - `InputSource`（基类）：统一 `on_press` / `on_release` / `on_tap` 回调 +
    `start()` / `stop()`。
  - `KeyboardSource`：包装 `voice_trigger.RawKeyTrigger`（**已启用、已验证**）。
  - `HidSource`：通用 HID 按键源（**已实现骨架，默认禁用**，待偏移确认）。
- 唯一工厂 `make_source(binding, ..., enable_hid=False)`：按 binding 前缀
  分派 `kb:<vk>` / `hid:VID:PID:byte:mask`。**HID 默认门控关闭**，非键盘
  绑定在未确认前返回 `None`。
- 上层（`ptt_controller` 纯状态机、`voice_main`、`speech`、`radio_*`）**完全不
  感知输入源类型**，只接收 press/release/tap 事件。

**结论：完善手柄/方向盘支持只需改 `input_sources.py`（+ 配置项），
不影响 ptt_controller / voice_main / speech / radio / app / state / 任何测试。**

### 现状

| 输入方式 | 状态 | 绑定格式 | 备注 |
|---|---|---|---|
| 键盘 | ✅ 可用（默认） | `kb:<vk>`，默认 `kb:0x6B`（小键盘 +） | 全屏可用，Raw Input 只订阅 |
| DualSense 手柄 | ⚠️ 未闭环（本机实测失败） | `hid:VID:PID:byte:mask` | 见下方"实测记录" |
| 方向盘 / 其他外设 | ⏳ 未做（用户暂无设备） | 同上（通用 HID） | 待有设备后按同一流程做 |

### 实测记录：DualSense（VID_054C PID_0CE6）— 未闭环

- 设备在系统中存在：`HID\VID_054C&PID_0CE6&MI_03`（`Get-PnpDevice -Class HIDClass`）。
- Raw Input 能收到报告：`len=64`，`report_id=0x01`，约 250Hz，静止时平均
  **20+ 字节/帧在变**（陀螺仪/触控/扳机噪声）。
- 三种探测法均无法定位 R1：
  1. `--scan`：只有一个报告 `len=64 id=0x01`，按钮与传感器混在同一报告。
  2. `--diff-auto`（静止基线 vs 按住，取"静止=0 且按住稳定=1"的位）：返回 none。
  3. `--hold-now`（按住全程稳定的位）：常量位淹没信号，无法区分。
- **未开 Steam Input / DS4Windows** 的前提下仍失败。
- **推测根因**：本机 DualSense 的实体按键可能不在 gamepad usage(0x05)/joystick
  usage(0x04) 的这份报告里，而是走了另一个 HID 接口（如消费者控制 / vendor），
  或被系统以非标准方式上报。需要专门的接口枚举才能确认。
- **决策**：按 R9 不盲目硬试偏移（避免随机猜）。**暂不接入手柄**，主路径用键盘。

### 待办（后续单独完善，互不影响）

1. **手柄（DualSense）**
   - 做一个 `--interfaces` 探测模式：枚举该设备的所有 HID 接口
     （`MI_00..MI_03`），对每个接口尝试不同 UsagePage/Usage 注册，
     定位实体按键所在的报告与偏移。
   - 确认后：实现 `HidSource.start()`（Raw Input HID 消息循环），
     在 `make_source` 打开 `enable_hid`，在配置页选 HID 绑定。
   - 备选：若确认 Windows 只暴露触控/传感器接口，可考虑 `Windows.Gaming.Input`
     或 `RawGameController`（UWP API）——但这会引入平台分支，需先评估。
2. **方向盘 / 其他外设**
   - 拿到设备后，复用同一 `--interfaces` 流程确认偏移；
   - 通用绑定格式 `hid:VID:PID:byte:mask` 已支持任意 VID/PID，无需改架构。
3. **绑定向导（可选）**
   - 把 `tools/probe_dualsense.py` 的向导泛化为"任意设备按键扫描"，
     配置页 `POST /api/bind` 已预留接口。
4. **多输入源并存（可选）**
   - 若需"键盘或手柄都能触发"，让 `voice_main` 同时启动多个来源，
     事件合并到同一 `PTTController`；当前架构已支持（回调可多路）。

### 相关文件
- `input_sources.py`（核心，唯一需改）
- `tools/probe_dualsense.py`（探测工具：`--scan` / `--diff-auto` /
  `--hold-now` / `--wizard` / `--diff`）
- `ptt_controller.py`（纯状态机，无需改）
- `voice_main.py`（通过 `make_source` / 配置绑定，无需改）
- `config_schema.py`：`PTT_MODE` / `PTT_BINDING` / `PTT_DOUBLE_TAP_WINDOW_MS`
