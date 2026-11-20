# 主动心智 Proactive Mind

让 AstrBot 的机器人**自己决定**要不要主动、什么时候再醒、醒了之后干什么、要不要说话。
v0.5 起按 **对话 / 爱好 / 临时** 三类作业管理节奏，一轮决策只在**同一个上下文**里注入一次人格、一次流水。

- 插件名：`astrbot_plugin_proactive_mind`　版本：`0.5.1`
- 适配：AstrBot `>= 4.24.0, < 5`（4.29.4 实机验证）
- 依赖：无第三方包

---

## 1. 三类作业

| 类型 | 数量 | 触发时的上下文 | 触发后 |
| --- | --- | --- | --- |
| **对话** | 恒 1 | 人格 + LivingMemory 流水（默认 50 条） | **触发就必须更新**：到点/用户消息后一定排下一次（时间来自回复标记 > 模型 > 默认间隔） |
| **爱好** | 恒 1 | 人格 + 爱好记忆（默认 30 条）+ 近期流水（默认 20 条） | 做事 → 写记忆（本轮总结 + 下轮计划）→ 排下一个（循环） |
| **临时** | 0..N | 人格 + 流水 + 备注 | 只执行一次，**不重建** |

- 用户消息触发的周期 **等同于对话节奏**。
- `basic.user_message_mode=record_only`（A 模式）：主 Agent 的回复**就是**这次对话 job 的执行体，插件这一轮只记账（更新作业/按需写记忆），**永不补话** → 一条用户消息只回一条。
- 下次时间靠三处，按优先级：回复末尾的 `[[pmind_next:+90m]]` 标记（发送前被抠掉，用户看不到）→ 模型 JSON 里的 `dialogue_next_at` → `wake.default_interval_minutes` 兜底。
- 作业 id：`pmind:<umo>:dialogue|hobby|temp:<id>`；重启后按 `jobs` 恢复，过期按配置补跑/顺延。
- 所有作业的**创建**都在周期最后一步（发送 → 落库 → 写记忆 → 排作业）。

## 2. 一轮是怎么跑的（单上下文）

```
messages = [ system: 人格+规则 ,  user: 状态块 + 上下文块 + 输出格式 ]
① 一次调用：act / reason / thought / steps / message（草稿）/ memory / 下次时间
② 若有工具：执行 → 在同一份 messages 末尾追加工具结果 → 再问一次「定稿 message」
   （mind.single_request=true 时跳过②，直接用草稿）
③ 发送 → 落库（平台历史 + LivingMemory）→ 写记忆 → 创建/更新作业
```
`trace.context_stats` 会记录 `{requests, persona_units, flow_units}`，可在面板轨迹上直接看到"请求 1~2 次 · 人格 1 · 流水 1"。

## 3. 记忆：用 LivingMemory 自己的总结模式

- 插件**不再拼** `【主动行为｜…】` 模板；模型输出字段 `memory`（自然语言），原样交给 `memorize_long_term_memory`，由它的 `build_memory_from_structured_data → _build_storage_format` 规范化。
- **只有爱好周期自动写记忆**（本轮总结 + 下轮计划，创建下一个爱好作业之前写）。
- **对话 / 临时周期不自动写**：需要留记忆时，由她自己决定调用 `memorize_long_term_memory` 工具；轨迹里会记 `by: model_tool`（调了）或 `reason: model_decides`（没调）。
- `memory.mode`：`tool`（默认）｜`reflection`（不调工具，只靠 echo 后的反思）｜`both`——只作用于爱好周期。
- topics：`proactive_mind`、`trigger:<kind>`；爱好额外带 `proactive_hobby`（也是下次检索的标记）。

## 4. 爱好记忆怎么取

1. `documents.metadata.topics` 含 `proactive_hobby`（近 30 条，`hobby.memory_count`）；
2. 不足 → 按 `hobby.keywords`（兴趣/爱好/喜欢/想学…）搜正文与标签；
3. 仍不足 → 用近期记忆补满；
4. 再加 `hobby.flow_count` 条近期流水。

只读 `livingmemory.db`，不写任何东西；失败降级为空列表，不影响周期。

## 5. 不复读策略（面板里"三个字"那种循环）

