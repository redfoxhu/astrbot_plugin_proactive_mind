# 主动心智 Proactive Mind

让 AstrBot 的机器人**自己决定**要不要主动、什么时候再醒、醒了之后干什么、要不要说话。
插件持有唯一的「下次唤醒作业」，并把每一次主动行为的**思考过程与实际动作**写进长期记忆；
发出的主动消息默认同时写回 **AstrBot 平台对话历史** 与 **LivingMemory 会话库**（标记为 Bot 自己说的），
这样记忆插件的反思摘要能正确归因并进入记忆数据库。

- 插件名：`astrbot_plugin_proactive_mind`
- 版本：`0.1.0`
- 适配：AstrBot `>= 4.24.0, < 5`（在 4.29.4 实机验证）
- 依赖：无第三方包（复用宿主自带的 APScheduler / LivingMemory 已注册工具）

---

## 1. 它怎么工作

一次决策（`core/decide.py`）分三段，全部要求模型输出 JSON：

```
人格 + 最近流水 + 状态
        │
   ① gate：要不要主动？
        │否 → 只把「下次唤醒时间」交给插件，静默结束
        │是
   ② plan：要调用哪些工具、做什么（可一串，串行执行）
        │
   ③ compose：要说的话（可为 null）+ 把「流水 + 即将发出的这条消息」一起判断下次唤醒时间
        │
   发送 → 平台历史落库 + LivingMemory 落库 → 写记忆 → 创建/替换唯一的唤醒作业
```

触发来源：

| 触发 | 说明 |
| --- | --- |
| `cron` | 唤醒作业到点（插件自持调度器，一次性 `DateTrigger`） |
| `user_message` | 用户消息、机器人回复发出后触发（带防抖 + 延迟，等流水落库；可设为只重排时间） |
| `manual` / `page` | `/mind run` 或面板「立即跑一次」 |

不行动时：**不发消息、不写记忆**，只更新下次唤醒时间与轨迹。

行动时：允许「只做事不发消息」（刷视频、回忆、整理记忆），但**一定会把思考与动作写进记忆**。

## 2. 安装

1. 把整个 `astrbot_plugin_proactive_mind/` 目录放到 AstrBot 的 `data/plugins/` 下；
2. Dashboard → 插件管理里能看到「主动心智 Proactive Mind」→ 启用/重载；
3. 打开插件详情页 → 「视图」里的 **mind** 面板，或聊天里执行 `/mind status`。

> 目录名必须是 `astrbot_plugin_proactive_mind`（元数据与面板路由都按这个名字注册）。

## 3. 设置项（Dashboard → 插件配置）

所有配置都有中文说明，这里只列关键项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `basic.enabled` | 开 | 总开关 |
| `basic.trigger_on_user_message` | 开 | 用户发言后也判断一次 |
| `basic.user_message_mode` | `decide` | `schedule_only` 则只重排时间、不发消息 |
| `flow.flow_source` | `livingmemory` | 流水来源：记忆插件 / 平台历史 / 混合 |
| `flow.flow_message_count` | **50** | 上下文窗口条数，与 LivingMemory 默认 50 对齐 |
| `flow.follow_livingmemory_window` | 开 | 直接跟随 LivingMemory 的 `session_manager.context_window_size` |
| `wake.wake_delay_minutes` | `0` | 唤醒时间整体延后（可被面板按会话覆盖） |
| `wake.min/max_interval_minutes` | `5 / 1440` | 时间点钳制区间 |
| `limit.max_consecutive_wakes` | `3` | **-1=无限；0=用户没回话时不再创建；N=最多连续 N 次** |
| `limit.count_only_when_message_sent` | 关 | 只统计真的发了消息的唤醒 |
| `mind.max_tool_calls` | `3` | 单次唤醒最多调用工具数（0=不调工具） |
| `mind.tool_blacklist` | `future_task`、`send_message_to_user` | 默认屏蔽，避免与调度/发送路径打架 |
| `memory.write_memory` | 开 | 主动行为写入长期记忆 |
| `echo.persist_proactive_to_platform_history` | **开** | 以 assistant 记录写回 AstrBot 对话历史（回读校验） |
| `echo.persist_proactive_to_livingmemory` | **开** | 以 `role=assistant` + `is_bot_message=true` 写入 LivingMemory 会话库 |
| `debug.trace_keep` | `30` | 面板保留的决策轨迹条数 |

## 4. 面板（插件视图 `mind`）

