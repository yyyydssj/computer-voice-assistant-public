# P7 Batch 78：路由动作计划门控拒绝分类与重建提示交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch77-routing-action-preflight-handoff.md`。本批继续按 `implementation_plan/implementation-roadmap.md` 的 P7 第 7 项推进，只完成“动作计划执行前门控的稳定拒绝分类/调用者重建提示”这一有界子批次；不改路线图、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。

本批仍不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器或 P8；不把 READY 描述为动作已经执行，不把 REJECTED 描述为动作已经回滚。

## 2. 完成的工作

- 扩展 `app/tts/routing.py`：
  - 新增冻结枚举 `TTSRoutingPreflightRejection`，稳定区分 `REQUEST_IDENTITY_DRIFT`、`LIFECYCLE_DRIFT`、`PLAYBACK_DRIFT`、`QUEUE_DRIFT`、`ROUTER_DRIFT`、`PLAN_DRIFT`。
  - 新增内部只读异常，将 binding/request、lifecycle、playback、queue、router 与动作步骤漂移映射为稳定拒绝分类。
  - 扩展冻结 `TTSRoutingActionPreflight`：REJECTED 必须携带分类；READY 不携带分类。
  - 增加 `requires_rebuild` 与 `rebuild_hint`，明确调用者应丢弃旧计划并基于最新本地快照重新生成 binding 与动作计划；不自动重建、不执行动作、不回写事实。
- 扩展 `app/tts/__init__.py` 导出 `TTSRoutingPreflightRejection`。
- 扩展 `tests/tts/test_routing_binding.py`：
  - 覆盖 READY 无拒绝分类且无需重建。
  - 覆盖 lifecycle、playback、queue、router 与计划步骤漂移的稳定拒绝分类。
  - 覆盖 REJECTED 的重建提示与冻结门控结果。

## 3. 验证

使用项目嵌入式 Python 3.12、`-B`，显式将项目根加入 `sys.path`：

- 路由绑定专项：**18 passed**。
- TTS 全套：**192 passed**。
- 全量：**537 passed**。
- `compileall`（`app` 和 `tests`）：passed。
- `app`/`tests` 共 **140** 个 Python 文件 UTF-8 严格读取审计：无 U+FFFD。

曾尝试直接使用 `unittest discover` 配合环境变量 `PYTHONPATH`，嵌入式 Python 未采用该路径并产生 `ModuleNotFoundError: No module named 'app'`；随后按项目既有约定显式 `sys.path.insert(0, 项目根)` 重跑，以上结果为有效验证结果。

## 4. 边界、未完成项和强制停止

- `TTSRoutingPreflightRejection` 只是门控分类，不是 Provider 错误码、动作执行结果或回滚结果。
- `requires_rebuild=True` 只提示调用者丢弃旧计划并重新观察/绑定/规划；本批没有自动重建、自动重试、事件 ID 分配或状态回写。
- `READY` 只表示门控时刻本地快照与计划一致；不表示 lifecycle 已取消、playback 已中止、queue 已清理、Provider 已取消、设备已停止或旧音频已从设备缓冲区消失。
- `REJECTED` 只表示当前计划不可继续使用；不执行补偿、回滚、queue 清理、设备停止或远端取消。
- 本批没有实现计划执行器、真实 Provider cancel、设备 stop、PCM 实测时长、播放设备回调、自动编排、隐私探测/冷却恢复或 P8。
- 本批到此停止；不得在本批交接基础上越过 P7 第 7 项剩余边界进入隐私探测/冷却恢复（第 8 项）或 P8。

## 5. 下一批建议边界

下一新会话仍从 P7 第 7 项剩余的本地编排边界开始。优先选择动作计划门控的一个有界“调用者重建输入合同”子批次：只定义重建所需的当前 binding/快照输入边界或不可执行的提示字段；不得在同一批实现真实动作执行器、自动重建、自动状态回写或跨入第 8 项。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch78-routing-preflight-rejection-classification-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 78 已完成第 7 项的门控拒绝分类与调用者重建提示：新增 TTSRoutingPreflightRejection，稳定区分 request identity/lifecycle/playback/queue/router/plan 漂移；TTSRoutingActionPreflight 的 REJECTED 必须带分类，并提供 requires_rebuild/rebuild_hint；不自动重建、不执行动作、不写回事实。路由专项 18、TTS 192、全量 537 passed，compileall 与 140 个 Python 文件 UTF-8 审计通过。直接开始下一有界批次，优先定义调用者重建所需的当前 binding/快照输入合同，只返回不可执行的本地提示；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```
