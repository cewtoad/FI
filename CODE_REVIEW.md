# F1 Race Engineer 代码评审报告

- **评审日期**：2026-09-21
- **评审对象**：`cewtoad/FI` @ `4d13c89`（分支 `arena/01a0c495-fi`）
- **评审方式**：全部模块通读；离线运行 8 个自测脚本（全部通过，含 Python 3.11.2 环境）；对可疑路径写了 3 个一次性复现实验验证（未留在仓库中）
- **约束**：本轮只评审不改代码。本文件是唯一新增产物。

**代码规模**

| 部分 | 行数 | 说明 |
|---|---|---|
| 应用层（根目录 `*.py`，不含测试） | ~3,300 | receiver / state / summariser / AI / UI / 录制 |
| 测试（`tests_*.py`） | ~900 | 自包含脚本，非 pytest 风格 |
| `lib/`（复用 pits-n-giggles + 裁剪） | ~15,800 | 其中 `f1_types` 约 14,500 行 |

**总体印象**：这是一个方向正确、克制且文档诚实的项目。"AI 不进实时回路、先压缩再喂模型、单包异常不致命"三条设计原则确实贯彻到了代码里。主要问题集中在三处：**① 一个可复现的接收循环崩溃路径；② web 模式下"asyncio 接收线程 + ThreadingHTTPServer 线程"无锁共享状态的并发纪律缺失；③ 解析了却不用的大包（EVENT/MOTION 等）造成的 CPU 浪费与功能缺口**。工程质量侧最大的缺口是没有根目录 LICENSE 文件和自动化测试。

---

## 一、数据流与模块依赖关系

### 1.1 分层依赖（实读代码验证，非照抄 README）

```
F1 游戏 (UDP 127.0.0.1:20777, 20~60Hz)
   │
   ▼
lib/socket_receiver/udp_receiver.py      UdpTransport：非阻塞 socket + loop.sock_recvfrom
   │  (回调注入，await 逐包同步处理)
   ▼
receiver.py  TelemetryReceiver
   ├─ lib/telemetry_manager/factory.py   PacketParserFactory：24 字节头 → 17 种包类
   ├─ lib/telemetry_manager/frame_gate.py SessionFrameGate：帧单调 + 同帧同类型去重
   └─ state.process(packet)              异常包容（try/except 计数，不上抛）
   │
   ▼
state.py  TelemetryState（核心聚合器，单写者）
   ├─ lib/delta/manager.py               LapDeltaManager：距离-时间插值 vs 最快圈
   ├─ lib/fuel_rate_recommender.py       每圈末油量 → 油耗率/富余圈数/完赛预测
   ├─ lib/rolling_history.py             圈速环形历史
   ├─ latest{session,lap,car,status,damage,car2,history,time_trial,position_context}
   ├─ leaderboard[]（LAP_DATA × PARTICIPANTS × CAR_STATUS 三表 join，每次到达重建）
   └─ events[]（由 leaderboard 位置差分推导，非游戏 EVENT 包）
   │
   ▼  snapshot()（注意：返回的是活引用，非拷贝，见 §3）
summariser.py  facts / notes / lap_history / leaderboard / recent_events（纯计算）
   ├─→ console_ui.py（终端面板）
   ├─→ webui.py /api/state（1s 轮询）
   └─→ engineer.py → prompts.py → ai_client.py(DeepSeek, urllib) → /api/ask 回答
recorder.py（由 /api/state 轮询驱动落盘）→ report_txt.py（同步刷 TXT）
```

**依赖方向总体干净，无循环导入**。有一个例外：`lib/telemetry_manager/factory.py:32` 为了一个无人使用的 `telemetry_transport_factory()`（第 134 行）import 了 `lib.socket_receiver`，让"解析工厂"反向依赖"传输层"，层级被污染（pits-n-giggles 原架构的残留）。

### 1.2 两种运行拓扑（理解线程安全问题的关键）

| 模式 | 进程内并发结构 | 状态访问 |
|---|---|---|
| `run.py`（终端） | **单线程 asyncio**：`receiver.run()` 与 `_panel_loop()` 在同一事件循环 | 顺序执行，天然安全 |
| `run.py --web`（推荐模式） | **1 个 asyncio 接收线程**（`webui._receiver_thread` 自建事件循环）+ **ThreadingHTTPServer 每请求一线程** | 跨线程无锁共享 `TelemetryState` / `Engineer` / `SessionRecorder` |