旧版会把 `上次我的判断：{"reason": "…三个字…"}` 和 `我上次主动说话内容：…三个字…` 逐轮注入，导致模型自己复述自己。现在：
- 上次主动消息只在 `state.recent_sent_hours`（默认 6h）内回放，超时只写"内容不再回放"；
- 上次判断只保留 `行动/不打扰`，**不再注入理由原文**；
- 提示词硬规则：不复述上一轮措辞、不反复催同一件事、已翻篇的旧话题不再索要回答；
- `flow.flow_max_age_hours`（默认 24h）裁掉隔夜旧流水；
- **流水读取绕过 LivingMemory 的短缓存**：它的缓存按"上次查询的 limit"落库，而它自己的空闲检测会调 `get_messages(limit=2)`，导致我们只读到 1~2 条（曾造成"她只剩最后一句上下文、开始自编自演"）。现在固定 `use_cache=False` 直读，并在条数异常时记 warning；
- 面板轨迹对相邻两条判断做相似度标注（`panel.trace_similarity`，≥0.85 显示"疑似复读"）。

## 6. 设置项（Dashboard → 插件配置）

关键项（其余见配置页提示）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `wake.default_interval_minutes` | 120 | 对话兜底间隔 |
| `hobby.default_interval_minutes` | 180 | 爱好兜底间隔 |
| `hobby.memory_count` / `flow_count` | 30 / 20 | 爱好轮上下文 |
| `hobby.memory_tag` / `keywords` | `proactive_hobby` / 兴趣… | 爱好记忆检索 |
| `hobby.tool_whitelist` | bilibili_read / search_memes / web_search | 爱好轮工具 |
| `temp.max_jobs` | 5 | 临时作业上限 |
| `limit.max_consecutive_wakes` | 3 | -1 无限 / 0 不再创建 / N |
| `mind.single_request` | 关 | 一轮只发一次请求 |
| `memory.mode` | tool | 记忆写入方式 |
| `state.recent_sent_hours` | 6 | 旧主动消息回放时限 |
| `flow.flow_max_age_hours` | 24 | 流水回溯上限 |
| `panel.stale_hours` | 6 | 面板"很久没主动说过话"阈值 |
| `basic.user_message_mode` | decide | `record_only`=只记账不补话（A 模式） |
| `basic.next_time_marker` | 开 | 解析并抠掉 `[[pmind_next:…]]` |
| `mind.catalog_max_entries` | 24 | 工具目录上限（0=不限） |
| `mind.catalog_char_budget` | 8000 | 工具目录字符预算（0=不限） |
| `mind.tool_deny_pattern` | 见默认 | 额外剔除的工具名正则（已放宽，只挡破坏性/自身工具）|

## 7. 面板与命令

- 面板：每个会话分三块（对话 / 爱好 / 临时），可改时间、取消、立即跑一次；显示"上次主动消息（含 25 小时前这类标注）""最近一条对话""爱好下轮计划"；轨迹显示请求次数/人格/流水计数与"疑似复读"。
- 命令（管理员）：
  - `/mind status`
  - `/mind dialogue +30m`　`/mind hobby +3h [下轮想做什么]`
  - `/mind temp +10m 提醒内容`
  - `/mind cancel [dialogue|hobby|temp[:id]|all]`
  - `/mind run [dialogue|hobby|temp]`　`/mind count [reset]`　`/mind tools`　`/mind on|off`
- 工具（给主 Agent）：
  - `pmind_wake`：临时提醒（一次性、可多个）；
  - `pmind_schedule`：三类都能排——`dialogue` / `hobby`（可带 `intent` 下轮计划，带 `summary` 会**先写记忆再建作业**）/ `temp`。
  - 延时回复用它俩，不要原地 sleep。

## 8. 状态文件与迁移

`data/plugin_data/astrbot_plugin_proactive_mind/state.json`（version 2）：

```jsonc
{"version": 2,
 "sessions": {"<umo>": {
   "jobs": {
     "dialogue": {"next_at": "...", "updated_at": "...", "last_spoke_at": null, "cancelled": false},
     "hobby":    {"next_at": "...", "intent": "下轮要做什么", "intent_at": "...", "memory_id": null, "runs": 0, "cancelled": false},
     "temp":     [{"id": "t-xxxx", "next_at": "...", "note": "...", "created_by": "pmind_wake", "created_at": "..."}]
   }}},
 "traces": [ ... ]}
```

