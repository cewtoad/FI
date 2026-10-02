# 已知问题 / 待办

> 记录测试中发现但暂未修复的问题，供后续处理。

## ✅ CI 首绿修复（2026-10-03，release workflow 首次真正运行）

仓库根曾有一个 pits-n-giggles 时代的 `__init__.py`（骨架 docstring，无代码引用）。
GitHub Actions 的检出目录名叫 `FI`（D:\a\FI\FI），pytest 解析根 conftest 包名时
向上走 `__init__.py`，把仓库根当成了包 `FI` —— 导入 conftest 前先执行骨架
`__init__.py` 并缓存进 `sys.modules["FI"]`，之后测试里所有 `import FI` 都拿到
骨架而不是启动器模块（无 _port_busy/--selftest/--port），5 个测试必挂。
本地目录名叫 F1_TR 所以从未复现；release workflow 在 v0.5.0 之前从未真正运行
（历史 release 均为本地构建手动上传），因此一直没暴露。修复 = 删除该文件，
同名目录已复现验证。教训：**不要在仓库根放 `__init__.py`**（尤其当仓库名与
根模块同名时，CI/任意检出目录名都会触发包名遮蔽）。

## 审查第二轮修复（2026-09-27，全量 pytest 199 passed / 4 skipped）

### 严重（4）
- **S1 `speech.py`**：播报线程 `_run` 全程无异常隔离，一条损坏 WAV 就永久杀死唯一工作线程（radio 整体哑掉）→ pop 后整段包 try/except + 计数；`_emit` 捕获放宽到 Exception。合成池由 FIFO `ThreadPoolExecutor` 改为**优先级感知的 daemon 线程**（P0 不再排在两条长合成后面；Piper 挂死不再卡进程退出）。
- **S2 `state.py`**：包流期间 dirty 恒真，每个 HTTP/语音读者都 force 全量 deepcopy（持 `_state_lock`），读者越多 RX 丢包越多 → 读者仅在**无发布者新鲜副本**时才按需重建（发布者 2Hz 活跃时直接复用冻结件），并加 `_build_lock` 单飞。附带：快照 events 窗口 6→24、flashback 后重置位置基线（修假"掉位"事件）、`_reset_for_new_session` 清 `packet_errors`、直道阈值 60Hz→5s 缓存、snapshot provider 契约文档修正。
- **S3 `radio_director.py`**：跨会话状态从不重置——第二场 `_last_event_seq` 残留旧值，**安全车/红旗/罚时/超车等事件规则整场失效** → tick 检测 `session_uid` 变化即清 `_fired_keys/_last_fired/_last_event_seq/_prev_model` 等。
- **S4 `race_model.py`**：PitWindow "done" 只由瞬态 pit_status 支撑，进站后回退成 open/missed 假警报 → 用 `num_pit_stops` 增量把 done 闩锁到窗口轮转。

### 中（其余）
- `.bat`×5：嵌入式 python 路径含空格时双击即失败 → 嵌入路径单独加引号分支。
- `config_schema.validate`：str 值不滤换行（可向 .env 注入任意键）→ 拒绝换行+剥控制字符；NaN 穿透范围校验 → `isfinite` 拒绝；`int(inf)` 的 OverflowError 逃逸 → 并入 except；`PTT_BINDING` 接入格式校验（与 `parse_binding` 同语义）。
- `config.py`：`_persist` 读+合并移进锁内（并发写不再互相丢键）；热加载 overlay 逐出改按 **file-only keys** 判定（不再被 os.environ 遮蔽回退）。
- 圈数口径统一：`race_model.fuel_laps_left` 改为 `total-cur+1`（与 summariser `laps_remaining` 同源，LLM 不再看到差 1 的两个"剩余圈数"）。
- 播报 dedup key 修复复发漏播：fastest_lap_you 按 seq、tyre_critical/attention 按 stint、wrong_way/undercut 按圈、rain_incoming 按 ETA、unserved_penalty 按次数。
- `_rule_tt_new_pb`：`tt_pb_ms` 由 race_model flags 持久化，首拍见到的 PB 不再误报"新纪录"。
- 2026 漏网 DRS：`_rule_drs_fault` 与 `report_txt` 按 `regulations_2026` 屏蔽（recorder 补记该标志）。
- `debrief.py`：写失败不再永久跳过（成功后才置位）；无 session_uid 的会话也能写复盘。
- `llm_client.FallbackLLM`：回退也失败时带上 primary 的错误（429 详情等）。
- `ptt_controller.on_release`：无前置 press（reset 后残留）不再误开始录音。
- `RADIO_GAP_EVERY_N<=0` 视为静音（原先退化成每圈播）；`laps_to_1s` 按总圈数钳制（噪声斜率不再输出几万圈）。
- 阈值同源：磨损阈值统一走 `TYRE_WEAR_LIMIT_PCT`；缺油阈值新增 `RADIO_FUEL_DEFICIT_LAPS`。
- `engineer.cancel()` 修复为真实生效（LLM 调用前后检查）；`app._reload` 在 .env 变化后 `refresh_client()`，备用端点/超时配置现在热生效。