即：**项目自称"单进程"，web 模式实际是"单进程多线程 + 双并发模型（asyncio + threading）"**。console 模式没有的并发问题，全部集中在 web 模式（详见 §3）。

### 1.3 关键数据通路细节（正确性还不错的部分）

- **焦点车**：`_resolve_focus_car()` 优先 `spectatorCarIndex`（观赛模式），否则 `playerCarIndex`，实现干净。
- **油耗分桶**：`_on_car_status` 把油量按"最近一次 lap-data 的圈号"分桶，跨线后上一桶即该圈末油量，喂给 `FuelRateRecommender`；`_fuel_recorded` 集合防重复入账。思路正确，flashback 场景有防护。
- **delta**：`record_data_point` 自带"距离回退即重写时间线"语义，能在不显式处理 flashback 的情况下自愈（代价见 §6 死代码条目）。
- **会话切换**：`note_header()` 以 sessionUID 变化触发 `_reset_for_new_session()`。
- **AI 省.token**：facts 只传非空值、排行榜只传前 3 + 玩家邻车、历史裁 4 条——三段式（facts/notes/events）快照文本设计是本项目最有价值的抽象。

---

## 二、架构缺陷与潜在 Bug（按严重度）

### BUG-1（P0）：一个旧格式 UDP 包即可杀死整个接收循环 — 已复现

`lib/telemetry_manager/factory.py:103` 对 `packetFormat < 2023` 的包 **raise** `UnsupportedPacketFormat`（`:113` 的 `UnsupportedPacketType` 同理）。而 `receiver.py:97-98` 的 `_handle_raw()` 调用 `self.factory.parse(raw_packet)` **没有 try/except**——`state.process()` 有异常包容，`parse()` 的载荷解析段也有，唯独这两个格式守卫是抛出的。

后果链（实验验证）：

- 用户把游戏内"UDP 赛制"设为 2022 或更老（README 恰恰要求用户手选这一项）→ **每一个包**都抛异常；
- console 模式：异常沿 `UdpTransport.run() → receiver.run() → asyncio.gather` 传播，**整个程序崩溃**；
- web 模式：接收线程死亡（仅一行 error 日志），Web 面板继续运行但遥测永久冻结，且徽章仍显示"比赛中"（见 BUG-8）。

复现（已实测）：向接收端口发一个 `packetFormat=2022` 的 124 字节包，`receiver.run()` 任务立即以 `UnsupportedPacketFormat` 终止。

这直接违反 DESIGN.md 自己立的原则"异常不致命：单包解析失败只丢弃该包"。修法很直白：`_handle_raw` 包一层 try/except 计数丢弃，或让 factory 对这两类也走 `return None + last_failure_reason` 通路。

### BUG-2（P1）：web 模式静默忽略 `--bind-ip`

`run.py` 定义了 `--bind-ip` 并在 console 路径传入，但 `webui.serve()`（`webui.py:291`）硬编码 `bind_ip="127.0.0.1"`。推荐入口 `--web` 下，该参数是**无效谎言**。当游戏跑在另一台机器/主机（玩家常见：游戏机广播到 PC）时，用户按文档传 `--bind-ip 0.0.0.0` 不会生效，且没有任何警告。同时 `--mode/--interval/--json` 在 web 路径也被静默忽略。

### BUG-3（P1）：无效圈（切弯/出界）可被当成"最快圈"基准

`state.py:269 _on_lap_completed()` 只要 `last_lap_ms > 0` 就 push 进 `lap_times_ms` 并可能更新 `_best_lap_ms` / `delta.set_best_lap()`，**从不检查 `m_currentLapInvalid`**。计时赛里几乎每圈出界都会产生无效圈，于是"vs 最快圈 delta"可能以一条切弯圈为基准，AI 据此给出完全错误的节奏判断。而权威数据其实已经在手上：`SESSION_HISTORY` 包（`latest["history"]` 里有 `valid` 位和 `bestLapTimeLapNum`）、TIME_TRIAL 包（`personal_best_ms`），项目自己解析并存储了，却没有用来校准 `trends.best_lap_ms`。这是"总结层是全项目核心"承诺下最伤正确性的一个洞。

