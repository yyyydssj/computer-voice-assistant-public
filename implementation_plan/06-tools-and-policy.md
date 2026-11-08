# 阶段 6：工具层 + 9 个工具 + 安全分级

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `archive/implementation_plan.legacy.md`。

## 按需阅读的共享依赖

- [`shared/safety-boundaries.md`](./shared/safety-boundaries.md)
- [`shared/action-plan.md`](./shared/action-plan.md)
- [`discussions/pending-decisions.md#a-mvp-工具清单与权限等级`](./discussions/pending-decisions.md#a-mvp-工具清单与权限等级)

- 目标：实现"控制电脑"的工具集合。
- 步骤：
  - [ ] 定义统一 Tool 接口（名称/描述/参数 schema/执行函数/危险级别）
  - [ ] 实现 9 个工具：`read_file` `list_dir` `search_file` `list_process` `run_command` `write_file` `edit_file` `move_file` `delete_file`
  - [ ] `delete_file` 用「移到回收站」实现，不真删
  - [ ] `run_command` 加超时、限工作目录、拒绝交互式命令
  - [ ] 单独写测试脚本逐个测工具
- 产出：`tools/` 模块
- 验收：每个工具手动跑通，危险级别标注正确
