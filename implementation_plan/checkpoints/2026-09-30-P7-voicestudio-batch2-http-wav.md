# P7 VoiceStudio Batch 2：逐句 HTTP /generate WAV 快速路径

- 日期：2026-09-30
- 项目：<PROJECT_ROOT>
- 分支：codex/review-playback-lifecycle
- 状态：**Batch 2 完成（离线验证通过）；真实环境验证未执行**
- 范围：逐句 HTTP WAV 快速路径；不进入 Batch 3 WebSocket，不改变默认 runtime。

## 1. 本批次目标与边界

Batch 2 将一个已完成的句子通过 VoiceStudio v0.5.6 的 POST /generate 生成完整 WAV，再转换为 Batch 1 统一的 PCMChunk，接入显式 RealtimeVoiceSession(provider_mode="http_wav")。本批次不实现 WebSocket 客户端、PCM 流式播放、自动 fallback、真实设备验收，也不把 HTTP 模式接入默认 main.py runtime。

## 2. VoiceStudio v0.5.6 API 核对结论

依据 2026-09-29 已保存的官方仓库/release 复核记录与本机源码核对：

- 版本：0.5.6，release commit 3915a62cb482bb43117aaebab57d22745bd1cc21。
- 端点：POST http://127.0.0.1:3900/generate。
- multipart 字段：	ext、profile_id、language、speed、stream=false，可选 engine。
- 响应：非 streaming 时返回 udio/wav；本项目只接受 HTTP 200。
- VoiceStudio 源码中的其他 Form 字段暂不扩展到本批次；本批次只发送已确认的最小字段集合。
- 本批次没有修改 VoiceStudio 源码；若未来修改第三方项目，需另行核对其 AGPL-3.0 义务。

## 3. Provider 实现

文件：pp/tts/providers/http_wav.py

- endpoint 仅允许 localhost、IPv4 loopback、IPv6 loopback，拒绝远端 host、用户信息、query/fragment、非法端口。
- 默认 URL 为 http://127.0.0.1:3900/generate。
- 使用 urllib.request multipart POST；请求附带 Accept: audio/wav 和脱敏身份 header：X-P7-Reply-ID、X-P7-Segment-ID、X-P7-Request-ID、X-P7-Generation。
- 默认 opener 安装 _NoRedirectHandler，HTTP 301/302 等重定向直接映射为 edirect_not_allowed，避免 POST 被带到未授权地址。
- 无自动重试；HTTPError 映射为 http_status_<code>，网络/超时/OS 错误映射为 	ransport_failed。
- 仅接受 status 200 与 content type udio/wav；缺失 status、非 bytes body、body 超限均拒绝。

## 4. WAV 与 PCM 边界

- wave 解码必须得到 24,000 Hz、mono、sample width 2（PCM16）。
- WAV 必须非空、非静音、frame 数在 1..192000（默认）内，body 默认不超过 500,000 bytes。
- frame/PCM 字节数不一致、截断 WAV、非法 WAV、格式不符均 fail-closed。
- 一个完整 WAV 响应只输出一个 PCMChunk(is_final=True, sequence=1)，并继承 request 的 reply/segment/request/generation 身份。

## 5. 实时会话接入

文件：pp/tts/real_playback.py

RealtimeVoiceSession(provider_mode="http_wav") 才启用 HTTP Provider；默认 provider_mode="legacy" 保持不变。HTTP 模式：

1. 分句器产出完整句；
2. 每句创建唯一、单调递增的 segment_id、provider_request_id，固定当前 generation；
3. 调用 provider，逐块通过 PCMChunkStreamValidator；
4. 收集 PCM 后封装为 24 kHz mono PCM16 WAV；
5. 复用现有完整 WAV playback worker/player。

实时专用分句参数：

- 最小句长：4 字；
- 软目标：32 字；
- 软搜索：24–40 字，优先在软边界字符处切分；
- 硬上限：48 字；
- pending 上限：2。

取消在 provider 调用前、chunk 接收中、validator 完成前、音频生成后和播放提交前均检查；取消后清空 pending，不提交新的已取消音频。

## 6. 默认运行与任务安全语义

- TTS_VOICESTUDIO_MODE 默认仍为 disabled；没有把它自动传入默认 CompletedReplySpeaker。
- 未修改任务执行、授权、审计、stop-ack 或 user_heard 语义。
- HTTP 快速路径不是用户已听到的证据；设备返回也不等价于用户听见。
- 未发生真实网络、GPU、Windows waveOut 或扬声器调用。

## 7. 测试与验证证据

本轮执行命令及结果：

`	ext
.\.venv\Scripts\python.exe -m unittest tests.tts.test_segmentation tests.tts.test_voicestudio_http_wav tests.tts.test_realtime_voice -v
30 tests, OK

.\.venv\Scripts\python.exe -m unittest tests.tts.test_voicestudio_http_wav -v
8 tests, OK

.\.venv\Scripts\python.exe -m unittest tests.tts.test_voicestudio_contracts tests.tts.test_realtime_voice -q
18 tests, OK

.\.venv\Scripts\python.exe -m unittest discover -s tests/tts -p "test_*.py" -q
348 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
847 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS

git diff --check
PASS
`

## 8. 修改文件

- pp/tts/providers/http_wav.py：HTTP/WAV provider、安全边界、WAV 解码、取消与关闭。
- pp/tts/segmentation/__init__.py：可选软长度策略，默认行为兼容。
- pp/tts/real_playback.py：HTTP opt-in 实时会话、validator、WAV 重封装、取消 fence、pending 背压。
- 	ests/tts/test_voicestudio_http_wav.py：multipart、状态/传输错误、边界 WAV、重定向、关闭竞态。
- 	ests/tts/test_segmentation.py：软策略校验、24–40 搜索、48 硬上限、旧行为。
- 	ests/tts/test_realtime_voice.py：逐句时序、identity/generation、取消竞态、旧 session 隔离、pending=2。
- progress.md、	ask_plan.md、implementation_plan/P7-followup-execution-tracker.md：追加本批次进度与入口。

## 9. 遗留风险与下一入口

- 尚未对真实运行中的 VoiceStudio v0.5.6 执行 HTTP 请求；实际 engine/profile 可用性、真实 WAV 内容与 latency 仍待单独授权验证。
- 尚未验证 Windows waveOut、真实音箱、GPU 或长时间取消行为。
- _generate legacy helper 仍保留在 eal_playback.py，本批次没有扩大清理范围；HTTP opt-in session 使用新 Provider。
- Batch 3 WebSocket 客户端与协议验证保持 **NOT READY / STOP**，不得因 Batch 2 离线绿灯自动进入。
- 工作区已有大量历史 staged/untracked 文件；本轮未执行 git add .、commit、push。审阅时应只看 Batch 2 相关文件，不要用全仓库无选择 staging。

下一入口：用户审阅 Batch 2 相关 diff；若需要真实环境验收，另行授权并建立独立证据记录；之后再单独授权 Batch 3。