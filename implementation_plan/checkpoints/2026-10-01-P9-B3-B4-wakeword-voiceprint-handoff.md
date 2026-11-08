# P9 B3/B4 交接：唤醒词门禁 + 声纹误触发过滤

> 日期：2026-10-01（本地）
> 阶段：P9（VAD、连续对话、唤醒词与声纹过滤）——**B1/B2 + UI + 修复批次 1-4 已完成，B3/B4 代码与接线完成**
> 上位交接：`2026-10-01-P9-hud-and-capture-issues-handoff.md`
> 全量基线：**1978 tests OK (skipped=3)**；`compileall -q app tests tools` exit 0
> 状态：**B3/B4 均已完成并真机验收通过（正例+负例，§4.4/§4.5）；两个真实模型已下载并离线验证**；未提交、未 push。

## 1. 目标与硬边界（冻结）

| 项 | 口径 |
|---|---|
| 唤醒词（B3） | **只决定"是否开启一个新的输入会话"**；唤醒阶段音频**不进 ASR**；不决定授权、不产生计划/执行 |
| 声纹（B4） | **只做误触发过滤**：不匹配就在**提交之前**丢弃该轮；不作为身份凭证、不决定授权 |
| 失效方向 | 两者都 fail closed：模型/依赖/构造失败 → 退出码 3，**不得**静默退化成"无需唤醒词"或"看起来过滤了其实没有" |
| 过滤故障 | 声纹判定链路报错或档案未注册 → **不丢弃该轮**（只记只读计数）；它不决定授权，也绝不因故障放行任何计划/执行事实 |
| 离线边界 | 两个适配器都不打开设备、不联网、不建线程、不读写文件（只在构造时 `Path.is_file()` 检查模型）；`sherpa_onnx` 只在构造时延迟导入 |

## 2. 冻结接口

### 2.1 `app/audio/kws.py`（B3）

- `KeywordGateConfig`：`model_dir`（约定 `tokens.txt/encoder.onnx/decoder.onnx/joiner.onnx`）或四个显式路径覆盖、`keywords_file`、`keyword`、`sample_rate_hz=16000`、`feature_dim=80`、`num_threads=1`、`max_active_paths=4`、`keywords_score=1.0`、`keywords_threshold=0.25`、`num_trailing_blanks=1`、`provider="cpu"`；`resolved_paths()` 给出解析结果。
- `KeywordGate`：`observe(AudioPacket) -> bool`（本帧是否命中）、`reset()`、只读 `detected_count/observed_frames/decoded_steps/injected/to_details()`。
- 判定口径：逐帧 `accept_waveform` → `while is_ready(): decode_stream(); get_result()`；拿到非空结果就 `reset_stream()` 并做**文本匹配**（配置了 `keyword` 时要求归一化后完全相等；未配置时任意非空结果算命中）。单帧解码步数上限 `MAX_DECODE_STEPS_PER_FRAME=64`（防死循环，超限 fail closed）。
- 错误码（固定短代码）：`model_missing` / `dependency_missing` / `init_failed` / `detect_failed` / `unsupported_sample_rate` / `unsupported_format`。

### 2.2 `app/audio/voiceprint.py`（B4）

- `VoiceprintConfig`：`model_path`、`threshold=0.5`（余弦相似度，越大越严）、`speaker_name="owner"`、`num_threads=1`、`provider="cpu"`、`debug=False`。
- `VoiceprintFilter`：`enroll(embedding)`、`enroll_from_pcm(payload)`、`score(embedding)`、`verify_from_pcm(payload) -> VoiceprintDecision`；只读 `enrolled/enrolled_speakers/verified_count/rejected_count/to_details()`。
- `VoiceprintDecision`：`matched` / `score` / `reason ∈ {matched, below_threshold, not_enrolled, too_short}`。
- 错误码：`model_missing` / `dependency_missing` / `init_failed` / `compute_failed` / `unsupported_sample_rate` / `unsupported_format`。

