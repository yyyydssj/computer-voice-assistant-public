# P7 批次 2 文本分段完成交接

> 交接日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：2  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/2026-09-21-P7-batch1-voice-profile-stop-handoff.md`

## 1. 本批次目标

完成 TTS 输出链路的第二批领域基础：把单个 `reply_id` 的有序模型文字流转换为有界、不可变、保序的完整句 `SentenceSegment`。

本批次不连接 VoiceStudio、Edge TTS、真实 Provider、播放设备、网络、ASR、UI 或真实音频；不实现 Provider capability、Provider 请求、音频块、播放事实、PCM 时长预算、故障切换或 P8 组合闭环。

## 2. 已完成内容

1. 新增 `app/tts/segmentation/` 纯内存分段模块，并从 `app.tts` 公开导出；
2. 新增固定 `reply_id` 边界的 `ReplySegmenter`，只接纳当前回复的连续文字块；
3. 新增不可变 `TextChunk`、`SentenceSegment`、`SegmentationResult` 和取消结果契约；
4. 每个输入块包含唯一 `chunk_id`、连续 `sequence`、`reply_id`、文字和 `is_final`；
5. 每个句段包含 `reply_id`、固定长度 `segment_id`、严格递增 `segment_index`、边界原因、源序列范围和字符数；
6. 优先按 `。！？；.!?;` 与换行形成完整句，保留闭合引号等尾随符号；
7. 没有标点时按有上限的合理长度切分，优先寻找逗号、顿号、冒号或空格等软边界；
8. 极短 token 片段只进入缓冲，不单独形成待合成句段；最终块可用 `FINAL` 边界冲刷剩余非空文字；
9. 支持有界未消费句段队列和 `dequeue()`，队列预算超限时原子拒绝，不消费 chunk ID、序号、缓冲或句段索引；
10. 空文本、重复 chunk、乱序/跳号、旧 reply、终态后文本、取消后迟到文本均 fail-closed；
11. `cancel()` 清空当前回复的缓冲和未消费句段，并永久关闭该 `reply_id` 的后续提交；
12. 文字分段状态没有 Provider 请求、音频、播放状态，因此不会把“文字已生成”误记为“已合成”或“用户已听到”；
13. 未修改路线图、`task_plan.md`、`progress.md`、P7 批次 1 handoff 或更早执行文档；本文件是本批次唯一新增的交接入口。

## 3. 关键不变量

- 一个 `ReplySegmenter` 只绑定一个 `reply_id`，旧回复或其他回复文字不能进入当前缓冲；
- chunk `sequence` 必须从 1 开始严格连续，重复、回退和跳号全部拒绝；
- 被拒绝的提交不改变最后序号、已见 chunk、文字缓冲、句段队列或下一句段索引；
- 极短 token 不直接提交给后续 TTS，只有完整标点/换行边界、合理长度边界或明确 final 才形成句段；
- 输出句段严格按原文字顺序编号并排队，后生成句段不能越过前句；
- 队列句数有明确上限，预算不足时使用背压式拒绝，不建立无界队列；
- 取消后清空未消费文本，迟到文字不能重新打开旧 reply；
- `SentenceSegment` 仅表示完整文字句段，不表示 Provider 已接纳、音频已生成、已经提交播放器或用户已经听到；
- 本批次没有文件读写运行时、网络请求、进程/命令、线程/定时器、真实音频设备或 Provider 调用。

## 4. 修改文件

- `app/tts/__init__.py`
- `app/tts/segmentation/__init__.py`
- `tests/tts/test_segmentation.py`
- `implementation_plan/checkpoints/2026-09-21-P7-batch2-segmentation-stop-handoff.md`

## 5. 验证证据

- 分段专项：`11/11`；
- P7 TTS VoiceProfile + segmentation 专项：`19/19`；
- 全量 unittest：`364/364`；
- `compileall app tests`：通过；
- 本批次 Python 文件严格 UTF-8：`3/3`，替换字符 `U+FFFD`：`0`；
- segmentation 模块执行面 AST 审计：`0` 个网络、进程、命令、线程、异步、文件读写或真实音频设备入口；
- 公开导入与最小句段 smoke：通过，输出 `[(1, 'one.', 'punctuation'), (2, 'two', 'final')] terminal`；
- 覆盖拒绝路径：空文本、极短 token、重复、乱序/跳号、旧 reply、队列预算、取消后迟到文本、final 后迟到文本；
- 覆盖边界路径：多句同块、跨块成句、标点、换行、长度兜底、final 冲刷和最大长度 reply ID；
- 未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- 当前只有文字句段与句数预算，不包含 Provider capability、Provider request、音频格式或估计延迟；
- `max_queued_segments` 只限制未消费句段数量，PCM 时长预算、最多提前合成句数和真实播放背压留到 P7 批次 6；
- 当前不记录 `provider_request_id`、`provider_generation`、音频 chunk 或播放偏移，这些分别属于后续批次 3–5；
- 当前取消只关闭文字分段边界，不调用 Provider cancel、不停止播放器，也不产生播放事实；
- 当前按字符数做无标点长度兜底，未做语言学分词、缩写识别或 SSML；
- 当前为内存对象，不提供持久化；
- 本阶段仍未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次为 **P7 批次 3：Provider capability 契约**。

下一会话应：

1. 先读取本文件；
2. 只实现 Provider 能力声明、严格校验与兼容性判定，覆盖文本流、音频流、取消、音色切换、音频格式和估计延迟；
3. 能力必须是 Provider adapter 的显式事实，未知或未声明能力不得被当作支持；
4. 优先覆盖拒绝路径：未知 Provider、空格式、重复格式、非法采样参数、负数/无界延迟、声明与请求不兼容、未声明取消或音色切换却尝试使用；
5. 保持 `reply_id` / `segment_id` 与 Provider request、播放事实分层，不在本批次调用网络、合成音频或播放设备；
6. 完成专项测试、P7 专项、全量回归、compileall、严格 UTF-8 和执行面审计；
7. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio 或 Edge TTS 网络/真实 Provider；
- 创建真实 `provider_request_id`、调用 synthesize、接收音频流或实现重试；
- 接入播放设备、sounddevice、扬声器或真实音频输出；
- 实现播放事实、PCM 时长预算、播放队列、Provider fallback、冷却或恢复探测；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。