### BUG-4（P1）：EVENT 包（packet 3）被完整解析后直接丢弃

`state._dispatch()`（`state.py:141`）的 if/elif 链**没有任何 EVENT 分支**。而 `packet_3_event_data.py`（1,814 行，项目里第二大的解析器）已经把 `OVTK`（含超车双方 vehicleIdx）、`PITL`、 penalties、`SST`、退赛等事件解析成了结构化对象。现状：

- 事件全靠 `_rebuild_leaderboard()` 里 1Hz 级别的位置差分反推（`state.py:526`）——两位置互换、一圈内多次攻防、同帧多名次变化都会生成含糊或错误的文案（"上升了 N 位"）；
- AI 提示词里专门强调了【最近事件】的权威性，喂的却是二手推断；
- 解析 EVENT 的 CPU 一分没少花，产出全扔。

要么消费 EVENT（推荐，超车问答质量会质变），要么把它从 interested 集合去掉。

### BUG-5（P1）：parse-and-drop —— 7 类大包全程解析、零消费

web 模式用 `PACKETS_ALL`（17 类全开）。其中 **MOTION（60Hz、单包最大、22/24 车全量浮点）、MOTION_EX、CAR_SETUPS、TYRE_SETS、FINAL_CLASSIFICATION、LOBBY_INFO、LAP_POSITIONS** 在 `_dispatch` 中无分支，解析完即丢。MOTION 是全部包型中频率×体积的乘积最大者，等于每秒为垃圾数据付出最贵的 struct.unpack。顺带的功能损失：FINAL_CLASSIFICATION 本可为录制报告提供权威终局榜（现在 `final_leaderboard` 只是最后一次轮询的活表快照）。另注意 console 默认 `timetrial` 模式订阅集不含 PARTICIPANTS，排行榜车手名退化为 `carN`，与 web 模式行为不一致。

### BUG-6（P1）：events 无时间戳、无 TTL、不落盘

`_record_position_event`（`state.py:526`）只记 `seq/kind/from/to/text`。`events[-20:]` 永不过期（直到换会话），提示词取最后 3 条当作"刚刚发生的事"。比赛末段问"刚才谁超我"，模型看到的第一条可能是 40 分钟前lap 1 的事件——时间语义完全失真。recorder 也不记录 events，TXT 报告里同样缺失。给每条事件加 `session_time`/`lap_num` 并在 prompt 渲染时带出，成本极低收益明显。

### BUG-7（P2）：`_reset_for_new_session()` 漏清字段

`state.py:99-130` 清了 `packet_counts` 但**漏了 `packet_errors`**；`session_type / track_id / total_laps` 也不清（在新 SESSION 包到达前的窗口里，快照会呈现"新 uid + 旧赛道"的混合态）。`console_ui` 和 web 的 meta 行会把上一局的解析错误继续计入本局。

### BUG-8（P2）：连接状态徽章一旦"比赛过"就永不回落

web 前端 `live = d.stats.accepted > 0 && d.summary.facts.lap`——`accepted` 是累计值，游戏退出/接收线程死亡后仍 > 0，页面永远绿着"比赛中"，数据冻结。应以"最后收到包的时间距今"定义活性（后端在 `stats()` 里加 `last_packet_age_s` 即可）。

### BUG-9（P2）：录制依赖浏览器轮询，且有并发重复风险

- `recorder.record_state()` 只在 `/api/state` 被轮询时执行——**关掉标签页，圈数就不再录制**（代码注释自知）。录制本应挂在接收路径（`receiver.on_packet` 目前在 webui 里是 None，恰好是现成的空插槽）。
- `record_state()` 对 `self.data` / `self._lap_keys` 的读-改-写**不持锁**（`recorder.py:48-115`，锁只在 `record_qa`/`_flush` 里）。两个标签页同时轮询、恰逢新圈出现时，check-then-add 竞态会产生重复圈记录。`_flush` 的 tmp 文件名固定为 `session_*.tmp`，虽然写在锁内，但与 `record_state` 的未加锁变异之间仍可能序列化出半更新状态。

### BUG-10（P2）：`Engineer` 对话历史跨会话泄漏 + 裁剪不一致