### 2.3 runner 开关（默认全 off，行为不变）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--kws-mode off\|gate` | `off` | `gate`：每轮先等唤醒词；命中才开新会话 |
| `--kws-model-dir` | — | 唤醒词模型目录（四文件 + 可选 `keywords.txt`） |
| `--keyword` | `小饼` | 唤醒词文本 |
| `--kws-threshold` | `0.25` | 触发阈值（越大越难触发） |
| `--kws-wait-seconds` | `30` | 每轮等待唤醒词上限；超时本轮不开启收音（`end_reason=wake_word_timeout`） |
| `--voiceprint-mode off\|filter` | `off` | `filter`：提交前做声纹误触发过滤 |
| `--voiceprint-model` | — | 声纹嵌入 onnx |
| `--voiceprint-profile` | — | 档案 JSON：`{"speaker": "...", "embeddings": [[...], ...]}`（只有嵌入向量，无音频） |
| `--voiceprint-threshold` | `0.5` | 相似度阈值 |
| `--voiceprint-seconds` | `8` | 判定用"本轮最近多少秒"音频 |

新增只读事实：`end_reason=voiceprint_rejected`（被过滤丢弃）、artifact `auto_capture.voiceprint{...}` 与 `voiceprint_threshold/voiceprint_seconds`、`hud_timeline`、`end_decision`。

驱动侧只增一个**只读帧观察口** `AutoCaptureDriverConfig.frame_sink`（默认 `None`）：声纹缓冲用它收集本轮 PCM；观察者抛错即整体停用，绝不参与任何判定。

## 3. 已交付文件与测试

| 文件 | 说明 |
|---|---|
| `app/audio/kws.py` | 新增（B3 适配器） |
| `app/audio/voiceprint.py` | 新增（B4 适配器；含只算向量的 `embed_from_pcm`） |
| `tools/p9_voiceprint_enroll.py` | 新增（声纹注册工具：只写嵌入向量、不写音频） |
| `tests/audio/test_kws.py` | 新增 34 项（31 离线 + **3 真实模型**：官方样例流式检出/「小饼」不误触发/静音不触发） |
| `tests/audio/test_voiceprint.py` | 新增 30 项（配置/错误码/注入契约/注册判定/过短音频/嵌入形状） |
| `tests/integration/test_p9_voiceprint_enroll.py` | 新增 14 项（opt-in 门禁/check-only 不碰设备/收满即停/档案无 BOM 只含向量/`--out` 才落盘 + 1 项真实模型冒烟） |
| `app/voice_loop/auto_driver.py` | 增 `frame_sink`（只读观察口，默认 None） |
| `tools/p9_auto_voice_runner.py` | 增 `wait_for_wake_word()`、`load_voiceprint_profile()`、会话接线、两组 CLI 与 artifact 事实 |
| `tests/integration/test_p9_auto_runner.py` | 增 `WakeWordGateTests`（6 项）与 `VoiceprintFilterTests`（7 项） |
| **目录清理** | 删 `models/kws/_model.tar.bz2`（31.9MB）与解包源目录（fp32/epoch-99 冗余）；`models/kws` 现约 5MB（冻结布局 + test_wavs） |

**验证（修复前先红、修复后绿；全部离线注入替身）**：
- 唤醒词命中 → 才开第二路采集并正常提交；**超时 → 从不开启收音**（`backend.turn_stream is None`）；门禁报错/模型缺失 → `WakeWordUnavailableError`（退出码 3）。
- 声纹不匹配 → `end_reason=voiceprint_rejected`、**未提交给 Agent**（`agent.calls == []`）；匹配 → 正常提交；过滤链路报错 → **不丢弃**且只累加 `errors`；档案缺失/为空/模型缺失 → 装配期 fail closed。
- 真实模型层：KWS 官方样例流式检出 ✓、静音/他人音频不误触发 ✓；声纹真实语音同人自查 `score=0.8923` ✓。
- 全量 **1961 tests OK (skipped=3)**、`compileall` exit 0。

## 4. 真实模型：本机现状与下载记录

本机实测：`sherpa_onnx.KeywordSpotter`、`SpeakerEmbeddingExtractor`、`SpeakerEmbeddingManager` **API 都可用**。

### 4.1 唤醒词模型：**已下载并验证**（2026-10-01，用户授权）

| 项 | 值 |
|---|---|
| 来源 | `https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2` |
| 归档 | 32,654,866 字节；`sha256=B2F7C89690DC8CE4C6ED6AFEAB7CD800C36AD1421FB6B6302B4A4B194CF7F35F` |
| 选用 | **`epoch-12-avg-2` int8 三件套**（与官方文档示例一致）；归档内另有 `epoch-99-avg-1` 与 fp32 版本，实测两者在这批样例上表现相同 |
| 冻结布局 | `models/kws/{tokens.txt, encoder.onnx, decoder.onnx, joiner.onnx, keywords.txt}` + `test_wavs/`；原始解包目录保留在 `models/kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` |
| git | `models/` 已在 `.gitignore`（第 21 行 `/models/`），模型**不会**被提交 |
| 唤醒词 | 「小饼」→ `x iǎo b ǐng @小饼`（`models/kws/keywords.txt`） |