- 顶部：插件状态、LivingMemory 连接与窗口条数、当前模型、工具数、作业数；
- 全局节奏：唤醒延后、连续上限、判断模式、最短/最长间隔（保存即写回配置）；
- 会话卡片：下次唤醒时间（可直接改并保存）、倒计时、连续触发计数（`x / 上限`，达上限会标红）、
  取消唤醒、按会话延后、立即跑一次、清零计数、最近一条主动消息、落库结果（平台历史 ✓/✗、LongMem ✓/✗）；
- 最近决策轨迹：触发原因、判断结论与理由、想法、工具调用结果、要说的话、下次唤醒、告警/错误。

## 5. 命令（需管理员）

| 命令 | 说明 |
| --- | --- |
| `/mind status` | 查看当前会话的启用状态、流水来源、下次唤醒、连续计数、落库开关 |
| `/mind wake +30m` | 手动指定下次唤醒（也接受 ISO8601） |
| `/mind cancel` | 取消当前会话的唤醒作业 |
| `/mind delay 15` | 本会话唤醒自动延后 15 分钟；不带参数恢复默认 |
| `/mind run` | 立刻跑一次完整决策（调试） |
| `/mind count [reset]` | 查看 / 清零连续触发计数 |
| `/mind tools` | 列出模型可选的工具 |
| `/mind on` / `/mind off` | 启用 / 停用 |

## 6. 连续唤醒计数语义

- 用户一说话 → `silent_wake_count = 0`、`user_replied_since_last_wake = true`；
- 创建唤醒作业时：
  - 用户刚说过话 → 计数 = 1（一定允许创建）；
  - 用户没回话 → 计数 +1，若 `计数 > 上限`（且上限 ≠ -1）则**不创建**，清空下次唤醒并记 `consecutive_limit_reached`；
- `max_consecutive_wakes = 0` 的实际效果：用户不回话就不再创建，用户一开口立刻恢复。

## 7. 已知边界

- 不使用 AstrBot 内置 cron 管理器：`active_agent` 型会再跑一遍主 Agent（与本插件三段决策冲突），
  `basic` 型只支持 cron 表达式、重启后内存 handler 丢失、精度只到分钟。故唤醒作业由插件自持 APScheduler。
- 平台历史是「读-改-写」整份 history，并发极端情况下可能丢一条记录（不会损坏数据），写入后会回读校验并重试一次。
- LivingMemory 未安装/未就绪时：流水自动回退平台历史，落库只写平台历史，记忆写入回退本地
  `memory_fallback.jsonl`，不影响作业链。
- LivingMemory 的 `add_message_from_event` 会主动丢弃 cron/agent_wakeup 合成事件，因此主动消息走
  `ConversationManager.add_message` 直接写入（这是它能进入反思窗口的关键）。

## 8. 目录结构

```
main.py                  插件入口：事件钩子、命令、面板路由注册
_conf_schema.json        设置界面
metadata.yaml            插件元数据
core/
  settings.py            配置归一与钳制
  state.py               状态 + 轨迹持久化（原子写）
  scheduler.py           唤醒作业调度（单作业/替换/恢复）
  flow.py                流水读取（记忆插件 / 平台 / 混合 + SQLite 回退）
  persona.py             人格解析（会话人格 → 默认人格）
  llm.py                 Provider 解析 + JSON 输出解析与重试
  toolbox.py             工具目录与串行执行
  memory.py              主动行为写入记忆（工具 / 本地兜底）
  echo.py                落库：平台历史 + LivingMemory
  delivery.py            消息发送
  decide.py              三段式决策流水线与调度收尾
  webapi.py              面板 API
pages/mind/              面板前端（index.html / app.js / style.css）
tests/test_logic.py      单元测试（stdlib unittest）
```

## 9. 自测

```bash
cd <plugin_dir>
python -m unittest discover -s tests -t . -v     # 38 个用例
python -m compileall -q .
```

覆盖：时间解析与钳制、配置钳制、状态原子读写与轨迹裁剪、流水解析与裁剪、JSON 抽取、
平台历史落库（含回读与去重）、LivingMemory 落库（`is_bot_message`/Bot 身份）、记忆文本、
三段式流水线（不打扰 / 行动 / 只做事不说话 / 达上限不调度 / schedule_only / 判断失败兜底）。

## 10. 回滚

停用插件后直接删除 `data/plugins/astrbot_plugin_proactive_mind/` 即可；
插件数据在 `data/plugin_data/astrbot_plugin_proactive_mind/`（`state.json`、`memory_fallback.jsonl`），
可一并删除。不会修改 LivingMemory 的数据库结构与配置。

## 11. 许可证

[MIT](LICENSE) © 2026 redfoxhu

