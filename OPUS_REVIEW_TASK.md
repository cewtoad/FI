# 给 Opus 的代码检查优化任务

## 背景

项目：F1 Race Engineer（F1 游戏 UDP 遥测 → 本地数值计算 → AI 措辞 / 语音工程师）。
路径：`G:\AI WORK\F1_TR`
基线：`c59fabf`；当前：HEAD（v2 "主动工程师"已完成，29 个提交，测试 166 passed / 4 skipped）。

本仓库刚经历一轮大改造（T0–T10b），功能已跑通，但需要一次**独立、严格的代码审查与优化**。你不是来实现新功能，而是**找问题、给出可落地的优化**。

## 必读文件（按顺序）

1. `SUMMARY_v2.md` —— 当前状态、已修 bug、未闭环项、使用方式。**先读这个。**
2. `KNOWN_ISSUES.md` —— 已知问题、停止点、设计权衡（含 PTT/输入源/打包的决定）。
3. `DESIGN.md` —— 架构与不可推翻的设计原则（见下）。
4. `README.md` —— 用户视角的功能与用法。
5. 然后按需读代码（见"重点审查范围"）。

## 不可推翻的设计原则（审查不得违反）

1. AI 不进实时回路：数值全部本地计算，AI 只负责措辞。
2. 先压缩再喂模型：进 prompt 的必须是本地算好的结论，不是原始序列。
3. 被动只读：UDP 收包 + Raw Input **只订阅**，不注入 / 不挂钩 / 不驱动。
4. 单包异常不致命：包容 + 计数。
5. stdlib 优先。
6. 扩展点工厂化：`make_llm` / `make_stt` / `make_tts` 同构，配置驱动。
7. 表达层与计算层分离：档位/prompt 只改措辞，不改 summary 数值。
8. 主动播报（radio）**禁止任何 LLM 调用**；文案全走模板。
9. 依赖白名单：faster-whisper、sounddevice、numpy、piper-tts[zh]、pyinstaller。
   **不得引入白名单外的新依赖**；**禁止使用 `audioop`**（Py3.13 已删）。
10. 依赖时间的组件必须可注入 `clock`（默认 `time.monotonic`），不得写死。
11. 现有 HTTP 端点和 `.env` 键只能新增，不得改语义/删除。

## 重点审查范围（按优先级）

### A. 正确性与并发（最高优先级）
- `state.py`：接收线程写 / 多线程读的冻结快照机制、`flashback` 回滚、注入 clock 的一致性、`lap_snapshots` 边界。
- `app.py`：`build_app` 装配顺序、`_assemble_pipeline`、`_make_alert_sink`（历史上有"arbiter 未创建先捕获 None"的时序 bug，确认已修且无同类问题）。
- `speech.py`：`SpeechArbiter` 的优先级队列、合成线程池、过期/打断/录音暂停、`AudioPlayer` 非阻塞播放与 `stop()`；有无死锁/竞态/线程泄漏。
- `ticker.py`：2Hz 分发、每 handler 异常隔离、每拍耗时告警。
- `receiver.py`：`raw_sink` 异常包容、`PACKETS_CONSUMED` 与 `state._dispatch` 同步。

### B. 量化正确性（容易错、影响大）
- `race_model.py`：磨损/配速的线性回归、`GapTrend` 对手切换重置、`PitWindow` 状态机、`laps_to_1s` 边界（除零/负斜率）、perf（<2ms 断言）。
- `summariser.py`：命名空间 facts、`laps_remaining`、胎温中位数、2026 隐藏 DRS 逻辑。
- `radio_rules.py` / `radio_director.py`：冷却/去重/每圈上限/全局最小间隔/话量档/安静模式权限；规则的正负例；`_DictModel` 属性适配是否稳健。

### C. 健壮性/边界
- `config.py`：`set_runtime` 校验、原子写 `_persist`、`reload_if_changed` 与 overlay 的一致性、并发。
- `config_schema.py`：类型/范围/枚举校验的完备性。
- `input_sources.py` / `ptt_controller.py`：绑定解析、状态机边界（hold/toggle）、HID 默认禁用逻辑。
- `debrief.py`：无数据不崩、每会话一次。
- 错误处理：有无静默 `except: pass` 掩盖真问题（应记录/计数）。

### D. 分发/打包
- `build_manifest.py`、`build_release.ps1`、`.github/workflows/release.yml`、`tools/make_zip.py`、`tools/fix_embedded_pth.py`：
  一致性、无漂移、无遗漏资源、无 HF 缓存垃圾。
- `FI.py`：单实例端口防呆、UTF-8 stdout、`--config/--selftest/--force/--port`。

### E. 代码质量
- 重复代码、死代码（如 `profiles` 里未用的路由、`radio_templates` 里未用的模板、`voice_main._on_tap`）。
- 命名、注释与实现是否一致（注释说 A 代码做 B）。
- 类型注解缺失/错误。

## 硬性要求

1. **不改 `lib/`**（第三方解析层，只读）。需要字段名先读源码确认，严禁猜。
2. **先读代码再下结论**；不确定的地方标注"需确认"，不要臆断。
3. **改动最小、保留既有行为**；不要借审查之名做大规模重构。
4. 任何改动后必须 `py -3.12 -m pytest -q` 全绿（当前基线 **166 passed, 4 skipped**），测试数量只增不减。
5. 网络/音频测试用 `RUN_NETWORK_TESTS=1` 才跑，注意别让它们默认失败。
6. 中文文件用 UTF-8；不要用 PowerShell `Set-Content -Encoding UTF8` 改源码（会破坏中文），用编辑器或 `[System.IO.File]::WriteAllText`。

## 输出要求

分三部分，**结论先行**：

**1. 问题清单**（按严重度排序，每条给出）
   - 严重度：严重 / 中 / 轻
   - 位置：`文件:行`
   - 现象/风险
   - 建议改法（最小改动）
   - 是否需要行为变更（是/否）

**2. 优化建议**（非 bug，但值得做：性能、可读性、健壮性、去重）
   - 每条注明收益与成本，让我决定做不做。

**3. 可直接落地的补丁**（可选）
   - 若某问题修复明确，给出精确 diff 或改后代码块，并说明验证方式。

**不要**在没有证据时改代码；**不要**引入白名单外依赖；**不要**提交（commit）——只给改动建议或在工作区改后让我 review。

## 已知的"不是 bug、别改"

- DualSense HID 按键在本机收不到（已记录，**用户决定最后处理**）。
- Piper 中文 pinyin/g2pW 在本机初始化卡死 → 已改 **opt-in**，默认用 SAPI，**别强行修**，仅可提建议。
- 胎温阈值 110 是占位值，待真实数据标定。
- 网页与语音模式只能二选一（UDP 端口独占），已加防呆。

## 起手命令

```powershell
cd "G:\AI WORK\F1_TR"
py -3.12 -m pytest -q                 # 基线：166 passed, 4 skipped
git log --oneline -30                  # 看 v2 提交脉络
```