`engineer.py:15` `MAX_HISTORY=6`（3 对问答），`prompts.py:101` 再裁 `[-4:]`（2 对）——两层裁剪标准不一致（无害但混乱）。更实质的问题：`reset()` **全仓库无调用者**，webui 的 Engineer 与进程同寿命，换一局比赛后 AI 还带着上一局的无线电上下文回答。

### BUG-11（P3）：杂项小缺陷

- `PacketHeader` 定义了 `__eq__` 却没有 `__hash__`（Python 语义即 unhashable，已实测）。当前没人拿它当 dict key，属于潜伏地雷；`__ne__` 在 Py3 里也是多余的。
- `ai_client.load_dotenv()` 用 `utf-8` 读文件：Windows 记事本存出的 **BOM** 会让首行 key 变成 `\ufeffDEEPSEEK_API_KEY`，表现为"key 明明填了却说没配置"，对目标用户群（Windows 玩家）是高频坑；`KEY="value"` 带引号也不会剥。
- `README` 称"16 种 packet"，实际 `F1PacketType` 有 17 个成员（0–16）。
- UDP 端口被占用时（上次 `启动.bat` 的 taskkill 按窗口标题匹配失败就会留下占端口的孤儿进程），`UdpTransport.__init__` 抛裸 `OSError` 堆栈，无友好提示。
- `capture_live.py` import 了 `signal` 未使用。
- `report_txt.render_file()`、`SessionRecorder.summary_path()`、`state.fuel_per_lap / tyre_wear_per_lap`（定义后从未 push）、`summariser.RISING_TYRE_C`（声明未用）均为死代码。`fake_data.make_session()` 用 `inspect` 反射签名填默认值，是`PacketSessionData.from_values` 参数爆炸（30+ 参数）倒逼出的脆弱 hack。

---

## 三、异步与线程安全专项

### 3.1 console 模式：安全

接收与面板刷新在同一事件循环内顺序协程切换，`state` 无并发访问。唯一注意点：`_handle_raw` 是同步函数被 async 回调直接调用，解析+状态聚合会阻塞循环——单线程下这只是延迟不是竞态。

### 3.2 web 模式：单写多读、零锁、靠"替换而非变异"的隐性约定撑着

写者：接收线程（`state.process` 全链路）。读者：每个 HTTP 请求线程（`state.snapshot()`、`receiver.stats()`、`engineer.ask()`、`recorder.*`）。

现状没有崩溃的直接原因是几条**未被文档化的隐式纪律**：`latest` 各键整体替换不原地改、`leaderboard`/`events` 整表换引用、GIL 保证单条字节码原子。但这套约定既脆弱又依赖 CPython 实现细节：

1. **`snapshot()` 返回活引用**（`state.py:574` `"latest": self.latest`，实验确认 `snapshot()["leaderboard"] is state.leaderboard`）。任何消费者若遍历它（如今 `json.dumps` 恰好不直接遍历 `latest`，但 `dict(self.drop_reasons)`（`receiver.py:119`）和 `dict(self.packet_counts)` 会在写者并发插入新键时抛 `RuntimeError: dictionary changed size during iteration`——GIL 下窗口极小、free-threaded 3.13t 下必炸）。
2. **TOCTOU**：`snapshot()` 里 `if self.fuel is not None: ... self.fuel.curr_fuel_rate`——若恰逢新会话 reset 把 `fuel` 置 None，读属性抛 `AttributeError` → `/api/state` 500。同理 `self.delta`。
3. **`Engineer.history`**：`ask()` 从任意请求线程 append + 重切片，多标签页并发提问会交错出脏上下文。
4. **`SessionRecorder.record_state`**：见 BUG-9。
5. **接收线程死亡无自愈**：`webui._receiver_thread` 捕获后仅记日志，主服务照常跑，叠加 BUG-8 用户无从感知。

**推荐修复模式（一句话版）**：与其给所有东西加锁，不如把"冻结快照"职责移到写者侧——接收线程以固定节奏（如 2Hz）构建一次性深拷贝的 snapshot 存入带锁的单槽变量，HTTP 层只读冻结件；`Engineer.ask` 与 `recorder` 各自一把小锁。这样把 N 处竞态归约为 1 个明确的所有权边界，且 console/web 两模式共用同一不变量。

### 3.3 阻塞边界

