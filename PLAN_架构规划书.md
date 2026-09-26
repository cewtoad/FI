# F1 Race Engineer — 架构审计与功能扩充规划书

> 用途：交给架构审计方（Opus 5.5）作为**输入契约 + 任务书**
> 基线：`c59fabf`（GitHub = 本地）
> 角色约定：审计方**只做架构设计/接口定义/耦合与兼容规划**，不实际写业务代码
> 产出要求：审计报告 + 目标架构图 + 每功能的落点/接口契约 + 迁移策略

---

## 第 0 部分：给审计方的话

这个项目已稳定运行、有真实用户、有清晰且**不可推翻的设计原则**（见 §1.3）。你的任务**不是重构**，而是在**不破坏现有边界**的前提下：

1. 指出当前架构的耦合/边界问题（按严重度排序）
2. 设计目标架构（分层、接口契约、数据流）
3. 为 6 个新功能给出**落点方案**：加在哪个文件/层、新接口长什么样、怎么不破坏现有原则
4. 给出**兼容与迁移策略**：老用户零破坏、可分阶段落地
5. 明确**token 成本**与**实时性**约束（项目核心纪律：AI 不进实时回路）

---

## 第 1 部分：现状基线

### 1.1 数据流（五层，依赖单向）

```
F1 游戏 UDP:20777 (20~60Hz, 官方广播)
  → [接收层] receiver.py + lib/(17种packet解析/帧门/异常包容)
  → [状态层] state.py  TelemetryState
      · 实时快照 latest{}
      · 全场榜 leaderboard[]（LAP_DATA × PARTICIPANTS × CAR_STATUS join）
      · 官方 OVTK 超车事件 + 位置差分
      · 趋势 RollingHistory(lap_times/fuel/tyre_wear/lap_records)
      · 2Hz 冻结快照（深拷贝单槽+锁）★已解决的并发问题
  → [总结层] summariser.py  ★全项目核心
      · 纯计算：facts{} + notes[] + leaderboard + recent_events
      · 把 60Hz 原始流压成"几行人类可读事实"
  → [AI 层] engineer.py + prompts.py + llm_client.py
      · engineer: 本地快答路由 → 命中即返（不走 LLM）；否则走 LLM
      · llm_client: OpenAI 兼容 + make_llm 工厂 + 回退链
      · prompts: 系统提示词 + 紧凑快照文本
  → [输出层] webui / console_ui / voice_* / recorder
```

### 1.2 规模

| 项 | 值 |
|---|---|
| 应用层 | ~7,200 行（61 个 .py） |
| 复用库 lib/ | ~13,600 行（pits-n-giggles, MIT, 勿大改） |
| 测试 | 52 项（脚本式 tests_*.py + pytest 混合） |
| 打包 | core(PyInstaller onedir ~21MB) / full(embedded py + stt + model ~950MB) |

### 1.3 不可推翻的设计原则（审计的硬约束）

1. **AI 不进实时回路** —— 数值计算全本地，AI 只负责措辞
2. **先压缩再喂模型** —— summary 先算好，prompt 只塞紧凑文本
3. **被动只读** —— UDP 收包 + Raw Input 只订阅，不注入/不挂钩/不驱动
4. **单包异常不致命** —— 任何解析/状态异常被包容并计数
5. **stdlib 优先** —— HTTP/解析/LLM 全标准库；语音仅两处可选依赖
6. **扩展点工厂化** —— make_llm / make_stt / make_tts 同构，配置驱动
7. **表达层与计算层分离** —— 档位/prompt 只改措辞，绝不改 summary 数值

### 1.4 已存在的扩展点（新功能必须复用，不要另起）

- `config.py`：唯一配置中心（.env + 环境 + 运行时 overlay，带锁可持久化）
- `paths.py`：`app_root()` 兼容 源码/PyInstaller/绿色包
- `llm_client.py`：`make_llm()` 多端点 + fallback
- `audio.py`：`active_device()` 设备跟随系统 + 热切换
- `profiles.py`：档位 + `LocalRouter` 本地快答路由
- `app.py`：组合根（唯一装配处）
- `webui.py`：/api/llm /api/audio /api/profile 热切换

### 1.5 已知架构债（审计重点）

