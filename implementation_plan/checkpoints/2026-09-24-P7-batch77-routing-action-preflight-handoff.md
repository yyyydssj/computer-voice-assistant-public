# P7 Batch 77：路由动作计划执行前门控交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch76-routing-action-plan-handoff.md`。本批继续按 `implementation_plan/implementation-roadmap.md` 的 P7 第 7 项推进，只完成“动作计划的调用者预检/执行前门控”这一有界子批次；不改路线图、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。

本批仍不接真实 Provider、网络、PCM、播放设备、UI、ASR 或 P8；不把 READY 描述为动作已经执行，不把 REJECTED 描述为动作已经回滚。

## 2. 完成的工作

- 扩展 `app/tts/routing.py`：
  - 新增冻结枚举 `TTSRoutingPreflightState`，只允许 `READY` 与 `REJECTED`。
  - 新增冻结 `TTSRoutingActionPreflight`，绑定原动作计划、门控状态和拒绝原因；`READY` 不携带 reason，`REJECTED` 必须携带非空 reason。
  - 新增 `TTSRoutingCoordinator.preflight_actions(...)`，只读重新核对 request 身份/代际、lifecycle/playback 状态、queue pending/active 快照与当前路由。
  - 门控还校验计划步骤是否仍与当前事实一致；事实漂移或手工构造的步骤不匹配时返回 `REJECTED`，而不是执行或回写任何动作。
- 扩展 `app/tts/__init__.py` 导出 `TTSRoutingPreflightState` 与 `TTSRoutingActionPreflight`。
- 扩展 `tests/tts/test_routing_binding.py`：
  - 覆盖未漂移计划返回 READY 且不改变事实。
  - 覆盖 lifecycle、playback、queue 漂移分别被拒绝。
  - 覆盖空动作计划可通过。
  - 覆盖门控结果冻结。

## 3. 验证

使用项目嵌入式 Python 3.12、`-B`，显式把项目根加入 `sys.path`：

- 路由绑定专项：**16 passed**。
- TTS 全套：**190 passed**。
- 全量：**535 passed**。
- `compileall`（`app` 和 `tests`）：passed。
- `app`/`tests` 共 **140** 个 Python 文件 UTF-8 严格读取审计：无 U+FFFD。

## 4. 边界、未完成项和强制停止

- `TTSRoutingActionPreflight` 只是调用者执行前门控结果，不调用 `lifecycle.cancel(...)`、`playback.abort(...)`、`queue.cancel(...)`，不取消远端 Provider，不停止设备，不清理 queue，也不写回任何 playback/lifecycle 事实。
- `READY` 只表示本地快照与计划在门控时刻一致；不表示动作已经发生、Provider 已取消、设备已停止或旧音频已从设备缓冲区消失。
- `REJECTED` 只表示不能依据该计划继续进入下一步；本批不执行补偿、回滚或自动重建计划。
- 本批没有实现计划执行器、事件 ID 分配、真实 Provider cancel、设备 stop、PCM 实测时长、播放设备回调、自动编排、隐私探测/冷却恢复或 P8。
- 本批到此停止；不得在本批交接基础上越过 P7 第 7 项剩余边界进入隐私探测/冷却恢复（第 8 项）或 P8。

## 5. 下一批建议边界

下一新会话仍从 P7 第 7 项剩余的本地编排边界开始，优先选择动作计划执行前门控的一个有界“拒绝原因分类/调用者重建提示”子批次：只把已识别的快照漂移映射成稳定、不可执行的门控分类；不得在同一批实现真实动作执行器或自动状态回写。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch77-routing-action-preflight-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 77 已完成第 7 项的动作计划执行前门控：新增 TTSRoutingPreflightState 与冻结 TTSRoutingActionPreflight；TTSRoutingCoordinator.preflight_actions 只读复核 request/lifecycle/playback/queue/router 快照及计划步骤，返回 READY 或 REJECTED，不执行动作、不写回事实。请先运行并确认本批验证结果；然后直接开始下一有界批次，优先做稳定的拒绝原因分类/调用者重建提示，只返回不可执行的门控分类；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```