### 轻（清理）
- 吞异常补限频日志+计数：receiver 快照刷新、state provider、app recorder/speech 启动。
- stats 计数 dict 拷贝竞态（receiver/ticker）加有界重试。
- webui：`/api/ask_voice` 外层兜底返回 500；`_receiver_thread` 死参数、未用导入清理。
- 死代码删除：`speech._priority_bucket`、`profiles._route_rival_pace`、`FI._web_panel_running`、`config.as_dict`、`build_manifest.EXCLUDE_*`；`input_sources` on_tap 失真注释修正；`profiles._route_weather` 补 `rain_percentage` fact（summariser 现在产出）。
- 其余：`audio.py` 设备提示 `or` 优先级、FI netstat/tasklist 按 ANSI 代码页解码、config_ui Content-Length 容错+256KB 上限、build_manifest 白名单缺文件告警。
- 补 20 条回归测试（`tests/test_review2_fixes.py`，含 webui 护栏 403×2 与 `_local_only` 直测）。

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
- **edge MP3 解码**：✅ 已解决（按用户决定：**彻底移除 edge**）。本地播报只用 SAPI + Piper（均输出 WAV），不再有 MP3 解码问题；`EdgeTTS` / edge 语音包 / `_edge_rate` 已从代码移除。
- **Piper**：✅ 已解决——`piper-tts` import 名为 `piper`，`PiperVoice.load(.onnx)
  .synthesize_wav()` 输出真实 WAV（不碰 MP3 解码）。`tts_client.PiperTTS` +
  `voices` + `tools/download_piper_voice.py` 已接入；需 `pip install "piper-tts[zh]"`
  并下载中文模型。
- **embedded `._pth`**：✅ 已在真实全量包（0.3.4）验证——embedded python 能
  `import config` / `import lib`，selftest 14/15（唯一 FAIL 为未配 key，预期）。
- **全量包打包健壮性**：✅ 已修——`make_zip` + `robocopy` 双保险排除 HF 缓存垃圾
  （`.locks`/`trees`/`.agent_harnesses.json`/`CACHEDIR.TAG`），保留模型
  `blobs/`+`snapshots/`（验证 zip 内含 461MB model.bin）；顶层目录统一为
  `F1Engineer/`；`FI.py --selftest` 依赖的 `tool_selftest.py` 已入包。

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
| DualSense 手柄 | ✅ 已闭环（2026-10-02 实测） | `hid:VID:PID:byte:mask`，R1 = `hid:054C:0CE6:9:0x02` | USB；见下方"实测记录"与闭环说明 |
| 方向盘 / 其他外设 | ⏳ 未做（用户暂无设备） | 同上（通用 HID） | 待有设备后按同一流程做 |
| Xbox 手柄（045E） | 🟡 已适配、待硬件实测（用户暂未接入） | 同上（通用 HID，捕获向导自动适配） | 走微软 XInput 驱动栈，Raw Input 同样以 usage 0x01/0x05 送 20 字节左右报文；按键为位域（无 hat 值问题）。已做适配：VID 级时机配置（基线 0.5s/按住 0.12s）、pending 卡死保护（计数器位假按住 >2.5s 自动拉黑继续扫描）、describe_binding Xbox 前缀。待实测：实际报文字节布局（按键是否落 byte4-15）、稀疏发帧下的捕获体验 |

### ✅ DualSense 已闭环（2026-10-02）