| # | 问题 | 级别 |
|---|---|---|
| D1 | **双语音栈残留**：本地栈(voice_stt/tts) 与浏览器栈(stt_client/tts_client) 两套 transcribe/设备路径 | 中 |
| D2 | **测试为脚本式**，覆盖率未量化，无 CI 实跑 | 中 |
| D3 | **打包脚本脆弱**：Compress-Archive OOM；build_release.ps1 与 FI.py 逻辑重叠 | 中 |
| D4 | **summary 是"一次性快照"**，无跨圈时间维度、无记忆 → 无法做长距离推演（→ 功能2/3 的根因） | 高 |
| D5 | **无事件驱动的主动输出**：只有"问答"，没有"工程师主动提醒"（→ 功能4 的根因） | 高 |
| D6 | **无独立配置 UI**：全靠 .env / 网页零散端点（→ 功能5 的根因） | 中 |
| D7 | **TTS 单一**：SAPI/edge 混在 tts_client，无"语音包"概念（→ 功能6） | 低 |
| D8 | events 缺 session_time/lap_num；档位路由仅 12 条正则 | 低 |

---

## 第 2 部分：F1 工程师真实工作方式（功能 2/3/4 的参照系）

> 以下为检索 FIA 规则 + F1 轮胎/策略机制后提炼的"真实工程师行为模型"，作为 AI 行为的对标基准。

### 2.1 真实 race engineer 做什么

- **不是被动问答机**，而是**持续观测 + 关键时刻主动呼叫**。典型无线电：
  - "Box, box" / "Box this lap"（进站指令）
  - "Push now, we need to cover X"（阶段目标）
  - "Your tyres are gone, manage" / "Tyre temp high, back off 2 clicks"
  - "Safety car, stay out / pit now"（窗口决策）
  - "Gap to car behind is 1.2, you're safe / he's in DRS"
- **核心能力 = 把当前状态放进"比赛全局时间轴"里推理**：
  - 不是"你现在胎温 110"，而是"按你当前退化速率，还撑 6 圈，而进站窗口在 8 圈后打开 → 建议现在保胎"
  - 不是"你落后 5.2s"，而是"你正以 0.3s/圈 追近，还有 12 圈 → 预计 5 圈后进入 DRS"

### 2.2 工程师"长距离记忆"的四个维度（→ 功能 2 直接对应）

| 维度 | 含义 | 现状缺口 |
|---|---|---|
| **圈级时间序列** | 每圈速度/退化/油量的轨迹，不是单点 | 只有 RollingHistory 存了原始值，未做"趋势语义化" |
| **阶段（stint）记忆** | 本次进站后已跑 N 圈、退化速度、预计剩余寿命 | 完全没有 |
| **对手追踪** | 前/后车的历史速度、预估窗口、是否同圈进站 | 只有瞬时 gap |
| **全局比赛模型** | 剩余圈数 × 当前节奏 → 完赛名次预测、窗口打开点 | 只有瞬时预测 |

### 2.3 2026 规则要点（影响数据语义，审计时需知）

- **主动空气动力学（Active Aero）取代 DRS**：直道模式/弯道模式，前后翼都可动
- **50-50 油电**：电机功率大增（~350kW），ERS 策略重要性上升
- **MGU-H 取消**，燃油流量改按能量（MJ/h）
- 项目已通过 `CAR_TELEMETRY_2` 读到 `active_aero_mode / overtake_available / overtake_active`，**语义需对齐新规则**

---

## 第 3 部分：6 个新功能的架构落点（审计方重点）

> 每条：目标 → 架构难点 → 建议落点 → 接口契约 → 对现有原则的冲击 → token/实时性

### 功能 1：整体模块化（便于增删功能）

**目标**：新功能"加一个文件 + 注册一下"即可，不散落改多处。

**架构建议**：
- 引入**能力注册表**（capability registry）：每个功能模块声明 `name / 依赖 / 提供的接口 / 挂载点`
- 五层之间通过**明确的数据契约**（TypedDict / dataclass）通信，而非裸 dict
- **插件式输出前端**：webui/console/voice 作为"消费者"订阅同一 summary 流

**接口契约（待设计）**：
```
FeatureModule = {
    name: str
    provides: [interface_name]
    requires: [interface_name]
    on_packet?: (packet) -> None
    on_tick?: (interval_s) -> None      # 主动输出用（功能4）
    contribute_facts?: (snapshot) -> dict
    contribute_prompt?: (snapshot) -> str
}
```

**需审计**：现有 summariser 的 `facts` 是扁平 dict，扩展会让它膨胀。是否引入"事实命名空间"（如 `tyre.* / fuel.* / strategy.*`）？如何保持 prompt 紧凑？

