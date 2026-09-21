# R5 方案：降低无消费包型的解析开销

> 状态：**已实现 Option A**（静态已消费集合白名单）+ README 20Hz 建议；
> Option B（惰性解析）/ Option D（解析线程）维持否决，Option C（降频采样）后置。
> 实施细节：`receiver.PACKETS_CONSUMED`（10 类），`run.py` / `webui.py` 改传该集合，
> `capture*.py` 诊断工具保留 `PACKETS_ALL`；同步守护见 `tests_packet_filter.py`
> （含"集合必须与 `_dispatch` 分支一致"的源码级断言）。基准数据见文末附录。
> 背景：`CODE_REVIEW.md` BUG-5 —— web 模式使用 `PACKETS_ALL`，MOTION / MOTION_EX /
> CAR_SETUPS / TYRE_SETS / FINAL_CLASSIFICATION / LOBBY_INFO / LAP_POSITIONS 共 7 类包
> 被完整解析后在 `state._dispatch()` 中无分支直接丢弃。

## 0. 先说结论（推荐）

**推荐 Option A（静态"已消费集合"白名单）**，一行改动、零架构风险、把浪费降为零。
Option C 的"用户侧把游戏内 UDP 频率调到 20Hz"作为零成本补充写进 README。
**不建议**现在做 Option B（惰性解析）和 Option D（独立解析线程）。

## 1. 现状与事实核对（读码结论，非猜测）

1. **浪费发生在哪**：`lib/telemetry_manager/factory.py` 的解析顺序是
   `长度检查 → 解析 29 字节 header → 类型支持检查 → 格式检查 → interested 检查 → 载荷解析`。
   `interested 检查在载荷解析之前`——所以未被订阅的包型本来就只付 header 解析的成本。
   **真正的原因是 `webui.py` / `run.py` 主动传了 `PACKETS_ALL`**（17 类全开）。
2. **成本量级**（CPython 数量级估算，建议落地前用 §4 基准实测确认）：
   - MOTION 是频率×体积乘积最大者：22 车 ≈ 1.3KB/包，默认随帧率（最高 60Hz）发送，
     struct.unpack + 约 1100 次对象属性赋值 ≈ 每包 0.2–0.4ms，占单核约 1–2%。
   - 其余 6 类为低频或小包（FINAL_CLASSIFICATION 每场一次、LOBBY_INFO 秒级、
     CAR_SETUPS/TYRE_SETS 低频），合计浪费很小。
   - 结论：**这不是性能危机，是"纯浪费 + 功能缺口"**，优先级低于正确性问题。
3. **当前真实被消费的包型**（R4 合入后）：SESSION、LAP_DATA、CAR_TELEMETRY、
   CAR_STATUS、CAR_DAMAGE、CAR_TELEMETRY_2、SESSION_HISTORY、TIME_TRIAL、
   PARTICIPANTS、EVENT —— 共 10 类。

## 2. 候选方案与权衡

### Option A：静态"已消费集合"白名单（推荐）

- **改动**：`receiver.py` 在 `PACKETS_RACE` 旁新增 `PACKETS_CONSUMED`（上述 10 类），
  `webui.serve()` 与 `run.py` 由传 `PACKETS_ALL` 改为传 `PACKETS_CONSUMED`。
- **收益**：7 类无消费包退化为"仅 29 字节 header 解析"即丢弃；MOTION 的持续浪费归零。
- **代价/风险**：
  - 未来新增消费者需改一行集合定义（`receiver.py` 顶部注释已为按需子集预留了位置）；
  - 与现有注释"接收期过滤会让模式切换失效"的顾虑不冲突——race/timetrial 两种模式的
    读取需求都包含在 10 类之中，切换依然是纯读取层关注点；
  - FINAL_CLASSIFICATION 暂不入集：它每场只来一次、解析成本可忽略，等录制层要用
    权威终局榜时再加（避免"解析了又不用"的回归）。