- `DeepSeekClient.chat`（urllib，timeout 30s）在请求线程内同步阻塞：线程模型下可接受，但**无并发上限**（多标签页可同时打满 API）；前端虽 disable 按钮，服务端无防护、无 rate limit、`question` 长度无上限（Content-Length 全量读入）。
- `_flush` 每次 QA 都全量重写 JSON+TXT 并渲染报告，数据大了以后会拖慢 ask 响应（当前量级无碍）。

---

## 四、异常处理专项

**做得好的**（值得肯定，这些是真实防御而非装饰）：

- `state.process()` 的 try/except + `packet_errors` 计数 + 按 packetId 归因，正是"游戏更新带来未知字段"的正确姿势；
- factory 载荷解析段捕获了 6 类异常并携带 `last_failure_reason` 供统计；
- `engineer.ask()` 失败降级为 `[engine error]` 文案不炸 UI；`chat()` 对 reasoning 模型"只吐思考不吐正文"的边界有专门处理，`HTTPError` 读 body 给出可读 detail；
- `record_qa` 失败不影响问答主流程。

**问题**：

1. BUG-1 的两个 raise 逃逸（唯一的致命路径）；
2. **静默吞异常**三处：`webui.do_GET` 里 `record_state` 的 `except Exception: pass`、`recorder._flush` 里 TXT 渲染的 `except Exception: pass`、`capture.py` 的 lap 探测——都没有留下任何日志，出问题时零线索。至少 `logger.debug(..., exc_info=True)`；
3. 日志默认 `WARNING`，连 `Listening on ...` 都是 INFO，普通用户排障时什么也看不到；`webui` 用 `print` 直出两条提示，与 logging 体系割裂；
4. `run.py --web` 的 `serve_forever` 没包 `KeyboardInterrupt`，Ctrl+C 甩裸堆栈。

---

## 五、"单进程 + 标准库（无 Flask/FastAPI）"设计评估

**结论：对这个产品的目标场景，该决策是正确的，且执行质量在中位数以上；真正的债务不在"没用框架"，而在并发模型混用且无共享状态纪律。**

支持论据：

- **负载画像极小**：入向 UDP ≈ 60Hz × ≤1.5KB ≈ <1Mbps；出向 HTTP = 1Hz 轮询 + 偶发问答，单客户端。`ThreadingHTTPServer` 的每请求一线程在这个量级绰绰有余；stdlib `http.server` 的真实短板（无 WebSocket/SSE、无中间件生态、手动解析路由）这里一个都没踩到。
- **目标用户是 Windows 玩家**：`py -3.12 run.py --web` 零安装即跑，没有任何 pip/venv 步骤——换 Flask 后"装不上依赖"的求助只会更多。`ai_client` 用 urllib 而非 requests 同理。
- **对比原项目**：pits-n-giggles 的 launcher+多进程+ZMQ 对"一个问答面板"确实过重，裁剪判断准确。
- 该决策也**没有妨碍**可测试性：测试可以直接驱动 `TelemetryReceiver`/`TelemetryState`，无需起服务器。

需要诚实面对的代价（均为"何时该重新考虑"的触发器）：

| 触发器 | 说明 |
|---|---|
| 想做推送（实时刷新/语音流） | 轮询升级为 SSE 时，手写 http.server 会开始疼；届时可只把传输层换成 `asyncio` 原生实现，仍无需 Flask |
| 多客户端/局域网共享面板 | 线程模型 + 无鉴权 + 无 Host 校验（当前仅 127.0.0.1 绑定兜底，DNS rebinding 可穿透读 `/api/state`）需要补一层框架才能便宜地做对 |
| 并发 ask 变多 | 需要限流/队列——无论什么框架都得自己写 |
| 3.13t free-threaded | §3 的无锁假设失效，必须先补锁纪律再说 |

一句话：**不要换框架，先把"谁在哪个线程碰哪块状态"写成显式契约。**

---

## 六、代码质量与可维护性

### 6.1 lib/ 复用卫生：总体合格，两处待办