---

### 功能 2：长距离 / 长记忆 / 推演建议能力（★ 最高价值）

**目标**：像真工程师一样，"把当前状态放进比赛时间轴"给建议。

**核心认知**：当前 `summariser` 只做**单点快照压缩**，缺的是**时间维度的语义层**。这不是加几个字段能解决的，需要**新的一层**。

**建议架构**：在 状态层 与 总结层 之间插入 **推演层（Strategy / RaceModel）**：

```
状态层(state.py)          时间序列原始值
   ↓
推演层(race_model.py)     ★新增：把时间序列→趋势/预测/窗口
   · StintTracker      : 本次 stint 圈数、退化速率、预计寿命
   · PaceModel         : 本车/对手 的速度趋势、追近/拉开速率
   · StrategyWindow    : 进站窗口、undercut/overcut 机会
   · RaceProjector     : 剩余圈数×节奏→完赛名次/油量预测
   ↓
总结层(summariser.py)     facts + notes（现在含"推演结论"）
   ↓
AI 层                     prompt 里带趋势，AI 才推得出来
```

**接口契约（待设计）**：
```
race_model.update(snapshot) -> RaceModelState
race_model.project(horizon_laps) -> {
    pace_trend, tyre_life_est, pit_window, gap_evolution,
    projected_position, undercut_risk, ...
}
```

**关键约束**：
- 推演层是**纯计算**（符合原则1），AI 不参与
- 输出要**语义化**（"还撑6圈"），而原始序列仍保留在 state
- **必须省 token**：不能把整段序列塞 prompt，只塞**推演结论几行**

**需审计**：
- `RollingHistory` 容量固定（HISTORY_LAPS=20），长距离（50+圈）是否够/如何滚动
- 退化速率（轮胎）、油耗速率目前状态层有吗？还是只在 summariser？
- 新层放哪：`race_model.py` 独立文件？还是 state 内的 sub-module？

---

### 功能 3：AI 更像工程师 + 更多推理 + 省 token

**目标**：回答从"复述事实"升级为"结合数据的推理"，同时**更省** token。

**架构建议**：
- **分级 prompt**：快答路由（0 token）→ 模板推理（0 token）→ LLM 轻推理 → LLM 深推理
- **推理脚手架**：不靠提示词硬求 AI 推理，而是**推演层先算好"半成品结论"**，AI 只做最后语言化（既准又省）
- **按问题类型的 prompt 路由**：战术问题给战略档，事实问题给快答

**接口契约**：
```
profiles: fast / standard / deep  → 扩展为按"问题类型×复杂度"路由
prompts.build_messages(..., reasoning_context) → 注入推演结论
```

**省 token 的具体手段（需审计量化）**：
- 快答路由覆盖率提升（现在只 12 条正则）
- 推演结论用"压缩语义"（"6圈"代整段退化序列）
- history 裁剪策略（现 MAX_HISTORY × profile）

**需审计**：如何在不牺牲准确性的前提下降 token？是否引入"回答缓存"（同问题同状态直接复用）？

---

### 功能 4：数据异常监测 → 主动触发 AI 建议（★ 省 token 是关键）

**目标**：工程师**主动**提醒（不是等问）。例：胎温突升、油量异常、被追近到 DRS、进站窗口打开。

**架构建议**：
- **本地规则引擎先行**（0 token）：异常检测全本地，命中才决定是否叫 AI
- **两级触发**：
  - **L1 本地告警**（0 token）：阈值/趋势规则 → 直接 TTS/UI 播报固定话术
  - **L2 AI 增强**（花 token）：仅当"需要推理的异常"（如"为什么慢"）才调 LLM
- **防刷屏**：抑制/冷却/优先级（不然比赛中一直播报）
- **主动输出的通道**：复用输出层，新增"推送"路径（webui SSE / 语音打断）

**接口契约（待设计）**：
```
AnomalyRule = {
    id, severity, cooldown_s,
    check(prev_state, curr_state) -> Match | None,
    message?          # 固定话术(0 token)
    needs_ai?: bool   # 是否升级到 LLM
}
AnomalyEngine.evaluate(snapshot) -> [Alert]   # 2Hz 挂 on_tick
```

**省 token 的设计（核心）**：
- 默认**只走本地话术**，AI 仅用于"需要解释/建议"的少数异常
- 用**边沿触发**（状态跳变才报），不是持续报
- 冷却窗口 + 同类合并