- **兼容性**：`PACKETS_ALL` 常量保留（capture/capture_live 诊断工具仍用全量）。

### Option B：惰性/延迟解析（不推荐现在做）

- **思路**：header 常解析，载荷保留原始字节，首次字段访问才解析（wrapper 或 `__getattr__`）。
- **优点**：零配置、面向未来（任何消费者直接读字段）。
- **缺点**：侵入 `lib/f1_types` 的 17 个包类——这是上游 pits-n-giggles 的同步边界，
  改动越多将来 merge 上游新格式支持越难；解析错误从"接收时可统计"
  （`drop_reasons`）变为"访问时才暴露"，排障路径变差；为一个非瓶颈问题引入复杂性。
- **结论**：仅在 Option A 仍不满足时 reconsider（几乎不会发生）。

### Option C：高频率消费型的降频采样（可后置）

- **思路**：对"已消费但高频"的类型（LAP_DATA / CAR_TELEMETRY / CAR_STATUS，
  20–60Hz）按帧门计数做 N 抽 1（如 ≥10Hz 封顶）。state 聚合与 delta 插值在 5–10Hz
  分辨率下精度损失可忽略；**EVENT 绝不可抽稀**（事件不可丢）。
- **零成本变体（建议先做）**：README 指导用户把游戏内 UDP 频率设为 20Hz——
  不改代码，立减 2/3 全量解析与内存分配。
- **风险**：delta 精度略降；帧门逻辑要小心不要和 `SessionFrameGate` 的去重语义打架。
- **结论**：等真实 CPU profile 显示需要时再做。

### Option D：解析移入独立线程/进程（不推荐）

单用户场景下解析成本远低于一核容量；这会重新引入本项目从 pits-n-giggles
裁掉的 IPC/并发复杂度，方向性倒退。

## 3. 建议的实施顺序（获批后）

1. §4 基准脚本先跑一遍拿到 before 数据（30 行，可丢弃）。
2. Option A 一行改动 + README 的 20Hz 建议。
3. `tests_udp.py` 风格补一个断言：feed 一个 MOTION 包 → `stats()["dropped_unparsed"]`
   计入 "Uninterested packet type"（沿用现有 reason 文案），消费型包正常 `frames+1`。
4. after 数据对比，写进本文件附录。

## 4. 附：建议的测量方法

- 微基准：对每类包 `factory.parse()` 各 N=10_000 次取均值（用 `fake_data` /
  手工构包），输出每包 µs 与 60Hz 下折算的单核占比。
- 端到端：live 会话中对比 `receiver.stats()["accepted"]` 增速与进程 CPU%（任务管理器）。

## 5. 附录：实施后的实测基准（2026-09-22，Python 3.11 / Linux 沙盒）

方法：以 `fake_data.make_telemetry()`（22 车 CAR_TELEMETRY，1,352 字节，重解析器的
代表）作代理，各 3,000 次取均值，比较"interested 集合含该类型（完整解析）"与
"不含（仅 29 字节 header 解析后丢弃）"。

| 路径 | 每包耗时 |
|---|---|
| 完整解析（22 车载荷） | 33.2 µs |
| 仅 header 后丢弃 | 8.8 µs |
| **每包节省** | **24.4 µs** |

折算单核占用节省：60Hz 下 **0.15%**，20Hz 下 **0.05%**。

结论与 §1 预判一致：**这不是性能危机，而是"纯浪费 + 行为不一致"的卫生问题**。
MOTION（24 车 × 更多浮点场）会大一圈，但数量级不变。Option A 的实际收益排序：
① console 模式不再因 `PACKETS_TIME_TRIAL` 漏掉 PARTICIPANTS/EVENT 而与 web 模式
行为不一致（排行榜车手名退化、超车事件缺失）；② 消除每秒数千次无意义对象分配
（GC 压力）；③ CPU 节省（小但免费）。若未来出现真实 CPU 压力，再评估 Option C。
