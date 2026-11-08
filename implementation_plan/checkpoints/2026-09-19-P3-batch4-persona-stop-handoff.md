# P3 批次 4 persona 读取、版本与用户修改边界完成交接

> 交接日期：2026-09-19  
> 项目：`<PROJECT_ROOT>`
> 阶段：P3 文字聊天与 Harness 会话  
> 状态：`P3 batch 4 complete`；已停在下一批次授权门前

## 本次授权范围

用户明确要求读取批次 3 交接后直接进入下一批次，无须再次授权。本批次严格实现：

1. 从权威 `implementation_plan/persona.md` 严格读取人物模板全文；
2. 形成稳定、可比较的模板版本标识；
3. 冻结“只有用户修改文件、Agent 只能读取”的边界；
4. 对路径、编码、大小和文档结构执行拒绝优先校验。

未授权范围保持冻结：主界面接线、工具、ActionPlan、ASR、TTS、语音链路和 P4。

## 已完成

- 新增 `app/persona/source.py`：
  - `PersonaSource` 只接受已存在的本地绝对文件路径；
  - 拒绝相对路径、UNC/网络路径、目录和缺失文件；
  - 严格 UTF-8 读取，不容忍替换字符；
  - 默认最大 131072 字节，拒绝空文件和超限文件；
  - 解析唯一 `name`、`description` front matter，拒绝缺失、重复、畸形字段和空正文；
  - 将原文全文原样作为 `system_prompt`；
  - 用原始文件字节 SHA-256 形成 `sha256:<hex>` 版本。
- 新增不可变 `PersonaSnapshot`：保存来源路径、名称、描述、全文 prompt 和版本。
- `PersonaSource` 与 `PersonaSnapshot` 均为 frozen/slotted；模块不提供 `save`、`update` 或任何 Agent 写回接口。
- 新增 `app/persona/__init__.py` 公开 persona API。
- 新增 `tests/persona/test_source.py` 及包初始化文件，覆盖正常读取、版本稳定性、显式重载、不可变性和全部拒绝边界。

## 可观察退出事实

| 要求 | 证据 | 结果 |
|---|---|---|
| 全文作为 system prompt | 测试断言 `system_prompt` 与 UTF-8 文档全文完全相等 | 通过 |
| 稳定版本 | 测试断言版本等于原始字节 SHA-256 | 通过 |
| 用户修改需显式采纳 | 修改磁盘文件后旧快照不变，显式 `load()` 才得到新版本 | 通过 |
| Agent 不可改写 | frozen 快照/来源拒绝属性赋值，且无保存/更新接口 | 通过 |
| 非法来源拒绝 | 相对、UNC、缺失、目录、超限、非法 UTF-8、畸形 front matter 全部拒绝 | 通过 |
| 权威 persona 可读取 | 真实文档 smoke 成功，名称与版本可观察 | 通过 |

## 测试结果

- `tests.persona.test_source`：5/5 通过。
- 全量 unittest：102/102 通过。
- `python -m compileall -q app tests`：通过。
- 严格 UTF-8：50 个 Python 文件通过，无替换字符。
- 权威 persona smoke：
  - `name=persona-xiaobing-voice`
  - `version=sha256:e7aaa2eb799e8e9b8ab2a3bfd3ba92c2b1534b0c9e9214c0a4595b3fad263f2a`
  - `system_prompt` 长度 1508 字符。

## 验证环境注意事项

- 现有 `.venv` 仍引用不可用的外部 Python，测试继续使用项目内 `.python312\python.exe` 并临时把项目根加入 `sys.path`。
- persona 新目录位于当前沙箱只读项目路径，原位创建 `__pycache__` 被拒绝；compileall 使用可写工作区中的 `PYTHONPYCACHEPREFIX`，未修改项目运行设置。

## 本次未修改或未实现

- 没有修改权威 `implementation_plan/persona.md` 的人物设定。
- 没有把 persona 接入 `app/main.py` 或启动真实 Harness；adapter 已有 `system_prompt` 配置入口，实际主界面/运行时组装不属于本批次。
- 没有读取 `.env`、发送 API 请求、暴露工具或启动官方 runtime。
- 没有实现 persona 编辑器、切换 UI、自动热重载、持久化选择或远程模板。
- 没有进入工具、ActionPlan、ASR、TTS、语音链路或 P4。
- 没有执行 `git add`、`git commit` 或 `git push`。

## 设计与兼容注意事项

- `version` 表示精确文件字节版本；换行或任意字符变化都会产生新版本，便于确认当前 Agent 读取的是哪个用户版本。
- 已加载快照不自动热更新，避免会话运行中静默改变人设；调用方需要在明确的用户修改/重载动作后显式重新读取。
- 本批次把“用户修改”定义为用户在程序外或未来受控 UI 中修改权威文件；当前 API 故意不暴露写入能力。
- 全文包含 front matter、维护说明和安全规则，符合既定“读取本文件全文作为 system message”的设定。

## 当前停止点

P3 批次 4 已完成。**不得自动进入下一批次。**

下一步若获授权，只能进入 P3 批次 5：

1. 复核连接、限流、超时、流中断、空响应、未知事件、Schema 错误的全量映射；
2. 补齐必要的故障注入和全量回归；
3. 生成 P3 阶段 handoff，并停在 P4 授权门前。

仍不得提前接入工具、ActionPlan、ASR、TTS、语音链路或 P4。

## Git 状态

- 当前分支：`main`。
- 仓库仍无首次 commit；项目文件处于 untracked 基线。
- 本批次未执行 add、commit 或 push。
