# 冷场主动开口会怎样影响聊天历史和缓存？

研究对象为插件 1.4.0。这里的“冷场”指一段时间没人说话后，由定时器发起的回复。群里有人发消息时，机器人即使没被 @ 也能插话，那一轮已经走 AstrBot 的官方聊天流程。[入站交接](../main.py#L210) · [冷场定时器](../main.py#L453)

## 用一次查天气说明

假设群里刚问过天气，工具回答“明天下雨”，机器人发了“记得带伞”。

| 时刻 | 按顺序发给模型的内容（节选） |
| --- | --- |
| 普通聊天 | 你问天气 → 机器人调用天气工具 → **工具回答“明天下雨”** → 机器人继续回答 |
| 冷场主动开口 | 你问天气 → 机器人调用天气工具 → **直接跳到“记得带伞”** → 请主动开口 |

**聊天历史**是 AstrBot 下次还要用的记录。插件会过滤掉 `tool` 和 `_checkpoint`（框架用来关联平台消息的标记）。模型若接受这段不完整的输入并生成正文，插件会用过滤后的副本覆盖原历史。工具调用还在，结果却没了。写入发生在 QQ 发送之前，未送达的文字也可能出现在历史中。[过滤和写回](../reply_generator.py#L210) · [生成与实际发送顺序](../main.py#L328) · [AstrBot 4.29.4 历史更新](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/conversation_mgr.py#L279-L307)

**提示缓存**让模型服务商复用以前算过的相同开头。以 DeepSeek 为例，在上述例子里，如果前面的人格、工具列表等都相同，开头仍有机会命中。从“明天下雨”消失的位置起，后面的内容就不能沿用那条旧前缀了。冷场请求还会自行拼人格，并省略普通主 Agent 的工具列表，差异有时出现得更早。临时回复提示适合放在本轮用户输入的末尾，留住前面较长的共同部分。[DeepSeek 缓存说明](https://api-docs.deepseek.com/guides/kv_cache/) · [插件的提示位置和冷场请求](../reply_generator.py#L251)

**上下文压缩**是另一件事。聊天太长时，AstrBot 的内置 Agent 会按设置截断或摘要。冷场路径直接调用底层聊天提供商，跳过该步骤，也未给历史更新传回本次 token 用量。恢复历史完整性与恢复官方压缩，需要分别验证。[4.29.4 直接生成](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/star/context.py#L184-L226) · [官方压缩入口](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/agent/runners/tool_loop_agent_runner.py#L826-L846)

## 怎么处理

1. **现在要保护历史**，可以先关群聊和私聊各自的“冷场后主动开口”。日常对话及群内有人说话后的插话可以继续使用。[群聊开关](../_conf_schema.json#L99) · [私聊开关](../_conf_schema.json#L133)
2. **支持官方主动回合的版本**，推荐让插件决定“何时说”，让 AstrBot 负责“怎么读历史、压缩和回复”。已核实 4.30.0-beta.1 有 `context.agent.run_turn`，可指定会话并让框架发送；插件应先检测接口。用事件元数据关联本轮判断，在请求钩子末尾添加临时回复策略。唤醒输入要明确标为机器人定时触发，普通文本会参与本轮输入。QQ 文字交由框架发送，表情仍要等文字发送确认后才发一次。官方运行历史和平台送达是两种状态，输出失败也不会让官方运行自动回滚；试运行必须在提交前结束。[主动回合与元数据](https://github.com/AstrBotDevs/AstrBot/blob/v4.30.0-beta.1/astrbot/core/agent_service.py#L341-L414) · [运行和发送](https://github.com/AstrBotDevs/AstrBot/blob/v4.30.0-beta.1/astrbot/core/agent_service.py#L437-L507) · [官方装饰与分段](https://github.com/AstrBotDevs/AstrBot/blob/v4.30.0-beta.1/astrbot/core/agent_output.py#L332-L382) · [内置 Agent 压缩](https://github.com/AstrBotDevs/AstrBot/blob/v4.30.0-beta.1/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1202-L1222)
3. **继续支持 4.29.4**，最小的安全退路是让冷场问候仅使用有长度预算的近期对话副本，成功发送的片段记入插件自己的缓冲区，整个过程不覆盖官方会话。代价是普通主模型可能不知道这句问候；这条降级路径也没有官方自动压缩。如果必须让旧版主模型记住主动发言，再做专项适配，处理同一会话并发、切换会话和部分送达。旧版的 `add_message_pair` 需要一问一答，内部仍整份写回；定时任务的内部入口带有任务专用提示、投递和历史语义。两者都不能直接替代新版接口。[现有近期消息和发送记录](../main.py#L307) · [旧版历史接口](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/conversation_mgr.py#L355-L390) · [旧版定时任务实现](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/cron/manager.py#L563-L627)

旧版方案还需同步会话边界。当前插件按群聊或私聊保存近期消息。用户执行 `/new`、切换对话或执行 `/reset` 清空同一段历史时，都要让旧缓冲和未发出的定时问候失效。只比对会话 ID 识别不出同一 ID 下的重置。[插件缓冲](../session_store.py#L43) · [4.29.4 会话指令](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/builtin_stars/builtin_commands/commands/conversation.py#L110-L180)

## 怎样确认修好了

先在测试会话里查天气，让机器人冷场开口，再追问“工具刚才具体怎么说”。检查原工具调用与结果仍配对、旧的关联标记仍在。另测长会话触发压缩，定时器与新消息同时到达，以及 QQ 分段发送失败后没有重复正文或表情。以上是拟实施时的验收用例。

缓存另测。固定服务商、模型、工具和足够长的起始对话，在相近时间分别跑“普通回复 → 普通追问”和“冷场回复 → 同一句追问”。对比主模型的缓存命中和未命中 token，单列主动回合及随后追问，不把判断模型的用量算进去。DeepSeek 返回 `usage.prompt_cache_hit_tokens` 和 `usage.prompt_cache_miss_tokens`。缓存按服务商策略尽力复用，源码不能给出你服务器的命中率。[DeepSeek 命中统计与限制](https://api-docs.deepseek.com/guides/kv_cache/)

截至 2026-11-20，上述改法和验收用例属于研究建议。本次仅新增这份笔记。