**需审计**：主动输出如何与"用户正在问问题"互斥？语音播报优先级如何排？

---

### 功能 5：独立配置 UI 程序（★ 独立，不碰游戏）

**目标**：不落地编辑 .env，可视化调：抓哪些数据 / 用什么 AI / 提示词与风格 / STT 选择 / 触发键与方式。

**架构建议**：
- **明确边界**：这是**配置器**，不是运行时 UI；运行中是 webui（已有）
- 三个选择：
  1. 并入现有 webui（新增 /config 页）
  2. 独立 Tkinter/PyQt 小程序（读同一个 config.py）
  3. 独立 web 小页（复用 config.py + 单独 serve）
- **强烈建议复用 `config.py` 作为唯一真源**，UI 只是它的写入口

**接口契约**：
```
config.schema() -> 可配置项描述（类型/默认/范围/说明）
config.get/set_runtime(...)  # 已存在
ALLOWED_PACKETS 可配 → receiver 的 interested 集合变为可配
快捷键/触发方式 → voice_trigger 参数化（现 TRIGGER_VK 硬编码）
```

**需审计**：
- "抓哪些数据"要打通 receiver 的 `interested` 集合 → 现有 PACKETS_CONSUMED 是常量，需可变
- 触发方式（按键/长按/双击）需要 voice_trigger 支持多模式
- UI 技术选型（Tkinter 零依赖 vs PyQt6 你有基础）

---

### 功能 6：TTS 语音包切换

**目标**：可切换不同 TTS 后端/音色（SAPI / edge / 未来本地 TTS）。

**架构建议**：
- 现有 `make_tts()` 已是工厂，**方向对**，缺的是"语音包"概念
- 定义 **VoicePack**：`{ name, backend, voice_id, rate, pitch, mime }`
- `voices.py`：内置语音包列表 + 用户自定义
- tts_client 按 VoicePack 构造引擎

**接口契约**：
```
VoicePack = {name, provider, voice, rate, pitch}
make_tts(pack: VoicePack) -> TTSEngine
list_voices() -> [VoicePack]  # 供配置 UI 下拉
```

**需审计**：edge（联网）与 sapi（离线）语义差异如何统一？未来本地 TTS（如 Piper）如何接入而不改上层？

---

## 第 4 部分：审计交付清单（要求审计方产出）

1. **架构审计报告**：D1–D8 逐条 + 新发现，按严重度排序，带"影响面"
2. **目标架构图**：六层（接收/状态/**推演**/总结/AI/输出）+ 数据契约
3. **接口契约规格**：每个新 `make_*` / 数据结构 / API 端点，含类型签名
4. **6 功能落点表**：功能 → 新增/修改文件 → 接口 → 对原则的冲击 → 分阶段
5. **兼容迁移策略**：老用户零破坏的路径
6. **token & 实时性预算**：每个功能的最坏情况 token 与延迟估计
7. **风险清单**：哪些改动可能违反 §1.3 原则，如何避免

---

## 第 5 部分：审计方必须遵守的边界

- ❌ 不推翻 §1.3 七条原则
- ❌ 不让 AI 进实时回路（推演层必须纯本地）
- ❌ 不把原始高维数据塞进 prompt（省 token 是硬约束）
- ❌ 不动 `lib/`（pits-n-giggles 复用层）
- ✅ 优先复用已有扩展点（config/audio/llm_client/profiles/app）
- ✅ 新功能优先"加文件 + 注册"，而非改现有内核
- ✅ 老用户现有 .env / 工作流零破坏

---

## 附录：功能优先级建议（我方观点）

| 功能 | 优先级 | 理由 |
|---|---|---|
| 2 长距离推演 | ★★★ | 价值最高，是 3/4 的地基（先有推演层，AI 才有料可推） |
| 1 模块化 | ★★★ | 是 2/4/5/6 的工程前提，越早越好 |
| 4 主动建议 | ★★★ | 体验质变，且能省 token（本地规则先行） |
| 3 推理增强 | ★★ | 依赖推演层，可在 2 之后 |
| 5 配置 UI | ★★ | 提升可用性，独立性强可并行 |
| 6 TTS 语音包 | ★ | 独立小改，最易 |

**建议实施顺序**：1（模块化骨架）→ 2（推演层）→ 3+4（推理与主动建议，共享推演层）→ 5（配置 UI）→ 6（语音包）
