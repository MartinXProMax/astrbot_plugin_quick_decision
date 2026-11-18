# 自定义判断结果直接调用工具的实现方案

2026-11-19 · 研究基线 `2051fa7` · 设计提案。适用于插件声明的 AstrBot `>=4.29.4,<5`，上游执行接口的实查范围见文末。[插件元数据](../metadata.yaml)

1.4 首版已实现问题工具与 `send_meme`。配置在选项的 `tool` 或是否题的 `true_tool` / `false_tool` 中保存名称。参数从本轮启用的「选择表情」问题取得，问题可以改名。触发来源通过编辑或停用问题管理。下文保留调研提案，实际用法见 [README](../README.md#问题和工具)。


建议给每个自定义答案增加一个可选的「调用工具」，可以与已有的回复提示同时生效。第一个工具是 `send_meme`。选图仍由本轮判断模型完成，工具拿到确定的图片 ID 后直接发送。自定义题和内置题使用同一条结果处理路径。本文只设计工具调用，生图不在范围内。

## 一轮回复怎么走

1. **判断**。继续把自定义题、发言、回复目标和表情候选放在同一次 `decision.ask()`。用提问时的候选和配置快照完成现有的阈值、温度和权重选择。保留采样次序，整轮模型失败沿用私聊放行、群聊沉默的行为。[题目、快照与选择](../decision_engine.py) · [现有兼容测试](../tests/test_decision_engine.py)
2. **生成本轮计划**。扩展 `_assemble_result` 的既有投影。所有结果在此一次解释，得到发言方式、目标消息、临时提示和待调用工具。生成计划只处理数据。普通自定义答案只在 `outcome.status == "ok"` 时产生调用。私聊固定回复、`latest` 和旧 `join` 则继续使用各自的默认语义。旧日志字段从同一份结果投影，避免两套答案发生分歧。[结果投影](../decision_engine.py) · [结果结构](../models.py)
3. **执行**。入站在合并新消息、取得最终决策并通过沉默、冷却、试运行检查后才接纳计划。冷场另需通过主动次数和间隔检查。临时提示在生成正文前应用，`send_meme` 在正文发出后运行。一次重新判断只会替换计划。不要在配置保存、解析答案或重放日志时触发工具。[入站与冷场](../main.py) · [消息发送](../reply_generator.py)

| 现有判断 | 统一后的用途 | 需要保留的行为 |
| --- | --- | --- |
| `action` | 决定本轮能否继续 | `none` 取消回复及所有工具。私聊固定 `reply` 虽然标为 `skipped`，仍可放行 |
| `target_message_id` | 给本轮回复设置目标 | 校验本轮消息 ID，保留 `latest` 与现有引用/直发规则 |
| `length`、语气等自定义答案 | 追加临时提示，也可安排一个工具 | 选项权重与温度仍只决定答案。执行时不二次抽样 |
| `use_meme`、`meme_id` | 一个提供默认触发条件，一个提供 `send_meme` 的参数 | 保留当前阈值、候选 ID 与 `none`，同时保留 `use_meme/meme_id` 的日志视图 |

发言、引用和提示可以直接写进现有 `DecisionResult`。给每个工具调用保存来源题、选项、工具名与已解析参数，执行时机由注册的工具决定。这样新增工具只需登记参数怎样取得、在哪一步运行、如何处理结果，决策引擎不用再增加一种题型。

## 表情包如何成为第一个通用工具

`send_meme(meme_id)` 只负责在本会话向平台发送一张已有的图片。复用 `MemeManagerAdapter.resolve_image()` 和当前发送函数。调度方负责正文发送顺序、分段间隔和至多一次消费。选图本身已有统一的 `meme_id` 题和动态候选，取其 `DecisionOutcome.value` 即可。特别要在 `_assemble_result` 将旧 `use_meme=False` 对应的 `result.meme_id` 清空**之前**读取原始选图结果，否则自定义答案即便触发工具也拿不到已选图片。[候选和选择题](../decision_engine.py) · [图库解析](../meme_adapter.py) · [现有追加发送](../reply_generator.py)

建议界面明确提供表情包的触发来源，默认「内置判断」，也可切为「自定义答案」。例如选「调侃」就触发，选「认真」只注入提示。在自定义模式下仍与其他问题一起问 `meme_id`，省去 `use_meme` 判断，切回内置时恢复。此时决策结果页将内置触发题标为「由自定义答案接管」。多个答案同时选中 `send_meme` 时，一轮合并为一次并保留各自来源。`meme_id=none`、低于原阈值、已移出图库或文件不存在时只发正文。在内置模式下，编辑器应提示先切换触发来源才能启用自定义表情绑定，避免两个来源同时决定是否发图。

入站沿用事件上的 `quick_decision`，在 `after_message_sent` 一次性取走本轮计划，依据现有平台发送标记和文字链判断是否可以发图。这条路径取得的是插件可观察到的发送信号，没有逐段送达回执。冷场沿用本插件的分段发送，只有所有正文段成功才发图。两条路径共用同一个工具处理函数，保留独立发图、不引用、发送失败不自动补发的语义。执行范围是本进程、本轮至多一次。[官方发送后钩子与冷场分段](../reply_generator.py) · [现有发送测试](../tests/test_meme_followup.py)

## 配置和工具接口

建议保留原有 `session_hint` 和 `weight`。选择题在选项上加可选 `tool_call`，是否题对称使用 `true_tool_call`、`false_tool_call`。每个答案先支持一个调用。以下是选项的**建议格式**，字段尚未实现。

```json
{
  "id": "playful",
  "criteria": "适合开个无伤大雅的玩笑",
  "weight": 1.5,
  "session_hint": "语气轻松一点。",
  "tool_call": { "name": "send_meme", "args": { "meme_id": { "answer": "meme_id" } } }
}
```

工具目录由后端维护。每项描述名称、参数类型、可用场景、执行时机和处理函数。`send_meme` 同时支持入站和冷场，其时机固定为正文发出后。WebUI 显示中文的「发送表情包 / 使用本轮选图」，从后端目录生成可选项。参数只允许工具声明的常量或指定类型的答案引用。解析 `answer` 时只读取本轮有效结果和候选快照。执行的目标会话取自运行上下文，文件路径、任意表达式、Python 函数名不作为可填写参数。遇到停用的题、无值的引用或已失效的工具，显示具体原因，保留普通回复。[现有配置校验](../config_util.py) · [Web 保存入口](../web_api.py) · [表单往返](../views/meme_manager/app.js)

新工具通过同一目录增加处理函数。对其他插件，应优先适配其公开、可独立调用的业务接口。只有 Agent `FunctionTool` 的工具，接入前逐项验证所需事件、人格/用户授权、钩子、参数、输出和发送语义。依赖真实事件的工具只在可获得该事件的路径列为可用。若后续适配必须依赖 `req.func_tool` 的工具，在该轮实际请求上核对可用的工具实例，不从全局注册表补齐。`send_meme` 默认仅由决策计划执行。若将来同一个第三方工具既允许主 Agent 调用又已被本轮决策直调，对具有发送等副作用、约定一轮一次的工具，应在这一轮复制并过滤交给 Agent 的工具集，保留其他轮次的可用工具。具体上游依据见文末。

## 实施顺序与验收

1. **先迁移现有行为**。在 `models.py` / `decision_engine.py` 为本轮结果增加调用列表，旧 `use_meme + meme_id` 生成默认调用。把 `main.py` / `reply_generator.py` 两条现有发送入口都切到同一个分派函数，同时移除原来的独立触发，避免迁移期间发两次。用现有判断与发送测试验证私聊默认回复、`none`、阈值、权重、相同随机输入下的选项、试运行、分段失败、钩子重入和缺图。
2. **再开放自定义绑定**。串通 `models.py`、`config_util.py`、`_conf_schema.json`、`web_api.py` 与网页编辑器，保留严格保存与宽松加载的既有区别。验证旧 JSON 加载保存、boolean 两侧与 choice 逐项往返、工具和提示共存、非法参数与类型、切换触发来源、两题同时命中、最后一次判断覆盖早先答案，以及两个会话并发不串图。[旧配置测试](../tests/test_v11_config.py) · [入站路径测试](../tests/test_official_inbound_handoff.py)
3. **让日志区分选中与执行**。本项目先写决策日志，稍后才发消息。为新记录增加轮次 ID，记录调用来源、工具名和计划状态，执行后按 ID 合并或更新同一轮，保持旧日志可读和概率条形图完整。结果区分已执行、已跳过（含原因）、失败和未确认。dry run 仅显示计划。避免记录完整工具参数或聊天文本作为调用结果。用真实 AstrBot + aiocqhttp 最少验证普通入站、合并入站、冷场和发图失败四条路径，测试环境使用记录调用次数的工具，最后只对测试会话发送一张表情。[决策提前落盘](../main.py) · [日志存储](../session_store.py) · [概率显示](../views/meme_manager/log-probabilities.mjs)

## 已安装的第三方工具怎么接入

以一个接受字符串、返回一段文本的查询工具为例。管理员在选项中绑定已适配的工具名和参数。答案命中后，先记录意图；普通入站与合并入站均在本轮 `on_llm_request(event, req)` 中取得实际工具实例。适配器检查工具仍启用、用户有权限、参数符合工具声明，随后在第一次执行前标记该轮该调用已消费，重复进入请求钩子时复用结果。若工具确实只返回数据，就把限长的结果作为本轮临时参考资料交给回复模型，和用户指令区分。对于自行发送消息的工具，应登记其发送时机和结果归属，避免重复发送。冷场只有会话标识、没有原始事件，依赖真实事件的绑定显示「此场景不支持」并跳过。[本插件入站与冷场](../main.py) · [官方请求工具集及钩子时序](decision-upstream-research.md)

对仅提供 AstrBot `FunctionTool` 的候选，先用一项确实要接入的工具做版本验证。用本轮工具实例及真实运行上下文调用下节的执行器，消费异步结果并处理取消、超时与错误。若仅靠当前请求钩子不足以构造该工具需要的上下文或保留其授权、钩子及输出语义，就保留给官方 Agent，网页标注「仅 Agent 可用」。不要因直调失败再让 Agent 自动重试同一操作。验收用一个临时只读工具验证模型调用次数、仅限本轮的授权、卸载后跳过、长结果截断和异常后正文仍能生成，再逐个开放通过验证的工具。

## AstrBot 工具执行接口核查

按插件声明的最低版本 `v4.29.4` 的官方源码核查（固定提交 `61335414433db721b3f7ecc93f90deafe0a59d78`）。真实聊天平台、第三方插件及 MCP 服务尚需在目标部署联调。

- **可见性。** `Context.get_llm_tool_manager()` 返回全局注册表。官方主 Agent 先按人格与启用状态选工具，随后按事件允许的插件修剪集合，MCP 等另有处理。配置中存在名字不代表当前会话可调用。要对接已安装的外部工具，应从这轮官方实际提供的候选开始，同时核对工具自身的权限和调用条件。[工具管理器](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/star/context.py#L397-L399) · [人格与启用状态](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/astr_main_agent.py#L650-L672) · [事件插件范围](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/astr_main_agent.py#L1089-L1115)
- **直接执行。** 官方内部 `FunctionToolExecutor.execute` 可以在参数齐备时执行工具，无须模型先生成调用。但它返回异步迭代器，并区分本地、MCP、子 Agent 交接等类型。本地工具需要带真实 `event` 的运行上下文，可能用 `handler`、重写的 `call` 或 `run`，部分结果由执行器直接发回事件。MCP 工具依赖已连接的客户端、原始远端工具名与超时。仅凭名称直接 `await handler(...)` 无法涵盖这些行为。[分派](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/astr_agent_tool_exec.py#L135-L182) · [本地执行及消息发送](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/astr_agent_tool_exec.py#L685-L791) · [MCP 调用](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/mcp_client.py#L794-L821)
- **调用记录与运行环境。** 官方 runner 在执行器外查找本轮工具、处理参数、触发开始和结束钩子并将 tool-call 与返回结果配对追加到请求。单独调用执行器不会自动获得这些语义。`context.llm_generate()` 是聊天模型请求，`tool_loop_agent()` 则启动依赖事件的 Agent 循环。已选表情包有明确参数和自有发送方法，直接执行插件登记的动作即可；日志记录实际发送结果，不造一轮模型调用。将来接外部工具时需验证其输出是否由工具自行发送、由调用方消费，及所需的事件和工具权限。[runner 工具查找与调用](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1222-L1292) · [工具结束钩子](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1398-L1410) · [结果配对](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1095-L1144) · [Context 的两个生成入口](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/star/context.py#L184-L368)
