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

## 待办（用户明确想做）

- 打包 EXE / 依赖整合
- 整理上传
