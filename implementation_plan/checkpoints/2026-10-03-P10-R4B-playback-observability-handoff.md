# P10 批次 R4-B 交接：真机 `text_only` 溯源 + 播报启动失败可观测性修复

- 建立时间：2026-10-03
- 证据（权威）：`research/2026-10-03-P10-R4B-playback-channel-selfcheck.md`
- 上游：`checkpoints/2026-10-03-P10-R4B-P1P2-aec-engine-residual-gate-handoff.md`（三件套 P1/P2）

---

## 1. 一句话结论

三件套真机试听时 8 个回合全部 `spoken=false / reason="text_only"`——自检证明**不是文本/路由/入口
的问题**（工作类、闲聊、真机 ASR 入口三种组合下 `speech_eligible` 都是 `True`，裸 speaker 的
`start_stream()` 也返回 `ok:SessionStream`）；真因被 `chat_loop._speak()` **吞成了 `text_only`**。
已修：启动异常现在记 **`speech_start_failed:<ExceptionName>`**（只带类型名），
`start_stream()` 返回 `None`/不存在仍记 `text_only`。**未改任何播报/打断行为与安全口径。**

---

## 2. 交付物

| 文件 | 改动 |
|---|---|
| `app/voice_loop/chat_loop.py` | 常量 `TEXT_ONLY_REASON`、`SPEECH_START_FAILED_PREFIX`；`_speak()` 启动异常 → `speech_start_failed:<TypeName>` |
| `tests/voice_loop/test_chat_loop.py` | +5 项（启动异常原因码、只带类型名、None → `text_only`、无 `start_stream` → `text_only`、`finish` 失败 → `speech_failed`） |
| `tools/p10_r4b_channel_selfcheck.py`（新） | 播报通道自检：`--live-opt-in` 才发真实请求；`--router local|integration`、`--entry text|asr`；只打印结构事实（类型/布尔/固定码/文本长度），**不打印正文、不落盘音频**；`start_stream` 只开即取消（不出声） |

---

## 3. 验证

| 项 | 结果 |
|---|---|
| 相关专项 | `test_chat_loop` + `test_voice_status` + `test_p9_auto_runner` 合计 **134 项 OK** |
| **全量** | **Ran 2585 tests in 237.796s → OK (skipped=3)**，exit 0（2580 → 2585，+5） |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 见交接时扫描（新增/修改文件均无 BOM、纯 LF） |
| 自检成本（如实） | 3 次自检 ≈ **6 次真实 DeepSeek 请求** |

---

## 4. 未做 / 下一步

1. **仍未确定会话里真正的启动异常是什么**——本批只让它可见。需要**一次真机运行**（用户在场 +
   真实计费）读 `voice_turn.reason = speech_start_failed:<TypeName>`；届时若拿到播报窗口，
   顺带完成**三件套真机验证**（AEC3 是否逐帧跑、`aec_erle_db`/`gate_in_playback`/`aec_stream_delay_ms`、
   是否仍有自触发）。
2. 真机那 8 轮没有 AEC/打断数据（无播报就没有窗口）⇒ 三件套真机验证**仍待做**。
3. 自检工具未加单测（诊断工具，不在生产路径）；"TTS 服务未就绪时的行为"未单独测。

---

## 5. 回滚

`git revert <本批提交>`；本批只改原因码字符串与测试，回滚不影响播报/打断行为。
