# F1 Race Engineer — 第二轮审查修复总结

> 生成日期：2026-09-27
> 提交范围：`97e9ce0..029d238`（本轮 2 个提交）
> 测试基线：审查前 179 passed / 4 skipped → **修复后 199 passed / 4 skipped**（全量 `py -3.12 -m pytest`，本机实测）
> 设计原则零违反：AI 不进实时回路、被动只读、stdlib 优先、依赖白名单、`lib/` 未改动。

---

## 1. 一句话结论

按第二轮整体审查（4 个只读子代理分区扫查全部核心代码）的结论，修复了 **4 个严重、12+ 个中等、约 20 个轻微**问题，补齐 20 条回归测试；所有修复均通过全量 pytest。

---

## 2. 提交

| 提交 | 内容 | 规模 |
|---|---|---|
| `36fdee1` | **基线**：把上一轮会话遗留在工作区的 20 个文件改动（pipeline 拆分、CI 构建防漂移、端口探测 SO_EXCLUSIVEADDRUSE、配置页掩码密钥、Origin 校验等）先行提交，避免与本次修复混杂 | 23 文件，+964/-204 |
| `029d238` | **本轮全部修复** + 20 条回归测试 + KNOWN_ISSUES 记录 | 33 文件，+1008/-187 |

---

## 3. 严重项修复（4）

### S1 `speech.py` — 播报线程异常隔离 + 优先级合成池
- **原问题**：arbiter 播报线程 `_run` 全程无 try/except，一条截断/损坏的 WAV 在 `_emit` 内抛 `wave.Error` 即杀死唯一工作线程且无重启 → radio 永久静默。
- **修复**：pop 后整段（过期/闸门/播放/等待）包 `try/except Exception` + `dropped_failed` 计数；`_emit` 捕获放宽到 Exception。
- **附带重构**：合成池由 FIFO `ThreadPoolExecutor(2)` 改为**优先级感知的 daemon 线程 ×2**：
  - P0 安全播报不再排在两条长合成后面饿死超期；
  - 线程改 daemon——Piper g2pW 挂死不再卡住整个进程退出；
  - 删除遗留死函数 `_priority_bucket`。

### S2 `state.py` — 快照读者路径去放大（丢包根源）
- **原问题**：包流期间 `_snap_dirty` 恒真，每个 HTTP 轮询/语音问答都 `refresh_snapshot(force=True)`——在 `_state_lock` 内对 latest+排行榜+圈历史+事件做全量 deepcopy，把 UDP 接收线程卡住；读者越多丢包越多，且无单飞。
- **修复**：
  - 新增 `_publisher_at`（发布者时间戳）：接收线程活跃（2s 内有非 force 刷新）时，读者**直接复用冻结副本**（≤0.5s 旧），不再重建；
  - 无新鲜发布者副本时（直接驱动/测试/接收线程停摆）仍按需重建，保证 read-your-writes，经 `_build_lock` **单飞**（N 个并发读者只触发 1 次重建）。
- **附带**：快照 `events` 窗口 6→24（防单拍超量事件被永久丢弃）；flashback 回档时重置 `_last_position` 基线（修假"掉位"播报）；`_reset_for_new_session` 清 `packet_errors`；直道阈值从每遥测包 3 次带锁 config 查询改为 5s TTL 缓存；`add_snapshot_provider` 契约文档改为与实现一致（provider 在 `_state_lock` 内被调用、禁止 IO/回调）。

### S3 `radio_director.py` — 跨会话状态重置
- **原问题**：state 侧重置会话后事件 seq 从 1 重新计数，而 director 的 `_last_event_seq` 保留旧场最大值 → **第二场会话安全车/红旗/罚时/超车/退赛等全部事件规则整场失效**；一次性 dedup key 也跨场残留。
- **修复**：tick 检测 `snapshot["session"]["session_uid"]` 变化即清 `_fired_keys / _last_fired / _last_event_seq / _alerts_this_lap / _lap_marker / _prev_model`（`_quiet_override` 保留，属用户设置）。

