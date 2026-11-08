# P7 Batch 72：本地播放事实契约交接（2026-09-23）

## 1. 接续与授权

接续 `2026-09-23-P7-batch71-boundary-audit-stop-handoff.md`。用户明确批准本批次仅增加非重复的 P7 本地播放事实契约，并单独授权对 `<PROJECT_ROOT>` 通过正式权限升级写入。此批次不代表 Batch 71 的代际测试矩阵需要继续扩展。

## 2. 实际变更

- 新增 `app/tts/playback/__init__.py`，在现有 Provider request lifecycle 音频块元数据上提供句级不可变播放事件和快照；状态为 `NOT_COMMITTED`、`COMMITTED_NOT_STARTED`、`PLAYBACK_STARTED`、`PARTIALLY_PLAYED`、`PLAYED`、`ABORTED`。
- 提供 `commit/start/progress/played/abort` 本地同步事实记录，绑定 request/reply/segment/generation、既有音频块 event ID/index；事件序号连续、播放偏移严格递增（aborted 可与前一偏移相同）、块序号不得倒退，同一块序号与 ID 映射保持稳定。
- 快照直接构造也重放状态迁移并拒绝跳状态、终态后追加、零偏移 `PLAYED`、音频块序号/ID 别名及快照末态与末事件不一致。
- 写入前检查当前 Provider generation fence、生命周期 FAILED/CANCELLED 门控、音频块绑定和播放状态；失败不创建播放快照、不消费播放事件 ID、不替换既有快照。生命周期终态后只允许已提交播放事实显式 `abort`；播放终态仍锁定。
- `app/tts/__init__.py` 导出播放事实类型。`tests/tts/test_playback_facts.py` 增加 11 项独立测试，包含顺序迁移、失败原子性、跨 request、代际门控和直接构造审计。

## 3. 验证（2026-09-23）

- Batch 72 focused：11 passed。
- TTS 测试：154 passed；全量测试：499 passed。运行时使用项目根加入 `sys.path` 的 stdin-fed unittest discovery 与嵌入式 Python `-B`。
- `compileall -q app tests`：passed。
- 严格 UTF-8(-sig)、AST、NUL、U+FFFD 与异常连续问号审计：`app` 和 `tests` 共 135 个 Python 文件，0 failures。
- 测试过程先复现过 4 个契约红灯和 1 个零偏移红灯，修复后重新运行上述完整验证；不将先前失败误报为通过。

## 4. 边界与停止

这是调用者提交的**本地元数据事实**，不是设备回执；`PLAYED` 不证明播放设备实际出声，也不证明用户听到了音频。事件 ID 唯一性只在本 playback registry 内维护；未承诺与 lifecycle 事件或音频块共享 ID 命名空间。快照无法独立核实外部音频块是否真实存在，只有 registry 写入时依托 lifecycle 音频块进行绑定。registry 为进程内同步对象，不承诺持久化、跨进程或线程安全。

未实现真实 Provider、网络、PCM/音频 payload、播放设备、实时打断、P8 voice loop；未改路线图、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；未执行 Git add/commit/push。没有新的明确有界需求时，在此停止，不重复 Batch 66–71 的代际矩阵，也不因批次编号继续扩展。

## 5. 下一会话可复制指令

```text
严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch72-playback-facts-handoff.md。Batch 72 已完成 P7 本地播放事实元数据契约并通过 focused 11、TTS 154、全量 499 项测试。不要把 PLAYED 解释为设备出声或用户听见，不要擅自接入真实 Provider、网络、音频、播放设备或 P8；不重复代际测试矩阵、不擅改路线图/task_plan/progress/P5。下一批次仅在用户明确提出新的非重复且有界需求、并通过正式权限升级批准对 <PROJECT_ROOT> 写入后进行；否则保持停止并说明。若获授权开展新批次，完成后重新验证并编写新的交接与续接指令。
```