- 每个复用文件保留了 MIT 许可头与上游署名，`__init__.py` 说明了裁剪范围，README/DESIGN 有致谢——**做法规范**。
- 待办 ①：**仓库没有根 LICENSE 文件**（README 宣称 MIT，仅 lib 文件头有许可文本）。对一个公开仓库这是合规硬缺口：本仓库自有代码（state/summariser/webui 等 ~3,300 行，含对上游行为的实质修改）目前处于"无许可证"默认状态，他人依法不可复用。补一个 MIT LICENSE + 一份 NOTICE 说明哪些目录来自 pits-n-giggles 即可。
- 待办 ②：上游同步策略未记录。`lib/` 已有本地化改动（如 `F1RawValueEnum` 这类明显是本项目/近期演进加入的容错机制），一旦想拉上游新格式支持，没有边界清单会很难 merge。建议在 lib/README 或 DESIGN 里列"改过哪些文件"。

### 6.2 测试：数量可观、形态不可持续

- 8 个离线脚本全部通过（且在 3.11.2 上也通过，README 标称 3.12/3.13 偏保守，`__future__ annotations` 用法使其兼容面更宽）——覆盖了 UDP 全链路、2026 格式、观赛焦点、油耗、排行榜、录制，**对这个体量已属用心**。
- 但 `tests_*.py` 命名**不在 pytest 默认发现规则内**（`test_*.py` / `*_test.py`），等于零自动化；`tests_ai.py`/`tests_engineer.py` 依赖真实 API key，`tests_udp/full/recorder` 绑真实端口，全部无法进 CI；无 GitHub Actions、无 pyproject.toml（哪怕只为声明 Python 下限和挂 ruff 配置）、无 LICENSE（见上）。
- 断言写在了 `print` 流程里，失败模式是脚本报错而非清晰的用例失败。

### 6.3 结构与风格

- 根目录的 `__init__.py` 与平铺的脚本式绝对导入（`from state import ...`）互相矛盾：目录像个包，行为是个脚本目录。要么去掉根 `__init__.py`，要么收进 `src/f1_tr/` 包用相对导入。顶层模块名 `state`、`receiver`、`prompts` 过于通用，未来任何 `pip install` 的同名包都会被遮蔽。
- 类型注解风格混用（`Dict/List/Optional` 与 `dict/list | None`、`error_logger: Optional[Any]` 应为 `Optional[Logger]`）；`summariser`、`report_txt`、`capture_live` 各写了一份 `_fmt_ms`；`state._dispatch` 的 if/elif 链适合改字典分发表（顺带就能暴露"EVENT 没人处理"这类缺口）。
- 阈值（`HOT_TYRE_C`、`MIN_FUEL_KG`、事件保留条数、历史长度）散落各类且 DESIGN 已自知"待配置化"——建议至少集中到一个 `config.py` 常量模块。
- 前端：`esc()` 转义覆盖了 `<>&`，驱动名/回答注入 innerHTML 是安全的；`poll` 的 `catch(e){}` 静默失败无重连提示；这些在单文件内嵌 UI 的定位下可接受。

---

## 七、改进清单（按优先级排序）

> 每项含理由；P0=必须马上修，P1=下个版本，P2=近期排期，P3=顺手做。