- v1 → v2 自动迁移：`next_wake_at → jobs.dialogue`；`explicit_wake_at → 一条 temp`；`pending_note → jobs.hobby.intent`；迁移前备份 `state.json.bak-v1-<ts>`。

## 9. 自测

```bash
cd <plugin_dir>
python -m unittest discover -s tests -t . -v     # 45 个用例
python -m compileall -q .
```

覆盖：时间/配置、状态迁移、三类作业模型、流水清洗与去重、爱好记忆检索（tag→关键词→近期）、
上下文唯一性（人格 1 / 流水 1）、不回放旧话、三条周期的端到端（含"记忆先于排程"顺序断言）、
工具回流第二次请求、单请求模式、上限拦截、临时作业不重建、调度器多类作业触发与恢复。

## 10. 回滚

停用插件后删除 `data/plugins/astrbot_plugin_proactive_mind/`；数据在
`data/plugin_data/astrbot_plugin_proactive_mind/`（`state.json`、`memory_fallback.jsonl`），可一并删除。
不修改 LivingMemory 的数据库结构与配置（只读它的流水与记忆表）。

## 11. 许可证

[MIT](LICENSE) © 2026 redfoxhu

---

## 更新日志

### 0.5.1

- 修复：标记内容超过 64 字被正则拒掉导致"她给了时间却走随机兜底"（去掉长度上限）；话题存储上限 60 → 120 字。
- 修复：插件自己跑的工具轮在受限工具上被拒（如 qts 的 `browser_*` 仅管理员）——新增开关 `mind.synthetic_admin`（默认开），合成事件带管理员身份。
- 新增：轨迹记录工具入参（`steps[].args`，长值截断），面板展示 `tool(args) 成功/失败`，便于排查"URL 打错""权限不足"这类问题。
- 解析增强：标记里话题支持更长内容；`marker_unparsed` 如实记录解析失败的原文。

### 0.5.0

**作业与执行**

- 对话 / 临时作业交还主 Agent 执行（`mind.dialogue_execute` / `mind.temp_execute`），走完整管线：人格、LivingMemory 召回、工具循环、出站正则；爱好作业仍由插件自己跑。
- 唤醒事件带 `wake_id`，记账只认最近一次投递（`mind.wake_record_window_seconds`），一次投递只记一次账，杜绝流式分片多次触发导致"时间被改早"。
- 投递去重闸门 `mind.delivery_guard_seconds`：距上次投递过近的触发直接吞掉。
- 保底改成"宽限期"语义：投递后 `mind.provisional_grace_seconds` 内没有回复才补垫，**她给了下次时间就永不启用**。
- 启动时清理队列里积压的唤醒事件，避免重载/补跑堆事件导致"每 20 分钟冒一句"。
- `wake.catch_up_missed_wakes` 默认关闭：过期作业顺延而不是补跑。

**时间与区间**

- "默认间隔"与"随机区间"合并：没给时间时直接在 `wake.min_interval_minutes ~ max_interval_minutes` 之间随机；爱好同理（`hobby.min ~ max`，上限放宽到 3 天）。
- 解析器支持复合相对量：`+2h30m` / `+2小时30分` / `+1h45` / `+1天2小时`。
- 标记里的话题支持 `|`、`｜`、`¦` 分隔；时间解析失败时轨迹如实标记 `marker_unparsed`，不再假装用了标记。

**提示词**

- 拆成 `output_hint`（每轮注入）与 `wake_hint`（仅到点唤醒轮），面板可改、可恢复默认。
- 标记优先从正文解析；正文没有时回退读取思考块（`reasoning_content`），并标 `reply_marker_think`。
- 标记剥离钩子加高优先级（`priority=100`），先于 LivingMemory 执行，记忆库不再存标记。

**出站文本**

- 平台侧替换 → 分段 → 过滤三段处理，可在面板配置规则、段数、间隔并预览。

**面板**

- 顶部三块（全局节奏 / 提示词 / 出站文本处理）并排等高布局，窄屏单列。
- 会话卡显示下次话题、保底标记；轨迹区分 `reply_marker` / `reply_marker_think` / `random_fallback`。
- "疑似复读"不再把"记录模式"这类结构套话当成复读。
