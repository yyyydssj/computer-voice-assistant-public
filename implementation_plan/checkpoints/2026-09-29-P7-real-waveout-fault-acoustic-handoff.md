# P7 waveOut fault 声学测量与停止判定

> **原文损坏登记（2026-09-30 发现）**：本文件原始中文文本在首次入库（`fdb0ce1`）之前已被替换为 ASCII `?`，逐字内容不可恢复。以下正文为按现存证据（代码、测试、artifact、相邻完好文档）重写的摘要，并非原文；文末「附录：损坏原文」原样保留备审计。

> 复核说明：正文中文按现存证据重写；原始带 `?` 的行见文末附录。标题与连接性措辞的逐字原文不可恢复（重写句长与 `?` 计数不保证一致）。英文、数字、batch_id、artifact 路径、命令、命令输出、代码块、编号一律原样保留。凡证据无法确认的表述，均在文中标注「未能复核（原文丢失）」。另：本文件记录的两个 batch 属于分层字段修正之前的运行，修正后的重测批次（`p7-real-waveout-timeout-acoustic-20260929-dd0186f813664a1a`、`p7-real-waveout-unknown-acoustic-20260929-c1b42be7114e420c`）见 `2026-09-29-P7-five-evidence-status-addendum.md`。

- 日期：2026-09-29
- 对两个真实 waveOut fault batch 做独立 WASAPI loopback + 麦克风阵列测量
- 逐条引用 fault artifact，逐字段核对 batch_id，并保留 user_heard

## STOP_TIMEOUT 声学测量批次

- batch：`p7-real-waveout-timeout-acoustic-20260929-6d4996daac564740`
- artifact：`<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-acoustic-timeout\p7-real-waveout-timeout-acoustic-20260929-6d4996daac564740.json`
- fault result：`confirmation=timeout`、`reason=fault_shim_after_real_waveOutReset`
- WinMM：`open=0`、`prepare=0`、`write=0`、`reset=0`
- loopback：active `-26.32 dBFS`、post `-96.70 dBFS`、`post_quiet=true`
- microphone：active `-38.66 dBFS`、post `-51.49 dBFS`、`post_quiet=false`
- 因此 `loopback_output_stopped=false`、`acoustic_output_stopped=false`、`speaker_output_stopped=false`：三个字段按 fail-closed 合取，本批 loopback 通道 post 已经安静，而 microphone 通道未达 quiet threshold，故未停止/未闭合；当时三个字段同时为 false 的具体推导规则未能复核（原文丢失，产生该 artifact 的旧版判定代码不在入库历史中）

## UNKNOWN 声学测量批次

- batch：`p7-real-waveout-unknown-acoustic-20260929-bdec366dc86a43cb`
- artifact：`<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-acoustic-unknown\p7-real-waveout-unknown-acoustic-20260929-bdec366dc86a43cb.json`
- fault result：`confirmation=unknown`、`reason=fault_shim_after_real_waveOutReset`
- WinMM：`open=0`、`prepare=0`、`write=0`、`reset=0`
- loopback：active `-26.32 dBFS`、post `-96.70 dBFS`、`post_quiet=true`
- microphone：active `-38.56 dBFS`、post `-50.67 dBFS`、`post_quiet=false`
- 因此 `loopback_output_stopped=false`、`acoustic_output_stopped=false`、`speaker_output_stopped=false`：三个字段按 fail-closed 合取，本批 loopback 通道 post 已经安静，而 microphone 通道未达 quiet threshold，故未停止/未闭合；当时三个字段同时为 false 的具体推导规则未能复核（原文丢失，产生该 artifact 的旧版判定代码不在入库历史中）

## 测量端点与阈值

- loopback：index 13，WASAPI：`扬声器 (Realtek(R) Audio) [Loopback]`
- microphone：index 11，WASAPI：`麦克风阵列 (Realtek(R) Audio)`
- Stereo Mix 未使用
- 阈值与判定说明
- 本次只采集 active 与 post 两段，mic post 未低于 `-55 dBFS` quiet threshold，因此不判定为安静
- `user_heard=null`：本批次未采集人工听感确认

## 结论

```text
确认 STOP_TIMEOUT：Established
确认 UNKNOWN：Established
本批 fault batch 的 WASAPI loopback 与麦克风通道未同时安静，合取判定保持 fail-closed
本批 fault batch 未建立双通道停止证据，Blocked
本批 fault batch 的 speaker_output_stopped：Blocked
user_heard：未确认
P7：Blocked / incomplete
```

本次测量过程中未改动 VoiceStudio.exe 本体，未扩展 batch 判定，全部字段取自 artifact 原始数据。

## 附录：损坏原文（中文已被替换为 `?`，原样保留备审计）

````text
# P7 waveOut fault ??????????

- ???2026-09-29
- ???????? waveOut fault batch ??????? WASAPI loopback + ???????
- ???????? fault artifact??????? batch_id???? user_heard

## STOP_TIMEOUT ??????

- batch?`p7-real-waveout-timeout-acoustic-20260929-6d4996daac564740`
- artifact?`<STUDY_ROOT>\??????\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-acoustic-timeout\p7-real-waveout-timeout-acoustic-20260929-6d4996daac564740.json`
- fault result?`confirmation=timeout`?`reason=fault_shim_after_real_waveOutReset`
- WinMM?`open=0`?`prepare=0`?`write=0`?`reset=0`
- loopback?active `-26.32 dBFS`?post `-96.70 dBFS`?`post_quiet=true`
- microphone?active `-38.66 dBFS`?post `-51.49 dBFS`?`post_quiet=false`
- ???`loopback_output_stopped=false`?`acoustic_output_stopped=false`?`speaker_output_stopped=false`????? fail-closed??loopback ???????????????????? quiet threshold???????????/??????

## UNKNOWN ??????

- batch?`p7-real-waveout-unknown-acoustic-20260929-bdec366dc86a43cb`
- artifact?`<STUDY_ROOT>\??????\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-acoustic-unknown\p7-real-waveout-unknown-acoustic-20260929-bdec366dc86a43cb.json`
- fault result?`confirmation=unknown`?`reason=fault_shim_after_real_waveOutReset`
- WinMM?`open=0`?`prepare=0`?`write=0`?`reset=0`
- loopback?active `-26.32 dBFS`?post `-96.70 dBFS`?`post_quiet=true`
- microphone?active `-38.56 dBFS`?post `-50.67 dBFS`?`post_quiet=false`
- ???`loopback_output_stopped=false`?`acoustic_output_stopped=false`?`speaker_output_stopped=false`????? fail-closed??loopback ???????????????????? quiet threshold???????????/??????

## ???????

- loopback?index 13?WASAPI?`??? (Realtek(R) Audio) [Loopback]`
- microphone?index 11?WASAPI?`????? (Realtek(R) Audio)`
- Stereo Mix ???
- ???????
- ?????? active ????????????mic post ???? `-55 dBFS` quiet threshold?????????
- `user_heard=null`?????????????

## ??

```text
?? STOP_TIMEOUT?Established
?? UNKNOWN?Established
?? fault batch ? WASAPI loopback ?????????????????? fail-closed
?? fault batch ???????????Blocked
?? fault batch ? speaker_output_stopped?Blocked
user_heard????
P7?Blocked / incomplete
```

?????????? VoiceStudio.exe???? batch ?????????? artifact?
````