- **布局有权威文档**（nondebug/dualsense，与 Linux 内核 hid-playstation.c 一致），本就无需"探测发现"。
- **旧探测失败根因**：① `RegisterRawInputDevices` 每次调用**整体替换**注册表，旧 probe 分两次注册后只剩 Joystick usage 生效；② `--hold-now` 被摇杆中位（0x7e/0x81/0x84/0x84）的常量位淹没；③ 报文本身一直正确（len=64、id=0x01、250Hz，静止 20+ 字节变化=序列号+陀螺仪+加速度计）——按钮位始终在报文里。
- **实测确认**（独立脚本，边沿干净无抖动）：R1=byte9/0x02（9 次含 2.3s 长按）、✕=byte8/0x20（14 次）、△=byte8/0x80、Create=byte9/0x10、Options=byte9/0x20、十字键=byte8 低半字节（0=N 2=E 4=S 6=W 8=中位）。蓝牙连接布局不同（0x01 为 10 字节紧凑报文），首版仅支持 USB。
- **落地**：`HidSource` 已实现并默认启用——单次数组注册 Gamepad(0x01/0x05)+Joystick(0x01/0x04)（修掉整体替换坑）、按设备路径过滤 VID/PID（第二个手柄不会误触发）、短报告忽略防假释放、`run_blocking` 走主线程（与键盘同模式）；`voice_main` 改走 `make_source` 统一分派，hid 绑定不再静默回退键盘；新增 `tests/test_hid_source.py`（合成报文锁定边沿逻辑）。
- **首测失败修复（2026-10-03）**：`_device_matches` 原"NULL 缓冲两段式"查询设备名对 `GetRawInputDeviceInfoW(RIDI_DEVICENAME)` 不生效，返回空导致所有报告被 VID/PID 过滤器静默拦掉；改为预分配缓冲单次调用后 250Hz 全通。教训：ctypes 调查询类 Win32 API 不要用两段式 NULL 缓冲惯例，直接预 sizing。
- **模块化 + 按键捕获向导（2026-10-03）**：输入源拆分为 `inputs/` 包（`base`/`bindings`/`keyboard`/`hid`），`input_sources.py` 保留兼容门面，build_manifest 把 `inputs/` 加入 RESOURCE_DIRS 与 hidden-imports；新增 `capture_hid_binding`（基线学习静息位地板 → 首个持续 ≥0.25s 的 0→1 位 → **松开确认**，VID/PID 从设备路径自动解析，基线期已按住的键不可捕获——先松开再捕获）；网页【功能设置】PTT 按键行新增设备下拉（键盘/手柄）+ 一键捕获，`/api/bind` 支持 `{"device": "hid"}`；`PTT_MODE`（hold/toggle）本就在同页可选。
- **捕获向导两个实机 bug 修复（2026-10-03）**：① **假捕获**——空手复现 `hid:054C:0CE6:13:0x1`：DS 的 payload 计数器字节（12-15）高位可停留数秒，通用扫描范围 byte4-15 会把它当按键；修复 = 已知 DualSense 只扫文档按钮字节 8-10（其他设备仍 4-15）+ **松开确认**（计数器位长期停留无法"松开"，自然被拒）。② **同进程重复捕获秒退**——`GetMessageW+PostQuitMessage` 的 WM_QUIT 残留在线程消息队列（keep-alive HTTP 线程会复用），且固定窗口类名使第二次 RegisterClassW 失败、窗口路由到已回收的回调（潜在崩溃）；修复 = PeekMessage 泵（不用 quit 标志）+ 每次捕获唯一窗口类名 + `DestroyWindow` 同步拆除。教训：线程内做 Win32 消息泵不要用 PostQuitMessage（污染线程队列），用 PeekMessage 轮询；ctypes 回调对象必须与窗口类/窗口同生命周期。
- **十字键（hat）绑定（2026-10-03）**：DS 十字键是 byte8 低半字节的**值**（中位 0x08、北 0x00、东 0x02、南 0x04、西 0x06），位域扫描无法表达（北是清位、西/斜向位互相冲突）→ 新增 `hat:VID:PID:byte:value` 绑定类型（值匹配低半字节，中性 8 拒绝=永远按住）；CaptureScan 对已知 DS 单独学 hat 候选（基线学静息值→偏转持续 →回中确认），byte8 高半字节面键仍按位扫；HidSource 支持 hat 匹配（方向专属：滑到别的方向=松开）。
- **L1/R2 捕获不到（2026-10-03）**：肩键/扳机习惯快速点按，旧的统一 0.25s 按住门槛把真实点按当"闪烁"拒绝。修复 = 按设备差异化时机：已知 DS 基线 0.8→0.5s、按住门槛 0.25→0.12s（候选空间已只剩纯按钮位，安全）；byte10 位掩码收紧到 0x07（bit3-7 是厂商计数区，永远不是按键）。其他设备维持 0.8s/0.25s。
- **TTS 朗读规范化 + STT 进 UI（2026-10-03）**：新增 `tts_text.py`（圈速/差距/温度/P 名次/百分比/斜杠 → 可读中文；教训：Python re 的 \b 把 CJK 当 word 字符，无空格串"胎温97C"匹配不上，必须显式 lookaround），接线在 speech._do_synthesize（仅发音转换，原文保留）。STT 六个键（PROVIDER/LOCAL_MODEL/LOCAL_THREADS/API_KEY/BASE_URL/MODEL）首次进 config_schema（voice 组，功能设置可见，key 掩码）；AI 设置面板重构：服务商预设下拉（DeepSeek/硅基流动/OpenAI/Kimi/Qwen/Ollama/自定义，选中即填 URL+模型）+ 语音识别预设（本地默认 / 硅基流动 SenseVoice / OpenAI / 自定义），保存经 /api/settings 落 .env。

### 实测记录：DualSense（VID_054C PID_0CE6）— 历史记录（当日已闭环）

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
- **决策**：按 R9 不盲目硬试偏移（避免随机猜）——这一条是对的，但"暂不接入手柄"已被 2026-10-02 的文档核对 + 硬件实测推翻，见上方"已闭环"。

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

### PTT 交互决定（B1）
- **取消局内双击静音**。原因：toggle 模式下“按一下开始、再按一下结束”的两次短按
  会被误判为双击，本该停止录音却切成了静音（实测发现）。
- 现在：toggle 模式**录音中按下必为停止**；空闲短按=开始；长按无动作。
- **静音只在配置页调整**（`RADIO_QUIET_POLICY`），局内不再快捷切换。
- `PTT_DOUBLE_TAP_WINDOW_MS` 保留以兼容旧 `.env`，但已不再生效。