**关键词文件是怎么生成的（重要坑）**：
- 官方 `sherpa-onnx-cli text2token` 需要 `click`，本机 `.venv` **未安装**；按纪律**不擅自装依赖**。
- 改为**实证生成**：`小饼` = `xiǎo bǐng` → 在模型自带 `tokens.txt`（227 个 ppinyin token）中逐个确认 `x`(id 48)、`iǎo`(115)、`b`(29)、`ǐng`(84) 都存在，再按模型自带格式写成一行。
- ⚠️ **文件必须无 BOM**：PowerShell `Out-File -Encoding utf8` 会写 BOM，sherpa C++ 会把它当成 token 的一部分并报
  `Cannot find ID for token x at line: x iǎo b ǐng @小饼`。最终用 Python `encoding="utf-8"`（无 BOM）写入。
- ⚠️ **关键词文件必须真的传给门禁**（真机踩过，已修）：runner 原先只传 `model_dir`、没传 `keywords_file`，
  sherpa 构造时缺关键词 → 抛异常被收敛成 `init_failed`，表现为"一运行就退出"。
  现改为：`KeywordGateConfig.resolved_paths()` 在未显式给出时**自动采用 `<model_dir>/keywords.txt`**（仅当该文件确实存在），
  并新增专用错误码 `keywords_missing`（提示里直接给出格式示例）与 CLI `--kws-keywords-file` 覆盖项；
  `tests/integration/test_p9_auto_runner.py::test_real_model_gate_assembles_with_the_shipped_layout`
  用真实模型走一遍装配（不 patch），把这次的回归钉死。

**真机模型离线验证（真实模型 + 逐帧流式喂入，与 runner 同路径）**：

| 用例 | 结果 |
|---|---|
| 官方样例 `test_wavs/3.wav` + 官方 `test_keywords.txt`（任意命中口径） | **命中**（`detected_count=1`、`decoded_steps=7`；非流式对照检出 `['文森特卡索','法国']`） |
| 同一音频 + 「小饼」词表 | **不命中**（词表能被解析、不误触发） |
| 50 帧静音 + 「小饼」词表 | **不命中** |
| 已固化为测试 | `tests/audio/test_kws.py::RealKwsModelTests`（3 项，`skipUnless` 模型存在；模型缺失时自动跳过） |

### 4.2 声纹模型：**已核实来源并下载，真实语音验证通过**

- 官方来源（sherpa-onnx 文档 `Speaker Identification` 页明确指向的 release tag）：
  `https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models`（tag 名确为 `recongition`，官方拼写如此）
  该 tag 下有 **21 个 `.onnx`** 资源（已用 GitHub API 列全，中文可选见下）。
- **本次下载**：`3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`
  - 经 `https://ghfast.top/` 代理：`.../releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`
  - 28,281,138 字节；`sha256=F682B514C05D947EE3FA91CD6EC6C5C7543479A128373FA29B1FAEDCCD21FD11`
  - 落到 `models/voiceprint/`（`models/` 已 gitignore，不会提交）
- **其它已核实的候选**（同一 tag，按体积）：`wespeaker_zh_cnceleb_resnet34.onnx`(25.3MB)、
  `3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx`(27MB)、
  `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx`(37.8MB)、
  `3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx`(68.1MB)、
  `3dspeaker_speech_eres2net_sv_zh-cn_16k-common.onnx`(210.4MB)。切换只需改 `--voiceprint-model`。

**真实语音验证（本机，真实模型 + 仓库内 16k 语音样例）**：

| 用例 | 结果 |
|---|---|
| 模型加载 + 取嵌入 | 成功（`injected=False`） |
| 同一段音频前半注册、后半验证 | `matched=True`，`score=0.8923` |
| 另一段样例（4.wav） | `score=0.8197`（**同一位说话人的不同词句**，因此也命中——本机样例不足以做跨说话人负例） |
| 跨说话人负例 | **未做**：需要你本人 + 他人/电视语音真机验证（见 §4.4） |

### 4.3 声纹注册工具（新）

`tools/p9_voiceprint_enroll.py`：

