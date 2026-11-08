# 里程碑、MVP 路线与实施顺序

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `../archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `../archive/implementation_plan.legacy.md`。

## 4. 里程碑

| 里程碑 | 内容 | 对应阶段 |
|---|---|---|
| M1 实现基线冻结 | MVP 范围 + 契约 + 环境约束 | P0 |
| M2 能说能听 | TTS + 录音 + ASR | 2~4 |
| M3 文字版控制电脑 | 文字指挥读写执行 + 安全确认 | 5~8 |
| M4 语音版控制电脑 | 全语音闭环 | 9 |
| M5 完整助手 | 唤醒 + 声纹 + 状态机 + 打断 | 10~12 |
| M6 打磨 | 真机调优 | 13 |

### 7.1 分阶段交付路线

本项目采用分阶段路线，不以完整后台语音助手作为第一版阻塞条件。

#### MVP-1：按键触发的可用语音电脑助手

优先实现：

```text
Windows 单机
按键触发
中文录音
本地 ASR
DeepSeek 对话
结构化工具调用
工作区内文件操作
系统信息查询
系统黑名单
普通文件改动按 L2/L3 授权规则确认
Edge TTS
执行文档生成、打开、编辑和验证
日志、使用手册和热词速查表
```

暂不阻塞 MVP-1 的功能：

```text
后台唤醒词
声纹验证
连续对话
实时打断
AEC
音色克隆
浏览器自动化
GUI 控制
永久删除
任意 PowerShell
管理员权限操作
```

#### MVP-2：自然语音交互

在 MVP-1 稳定后增加：

```text
VAD 自动判断说完
“小饼”唤醒
连续多轮对话
声纹作为误触发过滤
TTS 播报时打断
降噪和播放冷却期
```

#### MVP-3：长期运行与产品化

最后增加：

```text
AEC
系统托盘
开机启动
单实例锁
设备断线重连
崩溃恢复
会话持久化
Windows 打包
更多 Skill
浏览器和 GUI 自动化
```

### 7.10 建议实施顺序

```text
0.5 环境诊断和评测基线
1   项目骨架、配置、日志、事件
2   ToolRegistry、PathGuard、PolicyEngine、ActionPlan
3   只读工具和文字版 Agent
4   文件改动、备份、验证、双重确认
5   执行文档生成、打开、语音修改和手动编辑
6   按键触发语音闭环
7   VAD 和状态机
8   使用手册和 Skill
9   唤醒词和声纹
10  连续对话
11  打断、降噪、AEC 评估
12  Windows 常驻和产品化
13  真机调优和发布前评测
```

---

## M1.5: Unified intent-routing boundary

- **Status:** design sync complete (2026-09-24); code implementation not_started.
- **Content:** Text and voice share `ConversationManager -> IntentRouter -> RouteHandoff`, required trace fields, and `ambiguous` failure normalization.
- **Mapping:** P7-R (design first); implementation still requires a separate authorization gate.
- **Acceptance fact:** README, overview, design, roadmap, architecture, ActionPlan, safety, ASR, Agent, text E2E, and voice E2E use the same seam; `app/` and `tests/` are unchanged.