| # | 级别 | 改进项 | 理由 |
|---|---|---|---|
| 1 | **P0** | `_handle_raw` 包 try/except（或 factory 两处 raise 改为 return None），把格式异常纳入 `drop_reasons` 计数 | 唯一可复现的整机崩溃路径（BUG-1，已实验证实）；游戏内"UDP 赛制"选错一档即触发，README 还要求用户手选该项；违背自家"异常不致命"原则 |
| 2 | **P0** | 补根目录 LICENSE（MIT）+ NOTICE（标注 lib/ 来源与改动） | 公开仓库宣称 MIT 却无许可证文件，法律上本仓库自有 3,300 行处于"保留所有权利"状态；OSS 合规硬要求，五分钟工作量 |
| 3 | **P1** | 建立跨线程状态契约：接收线程侧定时产出冻结深拷贝 snapshot（单槽+锁），HTTP 层只读冻结件；`Engineer.ask`/`recorder` 加小锁 | web 模式全部竞态（§3.2：活引用、TOCTOU、dict 拷贝、history、录制重复圈）的根因是"无所有权边界"；一次重构归约 N 个隐患，也解锁 free-threaded Python |
| 4 | **P1** | `webui.serve()` 接受并透传 `bind_ip`；无效 CLI 参数改为显式报错或警告 | `--bind-ip` 在推荐模式下是静默谎言（BUG-2），游戏在另一台设备的场景直接不可用 |
| 5 | **P1** | 无效圈过滤：`_on_lap_completed` 检查 `m_currentLapInvalid`；`best_lap` 基准改用 SESSION_HISTORY 的 `bestLapTimeLapNum` / TT 包校准 | 切弯圈污染"最快圈 delta"直接污染 AI 节奏类回答的正确性（BUG-3）；权威数据已在手上未用，属低成本高收益 |
| 6 | **P1** | 消费 EVENT 包：`_dispatch` 加分支，把 OVTK（含双方 vehicleIdx）、进站、罚时写入 `events`；或至少从 interested 集合剔除 7 类无消费包型 | 解析了却丢弃 = CPU 白付 + 功能缺口双重损失（BUG-4/5）；OVTK 能让"谁超我"类问答从位置差分猜测升级为权威事实，正是本产品卖点 |
| 7 | **P1** | events 加 `session_time`/`lap_num`，prompt 渲染带时间；recorder 落盘 events | "最近事件"的时间语义目前完全失真（BUG-6），AI 提示词却声明其权威 |
| 8 | **P2** | 录制挂到 `receiver.on_packet`（或独立定时器）而非 /api/state 轮询 | 关标签页即丢圈数（BUG-9）；on_packet 目前在 web 模式是空插槽，接线成本极低 |
| 9 | **P2** | `stats()` 增加 `last_packet_age_s`，前端徽章改用它；接收线程死亡时页面给出明确断连态 | "比赛中"徽章永不回落 + 线程静默死亡 = 用户面对冻结数据毫无感知（BUG-8） |
| 10 | **P2** | `_reset_for_new_session` 补清 `packet_errors` 及 session 元数据；`Engineer.reset()` 在 uid 变化时调用（或 history 带 uid 失效）；统一 MAX_HISTORY 与 prompts 裁剪 | 跨会话脏数据三类来源（BUG-7/10），都是一两行修复 |
| 11 | **P2** | 测试改造：更名 `test_*.py` 接入 pytest，网络/API 用例加跳过标记，加 GitHub Actions（跑离线集 + ruff） | 现有 8 个脚本质量不错但零自动化，名字规则导致 pytest 根本发现不了；OSS 无 CI 会持续腐化 |
| 12 | **P2** | 加 `pyproject.toml`（声明 Python >=3.11、ruff/mypy 配置）；异常吞咽处补 `logger.debug(exc_info=True)`；webui Ctrl+C 友好退出 | 工程化基线（§4.2/§6.3）；排障可见性 |
| 13 | **P3** | 消死代码：`telemetry_transport_factory`（连带解除 factory→socket_receiver 反向依赖）、`handle_flashback` 接线或删除、`fuel_per_lap`/`tyre_wear_per_lap`、`RISING_TYRE_C`、`signal` import、`render_file` 等 | 死代码误导维护者对系统行为的判断（如以为 flashback 已处理）；反向依赖污染分层 |
| 14 | **P3** | `load_dotenv` 用 `utf-8-sig` + 剥引号；UDP 端口占用给出友好报错；README "16 种"改 17；`PacketHeader` 补 `__hash__` | 面向 Windows 玩家的小坑清单（BUG-11），单个小修 |
| 15 | **P3** | 阈值集中进 `config.py`；`_fmt_ms` 三处合一；`_dispatch` 改字典分发；`/api/ask` 限长度 | DESIGN"规划中"已自知；纯可维护性 |

### 优先做的前三件事（如果只做三件）

1. **修 BUG-1（半小时）**——这是用户按文档操作就能触发的崩溃。
2. **补 LICENSE + NOTICE（十分钟）**——开源合规的门槛问题。
3. **冻结快照模式（半天）**——它同时消除 web 模式所有已知竞态，是后续任何功能（SSE 推送、语音、多面板）的地基。

---

## 附：本轮验证记录

- 离线测试：`tests_offline / udp / full / fuel / leaderboard / overtake / spectate / 2026` 全部通过（Python 3.11.2）。
- 实验 A：`packetFormat=2022` 单包 → `receiver.run()` 任务以 `UnsupportedPacketFormat` 死亡（BUG-1 证实）。
- 实验 B：`snapshot()["latest"]` 与 `state.latest` 为同一对象、`snapshot()["leaderboard"] is state.leaderboard`（活引用证实）。
- 实验 C：`hash(PacketHeader(...))` 抛 `TypeError: unhashable type`。