```powershell
cd '<PROJECT_ROOT>'
# 只看检查（不开麦克风、不写文件）
.\.venv\Scripts\python.exe tools\p9_voiceprint_enroll.py --live-opt-in --check-only
# 真机注册（6 秒），只写嵌入向量
.\.venv\Scripts\python.exe tools\p9_voiceprint_enroll.py --live-opt-in --seconds 6 `
    --model models\voiceprint\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx `
    --speaker owner --out <PROJECT_ROOT>\data\voiceprint-owner.json
```

- **不给 `--out` 就只打印摘要、不落盘**；给了才写 JSON，且**只有嵌入向量、没有音频**（UTF-8 无 BOM）；
- 失败（无设备/音频过短/模型缺失）一律 fail closed：固定短码 + 退出码 3/1，**不写半成品档案**；
- 档案属生物特征数据：请自行保管，可随时删除（建议放 `data_root` 并确保 gitignore）。
- 测试：`tests/integration/test_p9_voiceprint_enroll.py`（14 项，离线注入采集/提取器）。

### 4.4 声纹过滤真机验收：**正例与负例均已通过**（2026-10-01）

**注册结果**：`data/voiceprint-owner.json`（5,391 字节、无 BOM、`version=1`、`speaker=owner`、
`embedding_dim=192`、1 条向量、**无任何音频字段**）。

**正例（本人说话）真机通过** — artifact
`artifacts/2026-09-30-P9-auto-voice-loop/p9-auto-voice-loop-20261001T064855Z-cd9559ed714545d1.json`：

| 项（读自 artifact） | 值 |
|---|---|
| `auto_capture.voiceprint` | `mode=filter`、`enabled=True`、`threshold=0.5`、`errors=0`、`enrolled_speakers=['owner']`、**`verified_count=3`、`rejected_count=0`** |
| 3 个有效轮次 | 全部 `end_source=segment`、`submitted/spoken=True`、`route=voicestudio_ws_pcm`、`error=None` |
| 两轮无语音 | `end_reason=no_speech`（正确丢弃，不提交） |
| 用户听感 | `user_heard=yes`；`valid_turns=3` |

**离线跨说话人负例（真实模型 + 官方样例，已固化为测试）**：以 `3.wav` 注册后逐个打分：

| 样例 | score | 判定 |
|---|---|---|
| 3.wav（本人） | 1.0000 | 命中 |
| 4.wav（同一人、不同词句） | 0.8340 | 命中 |
| 5.wav / 6.wav | 0.3061 / 0.2298 | 拒绝 |
| 0.wav / 1.wav / 2.wav | -0.0314 / 0.0335 / 0.0455 | 拒绝 |

→ **同人 0.83~1.00、他人 ≤0.31，阈值 0.5 正好落在空档**；测试
`tests/audio/test_voiceprint.py::RealVoiceprintModelTests`（2 项）断言该分离度。

**负例（他人/电视）真机通过** — artifact
`artifacts/2026-09-30-P9-auto-voice-loop/p9-auto-voice-loop-20261001T065251Z-2f2f61dd369f4ec1.json`：

| 项（读自 artifact） | 值 |
|---|---|
| `auto_capture.voiceprint` | `threshold=0.5`、`errors=0`、**`verified_count=0`、`rejected_count=3`** |
| 三轮 | 全部 `end_reason=voiceprint_rejected`、`reason=voiceprint_rejected:below_threshold`、`submitted=False`、**`asr_text_length=0`** |
| 逐轮 `speaker_verification` | `score=0.2016 / 0.1749 / -0.0678`（全部远低于 0.5） |
| 用户听感 | `user_heard=no`（确实没有播报） |

**重要副产品**：被声纹拒绝的轮次**连 ASR 都没跑**（过滤发生在 `complete_turn()` → `driver.release()` 之前），
所以误触发既不进 Agent、也不产生识别正文，还省一次解码。

**同批修掉的两处提示不清**（这轮日志里暴露）：
1. 被丢弃的轮次原先打印笼统的"未得到可靠的识别结果"→ 现在打印
   `声纹不匹配：按误触发丢弃本轮（score=…，阈值 …）——不提交、不播报（未跑 ASR）`；
2. 收尾汇总原先一律归咎"Silero VAD 未判为人声"→ 现在新增 `声纹丢弃 N` 计数与对症提示
   （`_zero_valid_summary()`：全轮被声纹丢弃时明确说"这是预期行为，不是故障"）。

**复现命令**（单行、绝对路径）：

