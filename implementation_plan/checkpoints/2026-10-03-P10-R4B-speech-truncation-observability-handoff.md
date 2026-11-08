# P10 R4-B 交接：播报截断**事实可视化 + 如实上报**（"说一半就停"）

- 建立时间：2026-10-03
- 触发：用户反馈"一句话说一半，我没打断就停了"；随后 `--barge-in-mode off` 复测：**仍偶发中断**
- 上游：`checkpoints/2026-10-03-P10-R4B-echo-proof-tightening-handoff.md`（打断侧假停播已修）

---

## 1. `--barge-in-mode off` 复测的事实（16 轮）

| 观察 | 结果 |
|---|---|
| 打断 | 全部 `barge_in_enabled=false`（通道未建）⇒ **一次 `barge_in_interrupted` 都没有** |
| 有播报的轮次 | `spoken=true` / `reason=speech_finished` / `end_reason=silence_timeout` —— **应用认为"播完了"** |
| 无播报的轮次 | 2 轮 `text_only`（`asr_len=14`、`3`）——"有时完全不播"仍在 |

⇒ **打断路径已彻底排除**；中断发生在**播报链路自身**，而旧事实里**看不到 TTS 侧真相**。

---

## 2. 代码层机制（已定位）

```python
# app/runtime/voice_entry.py（改前）
def finish(self) -> Any:
    try:
        played = self._session.finish()      # 内部有界 join：segment_timeout_s（默认 30 s）
        self._record["played_segments"] = len(played)
        self._record["completed_sentences"] = len(self._session.timing_samples)
        ...
        return played
    finally:
        self._session.close()                # ← close() → cancel()：取消仍在播放的语音（设计如此）
```

`StreamingVoiceSession.finish()` 在**有界 join 超时**时会置 `finish_timed_out=True` 并把
`finish_timeout` 记进 `late_events`——**但 `SessionStream` 从未读取这两个事实**，调用方因此
无法区分"真的播完"与"被有界收尾截断"，轮次一律记 `speech_finished`。

（另有两条待区分路径：`push_text_chunk` 被拒 → `dropped_pushes`；播放 worker 的
`wait(ticket, …)` 提前结束。本批把它们的**事实**也都带出来了，但**未修**行为。）

---

## 3. 本批改动（只做"事实"与"如实上报"，不改播报行为）

| 文件 | 改动 |
|---|---|
| `app/runtime/voice_entry.py` | `SessionStream` 新增公开只读 `record` 属性（**没有它时截断永远不会被发现**）；`finish()` 追加记录 `finish_timed_out` / `late_events`（前 8 条）/ `dropped_pushes` / `cancelled`；`StreamingSpeaker.start_stream()` 的 record 增单调 `index`（供会话判断"本轮是否真的开了流"） |
| `app/voice_loop/chat_loop.py` | `_speak()`：`record["finish_timed_out"] is True` ⇒ **返回 `(False, "speech_truncated")`**，绝不声称完整播报；其余路径不变（`text_only` / `speech_failed` / `speech_finished`） |
| `app/runtime/auto_voice.py` | `AutoTurnFacts` 增 `speech` 字段；所有本轮事实统一经 `_turn_facts()` 构造并附带 TTS 事实；`_take_speech_facts()` 按单调 `index` **只报本轮新产生的那条**（没有新记录就 `None`，绝不把上一轮的旧事实算进来）；`log_details()` 增 `speech_route`/`speech_text_length`/`speech_played_segments`/`speech_completed_sentences`/`speech_finish_timed_out`/`speech_late_events`/`speech_dropped_pushes`/`speech_cancelled`/`speech_error`（**无文本**） |
| `tests/voice_loop/test_speech_truncation_facts.py`（新，**8 项**） | 完整回复不报截断；截断/错误/丢推/迟到事件如实入 record；`error` 只留类型名（断言不含 `secret`）；speaker record 的 `index` 单调；`finish_timed_out=True` ⇒ `speech_truncated` 且 `spoken=False`；无 record 的流保持旧行为 |

---

## 4. 验证

| 项 | 结果 |
|---|---|
| 新专项 | `tests.voice_loop.test_speech_truncation_facts` **8 项 OK** |
| `tests/tts` | **563 项 OK (skipped=3)** |
| `tests/voice_loop` | **126 项 OK** |
| 会话/主程序相关 | `test_auto_voice_barge_in` + `test_main_auto_voice` + `test_p9_auto_runner` **181 项 OK** |
| **全量** | **Ran 2608 tests in 234.872s → OK (skipped=3)**，exit 0（2600 → 2608，+8） |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 334 个 .py：**0 解码失败**、BOM **25**（与基线一致）；本批 5 个新增/修改文件均无 BOM、纯 LF |

---

## 5. 未做 / 下一步（**不得伪造**）

1. **截断本身没有修**：本批只让它**可见**且**不再谎报"播完"**。真正的截断路径
   （`finish()` 有界 join 超时后 `close()` 取消剩余语音 / worker `wait` 提前结束 /
   `push_text_chunk` 被拒）**还需一次真机运行**读新字段才能定案——届时
   `speech_finish_timed_out` / `speech_played_segments` / `speech_completed_sentences` /
   `speech_dropped_pushes` / `speech_error` 会把真凶指出来。
2. `speech_truncated` 会按"失败"呈现（比原来的"播完"更诚实），用户可能看到状态提示窗出现失败态。
3. `segment_timeout_s`（30 s）等参数**未调**；`speech` 事实依赖 `speaker.records`，替身 speaker
   没有 `records` 时如实返回 `None`。
4. 自检/探针工具与真机脚本仍未加单测。

---

## 6. 回滚

`git revert <本批提交>`；本批只改"事实与原因码"，回滚不影响播报行为（回到"一律报播完"）。
