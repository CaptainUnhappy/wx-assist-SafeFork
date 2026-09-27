# 关键词提醒

## 1. 功能定位

关键词提醒是纯规则功能，不需要 AI。系统收到新消息后，按**提醒分组**和关键词匹配，命中后写入通知队列，并根据绑定渠道配置进行投递。

配置模型与定时分组摘要对齐：一个分组包含多个会话（`chats`），组内所有会话**共用一份关键词**、共用开关和推送目标；每个会话还有自己的 `enabled` 开关（组开着但可以单独关掉某个会话）。一个会话只能属于一个提醒分组（前端 picker 会灰掉已被占用的会话，后端 `validate_alert_groups` 再校验一次）。

## 2. 数据结构与迁移

```text
AlertGroup  id / name / chats[] / keywords[] / enabled / push_target
AlertChat   chat_id / name / enabled
```

旧的"一个会话一条配置"（`chat_id` + `group_name` 平铺）由 `_parse_alert_groups` 按 shape 自动迁移成单会话分组：组名沿用原 `group_name`，id 自动补 `ag_001` 式编号，关键词逐字保留。迁移是纯函数、幂等、**不抛异常**（解析期抛错会让 `load_assistant_config` 用默认配置覆盖整份文件，抹掉用户全部提醒配置），脏数据只 warning。老 key 不会再写回磁盘。

只有群名、没有 `chat_id` 的历史条目（agent 工具建的）迁移后保留为 `chat_id=""` 的会话，引擎按群名匹配，前端会提示重新绑定。

## 3. 匹配流程

```text
新消息
  → 清理内部标识和无效内容
  → 检查助手总开关
  → 检查消息时间窗口
  → 遍历提醒分组：组开关 → 组内会话匹配（会话级开关）
  → 关键词匹配（字面大小写不敏感子串 / `/正则/` 大小写敏感）
  → Outbox 写入 keyword_alert
  → DeliveryService 投递
```

会话匹配优先使用会话 ID；该会话没有 ID 时用群名大小写不敏感精确匹配。关键词之间是 OR 关系。

## 4. 防误触

- 超过 5 分钟的历史消息不触发；
- 同一会话同一关键词 5 秒内冷却（冷却键是"会话 + 关键词"，同组内多个会话各自独立计时）；
- 已触发消息会持久化去重；
- 助手关闭、分组关闭或组内该会话关闭时直接跳过。

## 5. 通知处理

通知可以在 AssistantPanel 中查看、确认和忽略，也可以由外部程序通过 pending API 拉取后自行投递。

实际通知渠道由消息推送页面中已经绑定的平台决定，旧版单一目标字段只作兼容。

## 6. Agent 工具

`add_alert` 只拿得到会话名，因此按"组名 或 组内任一会话名"定位已有分组：命中就往该组追加关键词（回执会说明并入了哪个分组），否则新建一个单会话分组。`list_alerts` 按分组列出，显示组内会话与关键词。

## 7. 代码位置

- 规则引擎：`src/assistant/alert.py`
- 配置与迁移：`src/assistant/config.py`（`AlertGroup` / `AlertChat` / `_parse_alert_groups` / `validate_alert_groups`）
- 配置接口：`src/web/server.py`（`PUT /api/assistant/config` 的 `alert_groups` 分支）
- Agent 工具：`src/agent/tools.py`
- 通知：`src/assistant/outbox.py`
- 投递：`src/im/delivery.py`
- 前端：`ui/src/components/AssistantPanel.jsx`（分组编辑复用摘要的 `MultiChatPicker`）、`ui/src/components/Dashboard.jsx`（即时提醒卡片）
- 测试：`tests/test_alert_regex.py`、`tests/test_assistant_alert_digest.py`、`tests/test_assistant_config.py`、`tests/test_bound_push_routing.py`
