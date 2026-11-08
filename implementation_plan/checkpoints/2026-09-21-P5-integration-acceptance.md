# P5 阶段级集成验收记录

> 验收日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 验收范围：P1–P5 既有文字链路与 P5 批次 1–8 的授权、执行、验证、审计、恢复和重试安全边界  
> 验收结论：**通过**；P5 可标记为 `complete`，但当前必须停在 P6 授权门前。

## 1. 验收目标

验证以下链路在纯文字、无音频条件下能够按冻结契约串联，并在状态不明、审计失败、重启、防重放和汇报失败场景保持 fail-closed：

```text
ReviewableActionPlan / Manifest / Policy
→ L2 或最小 L3 确认
→ 一次性 AuthorizationSnapshot 消费
→ ExecutionPreflightGate 复检
→ Execution Ledger / fencing
→ ControlledExecutionRunner
→ ResultVerifier / ResultDocument
→ AuditStore 哈希链
→ RecoveryScanner
→ RetryPolicy
```

本次不接 ASR、TTS、Skill、UI，不执行真实应用启动、真实回收站或文件副作用，不增加命令、PowerShell、Shell、解释器、网络或进程入口。

## 2. 新增与调整的验收证据

### 2.1 跨模块集成测试

新增 `tests/integration/test_p5_integration.py`，覆盖：

1. L2 确认 → 授权消费 → preflight → ledger → 受控执行 → 验证 → result → 真实临时 `AuditStore`；
2. L3 固定短语、一次性消费和进程重启后旧授权不可消费；
3. 用户停止后的 `completed/state_uncertain` 恢复分类，不补做、不回滚、不自动续跑；
4. 副作用 `unknown_result` 必须先扫描，`state_uncertain` 阻断，已完成/确定未完成均不自动重试；
5. 已完成动作后的自然语言汇报失败不改变结构化结果，后端调用次数保持 1；
6. 确定失败只执行一次，审计脱敏和哈希链仍可验证。

### 2.2 验收中发现并关闭的安全链缺口

阶段验收发现：批次 7 已提供 `AuditStore`，但批次 6 的 `ControlledExecutionRunner` 尚未强制接入审计依赖，无法证明“审计追加失败即阻断执行”。按 P5 冻结规则完成最小修复：

- `ControlledExecutionRunner` 现在必须注入提供 `append()` 的审计存储；
- 执行开始、动作开始、动作结果和执行完成均追加结构化审计事件；
- 执行开始审计失败时，后端调用为 0；
- 动作开始审计失败时，后端调用为 0；
- 动作结果审计失败时，立即抛出稳定 `audit_append_failed`，不启动后续动作；
- 审计异常不会被误归类为普通未知执行异常，也不会触发重试。

对应实现与测试：

- `app/executor/controlled.py`
- `tests/security/test_controlled_execution.py`
- `tests/integration/test_p5_integration.py`

## 3. P5 退出条件核对

| 退出条件 | 验收证据 | 结果 |
|---|---|---:|
| 文字任务能完成只读、L2 和最小 L3 | P1–P5 集成 6/6；P5 集成 4/4 | 通过 |
| 停止后不补做、不回滚、不重试，能重新扫描并准确分类 | RecoveryScanner + RetryPolicy 集成断言 | 通过 |
| 崩溃重启后旧计划和授权不可执行 | 新实例消费旧授权返回 `authorization_unknown`；既有 preflight/授权回归 | 通过 |
| 已完成动作但自然语言汇报失败不会重复执行 | 汇报异常后 backend 调用仍为 1，`COMPLETED → NO_RETRY` | 通过 |
| 音频未接入时安全链完整 | 全链测试只使用文字契约、注入后端、只读观察和临时审计目录 | 通过 |
| 审计追加失败必须 fail-closed | 执行前/动作前调用数为 0；结果审计失败不启动后续动作 | 通过 |

## 4. 最终验证结果

| 检查项 | 结果 |
|---|---:|
| P5 集成专项 | **4/4 通过** |
| P1–P5 集成专项 | **6/6 通过** |
| P5 安全/恢复/重试聚焦回归 | **97/97 通过** |
| 受控执行与审计失败专项 | **10/10 通过** |
| 全量 unittest | **279/279 通过** |
| `compileall -q app tests` | **通过** |
| 严格 UTF-8 解码 | **103 个 Python 文件通过** |
| UTF-8 替换字符 `U+FFFD` | **0 个文件** |
| 新增执行面 AST 审计 | **无新增命令、Shell、网络、进程或音频入口** |
| 真实 API 请求 / `.env` / Harness runtime | **未发送 / 未读取 / 未启动** |
| 真实电脑副作用 | **未执行** |

已知既有编码债务保持不变，不属于本批次新增：

- `tests/recovery/test_recovery_scanner.py` 含 UTF-8 BOM；
- `tests/security/test_audit_persistence.py` 含 UTF-8 BOM。

## 5. 验收过程中的启动与测试装配事实

- 直接使用未显式加入仓库根的 unittest 启动方式会出现 `ModuleNotFoundError: app`；最终统一使用将项目根加入 `sys.path` 并指定 `top_level_dir` 的 runner。这是启动方式问题，不是产品失败。
- 新集成测试初稿的 Windows 根路径 raw string 以反斜杠结尾，产生语法错误；已改为合法字符串。
- 集成 verifier 初稿类名以下划线开头，不符合 `ResultDocument` 的稳定标识规则；已改为 `IntegrationVerifier`。
- 验收发现审计未接入受控执行器后，先写审计失败红测，再完成最小接线并重跑专项和全量回归。

## 6. 安全审计结论

- 无 `subprocess`、`os.system`、PowerShell、Shell、解释器、网络或浏览器入口；
- 无 ASR、TTS、麦克风或音频库导入；
- 未实现自动回滚、自动补做或自动续跑；`RecoveryReport.auto_resume_allowed` 仍固定为 `False`；
- 测试文件写入仅发生在 `TemporaryDirectory()` 内的审计 JSONL；
- 无真实应用启动、回收站、文件改写或外部进程；
- 未执行 `git add`、`git commit` 或 `git push`。

## 7. 验收结论与强制停止

P5 阶段级集成验收通过，P5 状态可更新为 `complete`。本结论只覆盖文字模式下的主安全基线，不表示 P6 或任何音频能力已开始。

当前强制停止在 **P6 授权门前**：

- 不进入 P6；
- 不接入 ASR/TTS；
- 不实现 Skill 或 UI；
- 不扩大工具清单或权限；
- 后续只有用户明确授权 P6 后，才能读取 P6 卡片并开始下一阶段。
