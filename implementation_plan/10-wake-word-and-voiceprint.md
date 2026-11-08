# 阶段 10：唤醒词 + 声纹

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `archive/implementation_plan.legacy.md`。

## 按需阅读的共享依赖

- [`shared/data-and-privacy.md`](./shared/data-and-privacy.md)
- [`discussions/pending-decisions.md#n-windows-常驻与打包后续`](./discussions/pending-decisions.md#n-windows-常驻与打包后续)

- 目标：后台常驻，喊「小饼」才响应，且只认主人声音。
- 步骤：
  - [ ] `wake/` 集成 openWakeWord，持续监听
  - [ ] 检测到「小饼」后，用声纹模型比对主人声纹模板
  - [ ] 首次使用流程：引导录几遍「小饼」注册声纹模板存 `models/`
  - [ ] 匹配才进入录音，否则忽略
- 产出：`wake/` 模块
- 验收：喊「小饼」能唤醒；别人喊或你之外的声音不唤醒