### S4 `race_model.py` — PitWindow "done" 闩锁
- **原问题**："done" 仅由瞬态 `pit_status` 支撑（出维修区即回 NONE），进站后状态回退为 open/missed → 假"窗口再次打开"+"已错过窗口"警报。
- **修复**：记录窗口首次出现时的 `num_pit_stops` 基线；窗口期间停站数增加即闩锁为 done，直到窗口轮转到下一计划停站（`(ideal, latest)` 变化）自动重臂。游戏无论是否轮转窗口，行为均安全。
- **附带**：`laps_to_1s` 按总圈数钳制（近零负斜率噪声不再输出几万圈进 LLM）。

---

## 4. 中等修复

| # | 位置 | 修复 |
|---|---|---|
| 1 | 5 个 `.bat` | 嵌入式 python 路径**加引号分支**：解压到含空格目录（`C:\Users\John S\`）双击即失败的首跑 bug |
| 2 | `config_schema.validate` | ① str 值拒绝换行 + 剥控制字符（**堵住经 /api/llm、配置页向 .env 注入任意键**的通道）；② NaN 穿透范围校验 → `math.isfinite` 拒绝；③ `int(inf)` 的 OverflowError 逃出校验 → 并入 except；④ `PTT_BINDING` 接入格式校验（正则语义与 `input_sources.parse_binding` 一致：VID/PID 十六进制 0x 可选，byte/mask 走 `int(x,0)`） |
| 3 | `config.py` | ① `_persist` 的"读文件+合并+写"全程在锁内（ThreadingHTTPServer 并发保存不再互相丢键、重启配置回退）；② 热加载 overlay 逐出改按 **file-only keys** 判定（原先用合并了 os.environ 的视图，运行时值可能被环境变量静默回退）；③ `set_runtime(persist=False)` 的 file-wins 语义写入 docstring |
| 4 | `race_model.py` | `fuel_laps_left` 由 `total-cur` 改为 `total-cur+1`，**与 summariser `laps_remaining` 同口径**——LLM/模板不再看到相差 1 的两个"剩余圈数"（对应更新 `test_race_model.py` 断言） |
| 5 | `radio_rules.py` | dedup key 复发漏播修复：`fastest_lap_you` 按**事件 seq**、`tyre_critical/attention` 按 **stint**、`wrong_way`/`undercut_risk` 按**圈**、`rain_incoming` 按 **ETA**、`unserved_penalty` 按**未服刑次数** |
| 6 | `radio_rules.py` + `race_model.py` | `_rule_tt_new_pb`：race_model flags 持久化 `tt_pb_ms`；规则只报**改进**，首拍见到的 PB 不再误报"新纪录" |
| 7 | `radio_rules.py` + `report_txt.py` + `recorder.py` | 2026 漏网 DRS：`_rule_drs_fault` 读 `regulations_2026` 屏蔽；recorder 补记该标志，TXT 报告 DRS 故障在 2026 下显示 `-` |
| 8 | `debrief.py` | `_written_for_uid` 改为**写成功后置位**（写失败下拍重试）；无 session_uid 的会话改用独立布尔标记，不再整段复盘静默缺失；删除防御性死 try/except |
| 9 | `llm_client.py` | `FallbackLLM` 回退也失败时抛 `primary: <原因>; fallback: <原因>`（原先 primary 的 429 详情/超时完全不可见） |
| 10 | `ptt_controller.py` | `on_release` 无前置 press（`reset()` 后残留）时返回 none——toggle 模式下原先会以 duration=0"短按"**误开始录音** |
| 11 | `radio_rules.py` | `RADIO_GAP_EVERY_N <= 0` 视为静音（原先 `n and cur%n` 短路退化成**每圈都报**） |
| 12 | `engineer.py` + `app.py` | `cancel()` 修复为真实生效（LLM 调用启动前后检查、取消结果不进历史）；`app._reload` 在 .env 变化后调 `engineer.refresh_client()`——**备用端点/超时配置现在热生效** |
| 13 | `config_schema.py` | 补声明 `LLM_TIMEOUT`、`LLM_FALLBACK_API_KEY/BASE_URL/MODEL`（原先运行时无法改，`set_runtime` 直接 KeyError） |
| 14 | `summariser.py` + `profiles.py` | `_route_weather` 读取的 `rain_percentage` 由 summariser 正式产出为 fact（"降雨概率多少"的本地快答从永 miss 变为可用） |

---

## 5. 轻微修复 / 清理

**吞异常补限频日志+计数**（原先零痕迹）：
- `receiver.py` 快照刷新失败、`state.py` snapshot provider 失败、`app.py` recorder 落盘失败与 speech 启动失败——均计数并在第 1~3 次及每 50/100 次打 warning。
- `receiver.stats()` / `ticker.stats()` 的计数 dict 拷贝与写线程的竞态（RuntimeError）加有界重试。

**死代码删除**：`speech._priority_bucket`、`profiles._route_rival_pace`、`FI._web_panel_running`、`config.as_dict`、`build_manifest.EXCLUDE_PREFIXES/EXCLUDE_DIRS`；`webui`/`voice_main` 未用导入（`PACKETS_CONSUMED`、`TelemetryState`、`DEFAULT_PORT` 等）；`webui._receiver_thread` 死参数；`voice_main._loop` 只写不读字段。

**失真注释修正**：`input_sources.py` 两处 on_tap 注释与代码相反（易误导后续接线再踩双触发坑）。

**其他一行修**：
- `audio.py:183` 设备提示 `or` 优先级 bug（"(none)" 分支永不生效）；
- `FI.py` netstat/tasklist 输出按 ANSI 代码页（mbcs）解码——中文 Windows 进程名不再乱码；
- `config_ui.py` Content-Length 容错 + 256KB 请求体上限；
- `build_manifest.py` 新增 `missing_modules()`：白名单文件缺失时打 warning（拼错名单不再静默丢模块）；
- `summariser` 磨损阈值统一走 `TYRE_WEAR_LIMIT_PCT`（与 race_model 同源），缺油阈值统一走新增的 `RADIO_FUEL_DEFICIT_LAPS`。

---

## 6. 配置变更

| 键 | 类型/默认 | 说明 |
|---|---|---|
| `RADIO_FUEL_DEFICIT_LAPS` | float / -0.2 | **新增**：缺油预警阈值（surplus_laps 低于该值提示缺油），summariser 与 radio 同源 |
| `LLM_TIMEOUT` | float / 30.0 | 补声明（restart_required） |
| `LLM_FALLBACK_BASE_URL` / `_API_KEY` / `_MODEL` | str / "" | 补声明（restart_required，配置页现在可见可改） |

现有端点与键语义均未改动（只增不改）。

---

## 7. 新增/修改测试（净增 20 条）

- **新增 `tests/test_review2_fixes.py`（20 条，418 行）**，覆盖：
  - S1：坏 payload 后队列继续、player 状态异常后 worker 存活、`_pick_synth_item` 优先级选取；
  - S2：无发布者时 read-your-writes、有发布者时复用冻结副本（对象同一性断言）、flashback 重置位置基线；
  - S3：新会话 seq=1 的事件仍触发（旧代码会整场静默）；
  - S4：进站闩锁全序列（not_open→done→出站保持 done→轮转重臂）、laps_to_1s 钳制；
  - config：NaN/inf/Overflow/换行注入拒绝、控制字符清理、PTT_BINDING 合法/非法、新键声明存在；
  - PTT 无 press release 守卫；
  - 规则：fastest_lap seq 去重键、`GAP_EVERY_N=0` 静音；
  - debrief 写失败重试；FallbackLLM 双端点错误链；
  - **webui 护栏**：`_local_only` 真实对端判定、恶意 Host→403、伪造 Origin POST /api/llm→403（真实 HTTP 请求）。
- **修改 `tests/test_race_model.py::test_fuel_laps_left`**：断言随口径统一 40→41（唯一的行为性测试更新）。

---

## 8. 未动 / 需注意

- **未动**：`lib/`（第三方解析层只读）；四条设计原则与依赖白名单无违反；未引入新依赖。
- **需实跑确认**：S4 闩锁假设"进站后 `num_pit_stops` 增加"——游戏若在进站后轮转 ideal/latest 圈，闩锁自动重臂（已测）；若不轮转则保持 done（诚实状态）。实跑时留意一次进站后的播报序列即可。
- **用户侧待办不变**：游戏内实跑验证、胎温阈值 110 标定（占位值）、Windows Sandbox 干净机验证、DualSense HID（用户决定最后处理）。

---

## 9. 快速验证

```powershell
cd "G:\AI WORK\F1_TR"
git log --oneline -3          # 029d238 / 36fdee1 / 97e9ce0
py -3.12 -m pytest -q         # 199 passed, 4 skipped
py -3.12 -m pytest tests/test_review2_fixes.py -q   # 本轮 20 条
```
