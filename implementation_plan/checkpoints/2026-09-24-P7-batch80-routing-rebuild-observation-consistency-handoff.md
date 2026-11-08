# P7 Batch 80：重建输入与 binding 重新观察一致性合同交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch79-routing-rebuild-input-contract-handoff.md`。
本批只完成 P7 第 7 项剩余本地编排边界中的一个有界子批次：定义“拒绝后重建输入”与调用者随后重新观察到的 `binding`/router snapshot 之间的不可执行提示与一致性合同。

本批不改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8。

## 2. 已完成工作

### 2.1 冻结一致性状态与不一致分类

扩展 `app/tts/routing.py`，新增冻结、只读的：

- `TTSRoutingRebuildConsistencyState`：`CONSISTENT` / `INCONSISTENT`；
- `TTSRoutingRebuildMismatch`：`REQUEST_IDENTITY`、`REQUEST_VERSION`、`LIFECYCLE`、`PLAYBACK`、`QUEUE`、`ROUTER`；
- `TTSRoutingRebuildConsistency`：携带状态、稳定不一致分类 tuple 和不可执行 `notice`，并提供只读 `consistent` 属性。

对象校验保持本地契约边界：一致结果不得带 mismatch；不一致结果必须至少带一个 mismatch；提示文本非空且有长度上限；对象不可变。

### 2.2 新增只读重新观察校验入口

扩展 `TTSRoutingCoordinator.validate_rebuild_observation(rebuild_input, binding)`：

- 仅接受 `TTSRoutingRebuildInput` 与 `TTSRoutingBinding`；
- 比对 request identity（request/reply/segment）、sequence/generation、lifecycle、playback、queue pending/active 和当前 router snapshot；
- 返回 `TTSRoutingRebuildConsistency`，不创建 binding、不生成 ActionPlan、不重试、不执行 cancel/abort/播放动作、不写回任何事实；
- 一致时给出“只读一致性”提示；不一致时给出“不得继续沿用旧计划、重新读取本地事实”的提示；
- `TTSRoutingRebuildInput` 仍只是 Batch 79 的当前本地输入，不被升级为自动重建器。

从 `app/tts/__init__.py` 导出上述三个新契约；`routing.py` 的 `__all__` 同步更新。

### 2.3 专项测试

扩展 `tests/tts/test_routing_binding.py`：

- 一致输入与重新观察 binding 返回 `CONSISTENT`，并验证 router/queue 未被修改；
- sequence/generation、lifecycle、queue 漂移分别归类为稳定 mismatch，并验证只读提示；
- router drift 返回 `ROUTER`，不执行动作；
- 保留 Batch 79 既有 `READY` 不带 `rebuild_input`、事实不可读不伪造输入及拒绝门控测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B`，并显式将项目根加入 `sys.path`：

- 路由绑定专项：**23 passed**；
- 明确 `tests/tts` discover 的 TTS 全套：**197 passed**；
- 全量 `tests` discover：**542 passed**；
- `compileall`（`app` 和 `tests`）：passed；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 读取审计：无 U+FFFD。

说明：直接从 `tests/tts` 启动 discover 时，项目既有包导入方式需要显式加入项目根路径；最终验证已按 Batch 79 约定的显式 `sys.path` 方式完成。

## 4. 边界、未完成项与强制停止

- 本批一致性结果是调用者可检查的提示，不是动作许可、执行结果或重建结果。
- `CONSISTENT` 不表示任何动作已执行，也不表示音频已生成、播放或用户已听见。
- `INCONSISTENT` 只表示输入与重新观察事实不能继续视为同一观察窗口；调用者必须自行重新读取本地事实。本批不替调用者重建 binding/ActionPlan。
- router snapshot 比对仍是进程内元数据比对，不代表 Provider、网络、PCM、设备或远端状态。
- 本批未接入真实 Provider/设备执行、自动动作、自动状态回写、隐私探测/冷却恢复，也未进入 P8。

## 5. 下一批建议边界

下一新会话继续停留在 P7 第 7 项剩余本地编排边界。应继续选择一个单独的、纯本地只读子批次；优先围绕一致性结果的调用者消费边界或拒绝后重新观察的最小证据链，但不得自动重建、执行动作、回写事实、接入真实 Provider/设备或进入 P8。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch80-routing-rebuild-observation-consistency-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 80 已完成第 7 项的一个有界本地子批次：新增冻结 TTSRoutingRebuildConsistencyState、TTSRoutingRebuildMismatch、TTSRoutingRebuildConsistency，以及 TTSRoutingCoordinator.validate_rebuild_observation；只读比较 TTSRoutingRebuildInput 与重新观察 binding/router snapshot 的 request identity、sequence/generation、lifecycle、playback、queue、router 事实；只返回一致/不一致提示，不自动重建、不执行动作、不写回事实。路由专项 23、TTS 197、全量 542 passed，compileall 与 140 个 Python 文件 UTF-8 审计通过。直接开始下一有界批次，继续停留在 P7 第 7 项剩余本地编排边界；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```