```powershell
& '<PROJECT_ROOT>\.venv\Scripts\python.exe' '<PROJECT_ROOT>\tools\p9_auto_voice_runner.py' --live-opt-in --turns 3 --tts-mode ws_pcm --voiceprint-mode filter --voiceprint-model '<PROJECT_ROOT>\models\voiceprint\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx' --voiceprint-profile '<PROJECT_ROOT>\data\voiceprint-owner.json' --voiceprint-threshold 0.5
```

做法：让**电视/视频或另一个人**说话（你自己别说话）；VAD 会照常开启一轮，声纹应当在**提交前**把它丢掉，
控制台出现 `end_reason=voiceprint_rejected`，且该轮**不提交、不播报**。
每轮 `score/reason` 现在会落进 artifact 的 `end_decision.speaker_verification`（调阈值靠它，不靠观感）。

### 4.5 唤醒词门禁真机验收：**已通过**（2026-10-01，用户本人执行）

artifact：`artifacts/2026-09-30-P9-auto-voice-loop/p9-auto-voice-loop-20261001T063603Z-bde668c1d0e748d2.json`

| 项（读自 artifact，非转述） | 值 |
|---|---|
| `auto_capture.kws_mode / keyword / kws_threshold / kws_wait_seconds` | `gate` / `小饼` / `0.25` / `30` |
| `min_utterance_ms / input_stall_ms / audio_capacity_frames` | `300 / 5000 / 500` |
| 第 1 轮 | `frames=176`、`end_source=segment`、`hits=73`、`last_hit=3520ms`、`segment_ends=1`、`probe_errors=0`、`submitted/spoken=True`；识别 `'今天天气怎么样？'` —— **不含「小饼」，证明唤醒音频没进 ASR** |
| 第 2 轮 | `frames=178`、`end_source=segment`、`hits=108`、`last_hit=3560ms`、`submitted/spoken=True`；识别 `'你说你是怎么样的？'` |
| 每轮提示窗时间线 | `listening → hearing → transcribing → thinking → speaking → listening` |
| 用户听感 | `user_heard=yes`（两轮播报都听到）；`valid_turns=2` |

命令（单行、绝对路径，任何目录都能跑）：

```powershell
& '<PROJECT_ROOT>\.venv\Scripts\python.exe' '<PROJECT_ROOT>\tools\p9_auto_voice_runner.py' --live-opt-in --turns 2 --tts-mode ws_pcm --kws-mode gate --kws-model-dir '<PROJECT_ROOT>\models\kws' --keyword 小饼
```

验收中发现并修复的真机缺陷见上位交接 §3.7（**上一轮残留段让下一轮 2 帧收尾**：每轮重置 VAD +
`min_utterance_ms=300` + 门禁可见化）；修复后两轮 `frames` 为 176/178，不再是 2。
若漏触发用 `--kws-threshold 0.1~0.2`，误触发用 `0.4~0.5`。

## 5. 下一步（B3/B4 真机均已通过）

已完成：① KWS 模型下载+验证；② 声纹模型核实来源+下载+真实语音验证；③ 声纹注册工具；④ 目录清理；
⑤ **B3 唤醒词门禁真机验收通过**（见 §4.5）。
剩下**必须由你在真机上执行**的两步：

1. 可选：`--kws-mode gate` + `--voiceprint-mode filter` 同时开启联调（两者单独都已真机验证）。
4. 可选（待你决定）：给 ResultGate 加"正文归一化后为空/纯标点 → 不提交"的兜底
   （那轮 `'。'` 曾被判 ACCEPTED_FINAL；有了 `min_utterance_ms=300` 后已很难触发）。
5. 收尾：更新 `NEXT-SESSION.md`/`task_plan.md`/`progress.md`，按你的 review 提交（工作区共 50+ 项未提交）。

## 6. 已知风险与未做

- 唤醒词误触发/漏触发需要真机调阈值（`--kws-threshold`）；`gate` 模式下每轮都要先喊一次。
- 声纹阈值 0.5 是通用起点，真机需按你的嗓音/麦克风标定；档案只含嵌入向量，但仍属隐私数据。
- `--kws-mode gate` + `--voiceprint-mode filter` **同时**开启尚未做真机联调。
- 未接 `app/main.py`（主程序语音入口仍是按键模式，无唤醒词/声纹）；未做 AEC/全双工（P10）。
- 本轮全部改动**未提交、未 push**。
