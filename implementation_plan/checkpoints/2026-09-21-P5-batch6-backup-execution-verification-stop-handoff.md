# P5 批次 6：备份、真实执行边界、ResultVerifier、部分成功与 `state_uncertain` 停止交接

- 日期：2026-09-21
- 阶段：P5 授权、执行、验证、审计与恢复
- 批次状态：**complete**
- 下一入口：P5 批次 7 审计哈希链、脱敏、恢复扫描与恢复报告
- 强制边界：本交接之后不得进入 P5 批次 8、P6、ASR、TTS 或任意命令执行入口。

## 1. 接续与本批次范围

严格接续：`implementation_plan/checkpoints/2026-09-21-P5-batch5-atomic-cancellation-stop-handoff.md`。用户已明确允许直接进入下一批次，无须再次授权。

本批次只完成：

1. 执行前备份与备份事实；
2. 七个固定副作用工具的真实本地执行边界；
3. 执行后重新观察的 `ResultVerifier`；
4. 确定失败、部分成功和 `state_uncertain` 分类；
5. 与既有 preflight、ledger、原子动作和协作取消边界的最小集成。

明确未做：审计持久化/哈希链、脱敏、崩溃恢复扫描、恢复报告、分层重试、自动回滚、UI、ASR、TTS、Skill、任意命令/PowerShell/Shell/解释器入口。

## 2. 已完成实现

### 2.1 固定执行后端

新增 `app/executor/local_backend.py`，公开 `ApplicationSpec`、`LocalExecutionBackend`、`ProcessLauncher`、`RecycleHandler`。

只接受以下七个已登记工具：

- `create_text_file`
- `write_text_file`
- `copy_file`
- `move_file`
- `rename_file`
- `move_to_recycle_bin`
- `start_whitelisted_application`

边界事实：

- 没有通用命令字符串、Shell、PowerShell、解释器、`subprocess` 或任意进程控制入口；
- 所有文件路径必须绝对且位于注入的 `allowed_root` 内；逐级拒绝链接/reparse 路径；
- 冲突策略固定为 `deny`，目录动作必须显式 `recursive`；
- 写入前要求匹配的 `BackupRecord` 与源摘要；创建动作校验冻结预览内容哈希；
- 应用启动只接受登记的 `ApplicationSpec`，启动前重新计算可执行文件摘要；不接受命令行参数；
- 备份失败在任何目标工具调用前终止当前动作，后续动作不执行；
- backend 异常按 `side_effect_may_have_occurred` 保守分类。

### 2.2 备份事实

备份目录固定为：

```text
backup_root/execution_id/action_id/attempt_id/
```

每个备份记录固定保存 `backup_id`、动作/attempt 身份、创建时间、源路径、备份路径和源摘要；文件/目录复制完成后立即重新计算备份摘要，摘要不一致则拒绝继续。备份根必须位于允许根目录内。

### 2.3 ResultVerifier

新增 `app/verification/result_verifier.py` 与公开导出 `LocalResultVerifier`。验证器不信任 backend 返回的 `result`/成功字段，而是重新观察：

- 创建/写入：目标存在、内容摘要、大小，以及从备份内容推导的 append/`replace_exact` 结果；
- 复制：源和目标均存在、摘要一致；
- 移动/重命名：源消失、目标存在、目标摘要等于备份摘要；
- 回收站：逐项源路径状态和回收站调用事实；永不虚构“可恢复成功”；
- 应用启动：PID、进程可执行文件路径、可执行文件摘要和白名单身份。

重新观察失败、证据冲突、PID/路径/摘要证据不足均返回 `state_uncertain`。

### 2.4 结果与账本收口

`ControlledExecutionRunner` 已接入备份、后端执行、重新验证和结果收口：

- 每个动作最多调用一次；
- 第一个非 `completed` 动作后，后续动作只生成 `skipped`；
- 确定无副作用的 backend/验证失败为 `not_completed`，整体可收口为 `failed`；
- 已有完成动作后出现确定失败，整体为 `partially_completed`；
- 可能已产生副作用、验证证据不足或未分类异常为 `state_uncertain`，不得降级为成功/失败；
- 不自动重试、不自动回滚、不自动续跑。

`ExecutionLedger.status` 与受控 `finalize()` 已支持并约束 `FAILED`、`PARTIALLY_COMPLETED`；`state_uncertain` 仍保持 UNCERTAIN，不得被 finalize 降级。

## 3. 测试与实际执行事实

已通过：

- `tests/security/test_local_execution.py`：5/5；
- P5 security 聚焦：75/75；
- 全量 unittest：258/258；
- `compileall`：通过；
- 严格 UTF-8：90 个 Python 文件通过，无 BOM、无 `U+FFFD`；
- `app/executor/*.py` 与 `app/verification/*.py` AST 执行面审计：`FILE_PROCESS_NETWORK_EXECUTE_CALLS=[]`。

测试确实在临时目录执行过文件创建、写入、复制、移动和重命名副作用，以验证真实文件状态；回收站仅通过注入式安全测试适配器验证，未调用真实 Windows 回收站；应用启动仅通过注入式 launcher 与 PID probe 验证，未启动真实应用。

## 4. 安全与环境事实

- 未读取 `.env`；
- 未发送真实 API 请求；
- 未启动正式 Harness runtime；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit；
- 未实现审计持久化、哈希链、恢复扫描、恢复报告或分层重试；
- 当前 ledger、fencing、备份和执行结果仍不是完整 durable transaction/WAL；真实 Windows 适配器尚未在本轮运行。

## 5. 下一批次入口与强制停止

下一批次只允许读取并实现 P5 批次 7 所需的最小文件集合，重点是：

- 审计事件的脱敏与哈希链持久化；
- 审计写入失败的 fail-closed 语义；
- 恢复扫描、遗留 execution 分类和恢复报告。

不得在下一次接续中顺手实现分层重试、自动回滚、P6、ASR、TTS、Skill、UI 或任意命令入口。若无法确认真实执行状态、无法追加审计或发现计划/授权/目标哈希/Manifest 不一致，应立即停止并记录为 blocked/system_aborted。
