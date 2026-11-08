# P10 R4-B：真机 `text_only` 溯源 —— 播报通道自检结论 + 一处可观测性缺陷

- 建立时间：2026-10-03
- 触发：三件套真机试听（`--aec-engine webrtc --barge-in-mode confirm --barge-in-cooldown-ms 1000`）
  8 个回合**全部** `spoken=false / reason="text_only"` ⇒ 打断通道没启、AEC3 一帧没跑。
- 工具：`tools/p10_r4b_channel_selfcheck.py`（新；`--live-opt-in` 才发真实请求，只打印结构事实，
  **不打印回复正文、不落盘音频**）
- 成本：3 次自检 ≈ **6 次真实请求**（每次 1 路由 + 1 回复）

---

## 1. 结论

**不是"闲聊文本不可播报"**：三种组合（工作类文本 / 闲聊文本 / 真机语音入口）在真机同款
router 与 adapter 下，`outcome` 都是**完整且 `speech_eligible=True`**。

**真因被代码吞掉了**：`app/voice_loop/chat_loop.py::_speak()` 把 `start_stream()` 的异常
吞成 `text_only`，与"本轮没有播报通道"记成同一个原因 ⇒ 真机上**看不到**是"没有通道"还是
"通道起不来"。已修（见 §4）。

---

## 2. 自检证据（全部 `--live-opt-in`）

| # | 文本 | router | 入口 | 关键事实 |
|---|---|---|---|---|
| 1 | 工作类指令 | `LocalChatRouter` | `submit_text` | `AgentTurnOutcome`、`completed=True`、`failure=None`、`finish_reason` 非空、`text_length=401`、**`speech_eligible=True`** |
| 2 | 闲聊"你好，听得见我说话吗？" | **`ProviderIntegration`**（`('deepseek-official','deepseek-flash','runtime-2026-09-30')`） | `submit_text` | `handoff_route=chat`、`text_length=22`、**eligible=True** |
| 3 | 同上 | `ProviderIntegration` | **`submit_asr_final`（真机语音入口）** | 同上（`stream_chunks=0`，仍 eligible=True） |

装配事实（未发请求时即可见，且已实测）：
`provider=deepseek`、`deepseek_opt_in=True`、`agent_adapter=DeepSeekChatAgentAdapter`、
`speaker=StreamingSpeaker`、**`start_stream()=ok:SessionStream`**（调用后**立刻 cancel**，未出声）、
`aec_backend=webrtc`、`aec_available=True`。

⇒ 输入侧（文本内容 / router / 语音入口 / 真实 Agent）与裸 speaker 的 `start_stream()` **全部排除**；
出问题的是**会话里的**播报启动路径，而它的失败原因当时不可见。

---

## 3. 代码层面的根因（已定位到行）

```python
# app/voice_loop/chat_loop.py（修复前）
start_stream = getattr(self._speaker, "start_stream", None)
if callable(start_stream):
    try:
        stream = start_stream()
    except Exception:      # noqa: BLE001 - speech is optional
        stream = None      # ← 异常被吞
if stream is None:
    return False, "text_only"   # ← 与"本轮没通道"同码
```

而 `_speak()` 的另外两个失败分支（`push_text_chunk`/`finish` 抛错）本来就记 `speech_failed`。
⇒ 只有**启动期**异常被降格成"没有通道"，这是本批要修的**可观测性缺陷**（不是安全缺陷：
`text_only` 与 `speech_failed` 都不播报、都不改变会话事实）。

---

## 4. 修复

| 文件 | 改动 |
|---|---|
| `app/voice_loop/chat_loop.py` | 新增常量 `TEXT_ONLY_REASON="text_only"` 与 `SPEECH_START_FAILED_PREFIX="speech_start_failed"`；`_speak()` 捕获启动异常时返回 **`speech_start_failed:<ExceptionName>`**（只带**类型名**，不带正文/路径）；`start_stream()` 返回 `None` 或没有该方法时**仍**记 `text_only` |
| `tests/voice_loop/test_chat_loop.py` | 新增 5 项：启动异常 → `speech_start_failed:RuntimeError`；只带类型名（断言不含 `secret`）；返回 None → 仍 `text_only`；无 `start_stream` → 仍 `text_only`；`finish` 失败 → 仍 `speech_failed` |

原因码合法性：`require_reason` → `require_identifier` 的模式是 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`，
`speech_start_failed:RuntimeError` 合法（含 `:` 允许）。
UI 语义：`app/ui/voice_status.py` 只把 `text_only` 当"非失败"；新的 `speech_start_failed:*`
会按**失败**呈现——这正是事实（有通道但起不来）。

---

## 5. 未做 / 局限（**不得伪造**）

1. **仍未确定会话里真正的启动异常是什么**——本批只是让它**可见**；需要**一次带该原因码的真机运行**
   才能读到（用户在场 + 真实计费）。
2. 本批未改任何播报/打断行为（只改原因码与测试）：`spoken`、是否停播、安全口径全部不变。
3. 自检工具**未加单元测试**（`tools/` 下诊断工具，不在生产路径）。
4. 三次自检都发生在 TTS 服务已就绪的状态；"服务未就绪时的行为"未单独测。
5. 真机那 8 个回合没有 AEC/打断数据（没有播报就没有窗口）⇒ 三件套真机验证**仍然待做**。

---

## 6. 复跑命令

```powershell
# 只做装配自检（不发请求、不出声）
.\.venv\Scripts\python.exe tools\p10_r4b_channel_selfcheck.py

# 真机同款入口 + 真实请求（1 路由 + 1 回复）
$env:ASSISTANT_PROVIDER='deepseek'; $env:ASSISTANT_DEEPSEEK_OPT_IN='true'
$env:DEEPSEEK_API_KEY=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
.\.venv\Scripts\python.exe tools\p10_r4b_channel_selfcheck.py --live-opt-in --router integration --entry asr --text "你好，听得见我说话吗？"
```
