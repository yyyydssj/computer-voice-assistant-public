# 损坏说明：p7-agent-preflight-20260929T062315Z-71e501feb039.json（2026-09-30）

- 对象：`implementation_plan/checkpoints/artifacts/2026-09-29-P7-agent-runtime-preflight/p7-agent-preflight-20260929T062315Z-71e501feb039.json`
- 性质：**artifact 本体未修改（字节不变）**；本文档只登记其中不可恢复的损坏字段，防止后续把缺失内容当成可引用事实。
- 结论：该 artifact 中有 4 处字符串字段的原始中文在首次入库（提交 `fdb0ce1`）之前已被替换为 ASCII `?`，**逐字内容不可恢复**，因此按既定规则不做猜测性回填。

## 1. 损坏字段（用 JSON 路径精确列出）

| # | JSON 路径 | 当前值 |
|---|---|---|
| 1 | `.request_text` | `?????P7-AGENT-PREFLIGHT-OK?????????????????????` |
| 2 | `.events[0].data.inserted[0].content[0].text` | 同 1（当轮投递的用户消息副本） |
| 3 | `.events[4].data.message.content[0].text` | `????? P7 ?? Agent?????????????????????????`（system 消息，来源 plugin `@deepseek-ai/dsh-system-prompt`） |
| 4 | `.events[5].data.content[0].text` | 同 1（`user/message` 事件副本） |

## 2. 为什么判定“不可恢复”（可复核）

1. 同目录**未损坏**姊妹件 `p7-agent-preflight-20260929T063803Z-b132c7ffc745.json` 使用英文固定文本
   `Reply exactly: P7-AGENT-PREFLIGHT-OK. Do not call tools or perform computer operations.`，
   其 SHA-256 本地复算为 `b7b97e5ffe37cbc2d3c37516d0f63f7d8c99a793de95c62aa802467eb4d1adec`，与该姊妹件自身记录的 `request_text_sha256` **一致** → 说明 runner 写入的是“原文的 SHA-256”。
2. 本损坏件记录的 `request_text_sha256` 为 `0655322fdf1bc451634754afd61fa756be8eb0613859828aef0f20b6baf29245`，**不等于**上述英文文本的哈希 → 本轮实际发送的提示词含非 ASCII（中文）字符；中文被 `?` 替换后，既无法反推原文，也无法用哈希反解（哈希不可逆）。
3. 已检索且**均未找到**该轮中文原文（2026-09-30）：仓库全量文件与 `.git` 历史（`fdb0ce1` 中即已损坏）、`implementation_plan/checkpoints/artifacts/**`、`<USER_HOME>\.dsh`（profiles/sessions/storages 等）、该轮 `DSH_HOME`（`<USER_HOME>\AppData\Local\Temp\p7-agent-dsh-ffqcufrb`，已不存在）、`%TEMP%` 下其余 `p7-agent-dsh-*` 目录（无 `P7-AGENT-PREFLIGHT-OK` 标记）。
4. 若用“看起来合理”的中文替换，`request_text` 与其 `request_text_sha256` 将不再自洽，等于伪造证据；故不修改本体。

## 3. 本 artifact 仍然可核验的事实（未受损，可引用）

- JSON 结构有效（`json.loads` 通过，顶层 23 个键）。
- `batch_id = p7-agent-preflight-20260929T062315Z-71e501feb039`；`started_at = 2026-09-29T06:23:15.206726Z`、`finished_at = 2026-09-29T06:23:17.859813Z`（约 2.65 s）。
- `workspace = <PROJECT_ROOT>`、`profile = sdk-minimal`、`provider = deepseek-official`、`model = deepseek-v4-flash`、`patch = app/agent/p3-no-tools.patch.yml`、`api_key_present = true`。
- `status = blocked`、`finish_reason = error`、`agent_reply = ""`、`event_count = 11`、`notification_count = 13`。
- 失败原因（原文可读，未损坏）：`llm-deepseek: the API key resolved from DEEPSEEK_API_KEY contains characters no HTTP header can carry; set DEEPSEEK_API_KEY to the raw key alone (the web Models page writes it)`，`code = INVALID_CREDENTIAL`。
- 由上述事实只能得出：这一轮**没有**任何 Agent 文本回复产出，未发生工具调用，未触碰电脑任务/授权/账本事实。**不能**由本 artifact 推导任何中文提示词内容或用户听感。

## 4. 若要真正补回

需在获得明确授权后，用**同一 runner、同一中文提示词**重跑一次 preflight（涉及真实 `DEEPSEEK_API_KEY` 与网络），把结果另存为新的 `batch_id` artifact；**不得覆盖**本文件，也不得把新结果写成旧 batch 的证据。

## 5. 复核命令

```powershell
# 解析与损坏字段枚举（脚本见会话临时文件，可自行重写等价逻辑）
<PROJECT_ROOT>\.venv\Scripts\python.exe -c "import json;d=json.load(open(r'<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-agent-runtime-preflight\p7-agent-preflight-20260929T062315Z-71e501feb039.json',encoding='utf-8'));print(d['request_text_sha256'],d['status'])"

# 姊妹件英文提示词哈希一致性
<PROJECT_ROOT>\.venv\Scripts\python.exe -c "import hashlib;print(hashlib.sha256('Reply exactly: P7-AGENT-PREFLIGHT-OK. Do not call tools or perform computer operations.'.encode()).hexdigest())"
```
