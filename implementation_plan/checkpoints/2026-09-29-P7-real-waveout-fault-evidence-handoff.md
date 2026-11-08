# P7 真实 waveOut STOP_TIMEOUT / UNKNOWN 证据交接

> **原文损坏登记（2026-09-30 发现）**：本文件原始中文文本在首次入库（`fdb0ce1`）之前已被替换为 ASCII `?`，逐字内容不可恢复。以下正文为按现存证据（代码、测试、artifact、相邻完好文档）重写的摘要，并非原文；文末「附录：损坏原文」原样保留备审计。

> 复核说明：正文中文按现存证据重写；原始带 `?` 的行见文末附录。标题与连接性措辞的逐字原文不可恢复（重写句长与 `?` 计数不保证一致）。英文、数字、batch_id、artifact 路径、命令、命令输出、代码块、编号一律原样保留。凡证据无法确认的表述，均在文中标注「未能复核（原文丢失）」。

- 日期：2026-09-29
- 执行入口：真实 runner，实际会播放 `VoiceStudio.exe`
- 证据类别：`controlled_real_waveout_fault`
- 目的：在 Windows waveOut backend 上建立受控的 stop 故障分类证据

## 执行条件

- runner：`<PROJECT_ROOT>\tools\p7_real_waveout_fault_runner.py`
- 必须显式提供 `--live-opt-in`
- 使用真实 `WaveOutPlaybackBackend` 实例
- 不替换 DLL、不结束进程、不破坏 VoiceStudio 或设备状态
- 所有 fault 模式都先真实调用 `waveOutReset`，之后才由 shim 改写 stop 分类
- fault 后 backend 走 fail-closed：不调用 `waveOutUnprepareHeader` / `waveOutClose`，并把 backend 保留到进程退出，避免 WinMM 仍持有的 buffer/callback 资源被提前释放

## STOP_TIMEOUT 证据

artifact：

```text
<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-timeout\p7-real-waveout-timeout-20260929-03bda445b6c24e1a.json
```

```text
batch_id = p7-real-waveout-timeout-20260929-03bda445b6c24e1a
fault_mode = timeout
backend_stop_result.confirmation = timeout
backend_stop_result.reason = fault_shim_after_real_waveOutReset
acknowledged = false
```

WinMM 诊断：`waveOutOpen=0`、`waveOutPrepare=0`、`waveOutWrite=0`、`waveOutReset=0`、`cleanup_verified=false`。

本批只证明 backend `STOP_TIMEOUT` 分类事实，未证明物理静音，也不建立 `user_heard`。

## UNKNOWN 证据

artifact：

```text
<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-unknown\p7-real-waveout-unknown-20260929-cbe0a22a122346dc.json
```

```text
batch_id = p7-real-waveout-unknown-20260929-cbe0a22a122346dc
fault_mode = unknown
backend_stop_result.confirmation = unknown
backend_stop_result.reason = fault_shim_after_real_waveOutReset
acknowledged = false
```

WinMM 诊断：`waveOutOpen=0`、`waveOutPrepare=0`、`waveOutWrite=0`、`waveOutReset=0`、`cleanup_verified=false`。

本批只证明 backend `UNKNOWN` 分类事实，未证明物理静音，也不建立 `user_heard`。

## 边界说明

`wom_done_observed=true` 只表示观察到 buffer callback 回调，不表示扬声器已静音，也不表示用户听到任何声音；**只证明 waveOut backend 协议事实，不证明物理静音**。

本批三个字段 `loopback_output_stopped`、`acoustic_output_stopped`、`speaker_output_stopped` 与 `user_heard` 均为 `null`，本批次 batch 因此不建立设备停止或听感证据。

## 验证

```text
未提供 --live-opt-in：exit 2，拒绝执行
python -m unittest tests.tts.test_waveout_diagnostics tests.tts.test_fault_fixtures -q：12 tests OK
python -m compileall -q app tests tools：OK
git diff --check：OK
本批的 artifact 字段校验：PASS
```

## 当前 P7 状态

```text
STOP_TIMEOUT / UNKNOWN：仅证明 backend 协议分类事实
loopback/acoustic/speaker stop：本批 batch 未采集双通道声学指标
user_heard：本批未建立
P7：仍 Blocked / incomplete
```

P7 的受控 backend fault 证据属于受控注入，不等于自然发生的硬件故障；本批 fault 证据也不回填 Agent batch 的设备停止字段与听感判定。

## 附录：损坏原文（中文已被替换为 `?`，原样保留备审计）

````text
# P7 ?? waveOut STOP_TIMEOUT / UNKNOWN ????

- ???2026-09-29
- ??????? runner???????? `VoiceStudio.exe`
- ?????`controlled_real_waveout_fault`
- ????? Windows waveOut backend ????????? stop ?????

## ??????

- runner?`<STUDY_ROOT>\??????\tools\p7_real_waveout_fault_runner.py`
- ?????? `--live-opt-in`
- ??????????? `WaveOutPlaybackBackend` ??
- ??? DLL?????????? VoiceStudio ????????
- ?? fault ???????? `waveOutReset`?????? shim ?? stop ??
- fault ? backend ??????????? `waveOutUnprepareHeader` / `waveOutClose` ??????????? WinMM ?????

## STOP_TIMEOUT ??

artifact?

```text
<STUDY_ROOT>\??????\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-timeout\p7-real-waveout-timeout-20260929-03bda445b6c24e1a.json
```

```text
batch_id = p7-real-waveout-timeout-20260929-03bda445b6c24e1a
fault_mode = timeout
backend_stop_result.confirmation = timeout
backend_stop_result.reason = fault_shim_after_real_waveOutReset
acknowledged = false
```

?? WinMM ???`waveOutOpen=0`?`waveOutPrepare=0`?`waveOutWrite=0`?`waveOutReset=0`?`cleanup_verified=false`?

????? backend `STOP_TIMEOUT` ???????????? `user_heard` ????

## UNKNOWN ??

artifact?

```text
<STUDY_ROOT>\??????\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-waveout-fault-unknown\p7-real-waveout-unknown-20260929-cbe0a22a122346dc.json
```

```text
batch_id = p7-real-waveout-unknown-20260929-cbe0a22a122346dc
fault_mode = unknown
backend_stop_result.confirmation = unknown
backend_stop_result.reason = fault_shim_after_real_waveOutReset
acknowledged = false
```

?? WinMM ???`waveOutOpen=0`?`waveOutPrepare=0`?`waveOutWrite=0`?`waveOutReset=0`?`cleanup_verified=false`?

????? backend `UNKNOWN` ???????????? `user_heard` ????

## ????

`wom_done_observed=true` ???????????? buffer callback?????????????????????????????**?? waveOut backend ????????**?

??????? `loopback_output_stopped`?`acoustic_output_stopped`?`speaker_output_stopped` ? `user_heard` ??? `null`???? batch ??????????????

## ??

```text
??? --live-opt-in?exit 2?????
python -m unittest tests.tts.test_waveout_diagnostics tests.tts.test_fault_fixtures -q?12 tests?OK
python -m compileall -q app tests tools?OK
git diff --check?OK
???? artifact ??????PASS
```

## ?? P7 ??

```text
STOP_TIMEOUT / UNKNOWN??? backend ???????
loopback/acoustic/speaker stop??? batch ???????????
user_heard???????
P7?? Blocked / incomplete
```

P7 ???????????????????? backend fault ??????? fault ?????????????????? Agent batch ???????????
````
