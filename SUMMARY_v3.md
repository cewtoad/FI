# F1 Race Engineer — 迭代记录 v3（2026-10-02 ~ 10-03）

> 本轮主题：**手柄 PTT 全链路落地 + 输入源模块化 + AI/STT 设置进 UI + 语音双层优化（TTS 规范化 / 本地 SenseVoice）**。
> 测试基线：258 tests collected，离线全套 exit 0（本机实测）。
> 设计原则零违反：被动只读、AI 不进实时回路、stdlib 优先、依赖白名单（新增 sherpa-onnx 为 stt_lib 可选依赖，不引 torch）。
> 文档同步：KNOWN_ISSUES.md / README.md 已随每项更新。

---

## 1. 手柄（DualSense）PTT —— 从"探测失败"到全功能

**研究结论**：十字键/按钮位有权威文档（[nondebug/dualsense](https://github.com/nondebug/dualsense)），
无需"探测发现"。实测（f1tr_hidtest 脚本）与文档 100% 吻合：USB 报文 0x01 共 64 字节，
byte8=hat+面键、byte9=L1/R1/L2/R2/Create/Options/L3/R3、byte10=PS/触摸板/静音；
R1=byte9/0x02、✕=byte8/0x20 等逐一硬件验证。

**落地**（输入源当时在 input_sources.py，后迁入 inputs/ 包）：
- `HidSource` 补全：单次数组注册 Gamepad(0x01/0x05)+Joystick(0x01/0x04)
  （`RegisterRawInputDevices` 整体替换，分次注册只剩最后一个——旧探测工具失败根因之一）；
  设备路径过滤 VID/PID；短报告忽略防假释放；`run_blocking` 主线程模式与键盘一致。
- `voice_main` 走 `make_source` 统一分派，hid/hat 绑定不再静默回退键盘。
- 实测修复的两个 bug：
  ① `GetRawInputDeviceInfoW(RIDI_DEVICENAME)` 不支持 NULL 缓冲两段式查询——
     预分配缓冲单次调用（否则所有报告被设备过滤器静默拦掉）；
  ② 同进程重复捕获秒退——`PostQuitMessage` 的 WM_QUIT 残留线程消息队列
    （keep-alive HTTP 线程复用）+ 固定窗口类名路由到已 GC 回调；
     改 PeekMessage 泵 + 每次捕获唯一类名 + DestroyWindow 同步拆除。
- **按键捕获向导** `capture_hid_binding`：基线学静息位地板 → 0→1 持续 ≥门槛 →
  **松开确认**（bind-on-release，类游戏改键；同时拒绝长停留的计数器假捕获——
  曾空手复现 `hid:…:13:0x1`）。已知 DualSense 只扫按钮字节 8-10，byte10 掩码 0x07；
  时机按设备差异化（DS：基线 0.5s / 按住 0.12s——肩键快速点按不再被拒）。
- **十字键（hat）绑定**：DS 十字键是 byte8 低半字节"值"（中位 0x08/北 0x00/东 0x02/南 0x04/西 0x06），
  位域扫描无法表达 → 新增 `hat:VID:PID:byte:value` 绑定类型（中性 8 在 parse 层拒绝；
  方向专属语义：按住北滑到东=松开）。捕获向导对 hat 单独学候选（回中确认）。
- **UI**：功能设置 PTT 按键行 = 设备下拉（键盘/手柄）+ 一键捕获 +
  **实时人话解读**（`= 手柄 R1` / `= 手柄 十字键 右` / `= 键盘 小键盘 +`，中英双语），
  新增 `GET /api/binding_name`；`/api/bind` 支持 `{"device": "hid"}`。

## 2. 输入源模块化（inputs/ 包）

`input_sources.py`（300+ 行单文件）拆为：

```
inputs/base.py        InputSource 基类（按压闩锁 = 边沿检测 + 去抖）
inputs/bindings.py    kb:/hid:/hat: 解析与格式化 + describe_binding 人话解读（纯函数）
inputs/keyboard.py    KeyboardSource + capture_keyboard_binding
inputs/hid.py         HidSource + capture_hid_binding + Raw Input 公共管线 + CaptureScan
inputs/__init__.py    make_source 工厂 + 公共 API
input_sources.py      兼容门面（voice_main/测试零改动）
```

build_manifest：`inputs/` 进 RESOURCE_DIRS 与 hidden-imports。enable_hid 门控默认放开
（布局已硬件确认），`enable_hid=False` 仍可显式关。

## 3. AI / STT 设置进 UI（预设式）

- **STT 六键首次进 config_schema**（voice 组）：STT_PROVIDER(local/cloud/auto/off)、
  STT_LOCAL_ENGINE(sensevoice/whisper)、STT_LOCAL_MODEL(tiny/base/small/medium)、
  STT_LOCAL_THREADS(1-8)、STT_API_KEY(secret 掩码)、STT_BASE_URL、STT_MODEL。
  此前只能手改 .env。
- **AI 设置面板重构**：AI 服务商预设下拉（DeepSeek/硅基流动/OpenAI/Kimi/Qwen/Ollama/自定义，
  选中即填 URL+模型）+ 语音识别预设（本地默认 / 硅基流动 SenseVoice / OpenAI Whisper / 自定义）。
  保存：LLM 走 /api/llm + STT 走 /api/settings；提示重启语音模式生效。

## 4. TTS 朗读规范化（tts_text.py）

`normalize_for_tts` 在合成时刻转换（speech._do_synthesize）：队列/日志/录制保持原文。
`1:31.204`→`1分31秒204`、`2.1s`→`2.1秒`、`97C`→`97度`、`P5/P12`→`P五/P十二`、
`24%`→`百分之24`、数字间 `/`→`、`。
**教训**：Python re 的 `\b` 把 CJK 当 word 字符，`胎温97C`（无空格）永远匹配不上——
必须显式 lookaround。7 条测试含 arbiter 接线测试。

## 5. 本地 STT 换 SenseVoice-Small（sherpa-onnx，免 torch）

- 新 `LocalSenseVoiceSTT`：sherpa-onnx OfflineRecognizer.from_sense_voice，
  模型在 `stt_models/sensevoice/`（model.int8.onnx + tokens.txt），
  `tools/download_sensevoice.py` 下载（HF_ENDPOINT 镜像，默认 hf-mirror）。
- 非自回归架构：**本机 A/B 实测（5.1s 样本，piper_preview.wav）**：
  SenseVoice **0.51s（RTF 0.100）** vs whisper-small int8 **6.77s（RTF 1.327）**
  ——**快 13 倍**；模型加载 1.8s vs 26s。该样本为 TTS 合成音，两者文本质量相当
  （合成音本就有糊音），真实麦克风语音按官方基准 SenseVoice 中文优于 Whisper-Large，
  待实际驾驶场景验证。
- `STT_LOCAL_ENGINE` 切换（默认 sensevoice），**未安装时自动回退 faster-whisper**，
  语音路径永不因半安装状态失效。load() 带锁（修掉 whisper 同款双重加载竞态）。
- 支持语音模式 float32 PCM 与 PCM-WAV 字节；SenseVoice 富标签（<|zh|> 等）自动剥离。
- 云端对照（价格调研 2026-10）：硅基流动 SenseVoice API 上线时免费（OpenAI 兼容，
  填现有 STT_BASE_URL/STT_API_KEY 即用）；Groq whisper-v3-turbo ~$0.04/时；
  阿里云 paraformer-v2 ~$0.000012/秒。用量下成本均可忽略，决策点是延迟与网络。

## 5b. Xbox 手柄适配（无硬件，按已知机制先行）

Xbox（VID 045E）走微软 XInput 驱动栈，Raw Input 同样以 usage 0x01/0x05 送报文
（~20 字节，仅状态变化时发帧），按键为位域。适配内容（均为设备无关的通用强化）：
- `CaptureScan.KNOWN_VID`：VID 级配置层（Xbox 全系 PID 生效）——基线 0.5s / 按住 0.12s，
  扫描区维持通用 4-15（Xbox 按键字节布局未经硬件确认，不硬编码）。
- **pending 卡死保护**：候选按住 >2.5s 不松（计数器/时间戳位的典型特征）→
  该位在本轮捕获中拉黑并继续扫描——否则会锁死整次捕获到超时。这对所有设备生效，
  也补上了通用扫描区（4-15 含滚动数据字节）的最后一块短板。
- `describe_binding` 识别 VID 045E → "Xbox 手柄 / Xbox controller" 前缀。
- 待硬件实测：报文字节布局（按键是否落扫描区）、稀疏发帧下"基线期按下 → 进静息地板，
  松开再按一次可捕获"的体验。接入后跑 f1tr_hidtest.py + 网页一键捕获即可闭环。

## 5c. 发布 v0.5.0 与 CI 首绿修复

- **打包**：全量包确认自带 STT 全套（stt_lib 整目录复制含 sherpa-onnx；
  stt_models/sensevoice 普通目录不受 HF 缓存排除规则影响）；tools/ 首次进全量包
  （udp_record/replay/download_sensevoice 是 README 给用户引用的）；
  APP_VERSION 0.5.0，exe 版本资源 0.2.0.0→0.5.0.0；.env.example 补 STT 键说明。
- **CI 首绿修复（重要根因）**：仓库根遗留的 pits-n-giggles 时代 `__init__.py`
  （骨架 docstring，__version__=0.0.1，无任何代码引用）在 GitHub Actions 上
  劫持了 `import FI`——CI 检出目录名叫 `FI`（D:\a\FI\FI），pytest 向上找
  conftest 包名时把仓库根当作包 `FI`，先执行骨架 `__init__.py` 并缓存进
  `sys.modules["FI"]`，之后测试里所有 `import FI` 都拿到骨架而不是启动器模块，
  5 个测试全挂。本地目录名叫 F1_TR 故从未复现；且 release workflow 在 v0.5.0
  之前从未真正跑过（历史 release 均为本地构建手动上传），所以一直没暴露。
  修复 = 删除该文件；已用同名目录复现并验证。CI 全绿后 Release v0.5.0 自动
  附 core 包；全量包本地构建（dist/F1Engineer-full-0.5.0-win64.zip，1.4GB，
  已校验含 SenseVoice 模型/sherpa-onnx/inputs/tts_text/tools），手动拖传到
  Release 页。

## 6. 测试

新增 `tests/test_hid_source.py`（13 条：边沿/捕获状态机/hat/设备名匹配/人话解读）、
`tests/test_tts_text.py`（7 条：含 arbiter 接线）、`tests/test_sensevoice_stt.py`（5 条：fake sherpa 密闭）、
`tests/test_web_settings.py` +2（STT schema/掩码）；`tests/test_voice_input.py` 门控断言更新。
全套 258 collected，exit 0。

## 7. 本轮未动 / 待办

- **第三轮整体审查（2026-10-02，只读）结论尚未修复**——待修清单（均已核实到 file:line）：
  - P0：profiles.py:227/282 快答路由误命中（"前面还剩多少圈"被前车路由截胡、
    rival 路由"多少"触发词过宽）；voice_main.py:202 云 STT 签名不兼容（配 key 必现识别失败）；
    webui.py:914 Origin 白名单只认 127.0.0.1（LAN 绑定 POST 全 403）；
    webui.py:923 /api/profile 绕过 local-only 且 persist。
  - P1：race_model.py:290 PitWindow 闩锁跨会话残留（S4 修复第二场复发）；
    state.py:1151 flashback 位置基线被 CarStatus 重建污染（S2 修复未闭合）。
  - P2：RADIO_GAP_MODE=on_change 未实现、RADIO_PER_LAP_CAP 无消费点、
    profiles.py:139 缺油阈值硬编码、dedup 作用域审计、语音栈竞态（_busy 提前释放/
    Piper 无超时/Whisper 双重加载——SenseVoice 的 load 已带锁）、0 秒圈样本进配速回归等。
  - 方向性建议：跨会话/flashback 统一 reset 协议；快答路由改打分制；
    用 tools/replay.py + .f1rec 建场景回放测试套件。
- 蓝牙 DualSense 报文布局不同（0x01=10B 紧凑/0x31=78B），首版仅 USB。
- 胎温阈值 110°C 仍为占位值；SignPath 签名申请；Windows Sandbox 干净机验收。
- 迭代记录惯例：每轮迭代在本目录追加/更新 SUMMARY_*.md，KNOWN_ISSUES 随改随记。

## 8. 快速验证

```powershell
cd "G:\AI WORK\F1_TR"
py -3.12 -m pytest -q                          # 258 collected, exit 0
py -3.12 -m tools.download_sensevoice          # 下载模型（首次）
py -3.12 voice_main.py                         # 语音模式：手柄 R1 PTT + SenseVoice 识别
py -3.12 run.py --web                          # 网页面板：AI/STT 预设 + 功能设置
```
