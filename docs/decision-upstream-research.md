# 统一判断所需的 AstrBot 上游接口核查

核对日期：2026-11-18。仅研究官方源码，没有调用真实模型、工具或平台发送接口。

本笔记固定比较两个版本，避免将开发分支的新能力误写为本插件当前最低版本的能力：

- AstrBot `v4.29.4`：`61335414433db721b3f7ecc93f90deafe0a59d78`，本插件声明的最低版本。
- 调研时 `master`：`06fc50b43ea1c13093c91762f3b9361911362f9a`。这是开发提交；本次没有确认新增接口首次进入哪个稳定版本。

## 1. 判断接口可以直接复用

`context.decision.ask(questions, state=None, *, provider_id=None)` 接受以调用方自定义 ID 为键的问题映射，返回使用相同 ID 的答案映射。未配置正确的判断提供商时抛出 `DecisionError`，不会自行改用聊天提供商。[1]

官方不仅有是否题和选择题，还已经公开导出等级评分题：[2][3]

| 类型 | 输入 | 输出 |
| --- | --- | --- |
| `DecisionBooleanQuestion` | `instructions` | `DecisionBooleanAnswer.probability`，表示命题为真的概率 |
| `DecisionChoiceQuestion` | `instructions`、`criteria: Mapping[str, str]`，至少两个选项 | `choice`、选中项的 `probability`、完整 `probabilities`、`confidence` |
| `DecisionScoreQuestion` | `instructions`、从低到高的 2–10 个等级描述 | `score`、`legend`、完整等级 `probabilities`、`confidence` |

因此本插件可以先保留 boolean / choice 两种题型，新增工具、生图和 TTS 时改变的是结果用途，不必先增加新的官方题型。以后确需“回复意愿强度”之类数值才接入 score，避免本轮新增无实际使用的配置。[2]

System One 的实现把本轮所有问题放进同一个 HTTP 请求。它要求非空问题集合与非空判断说明；选择题至少两个候选，评分题 2–10 级。请求失败、缺少任一题答案、概率非法等会导致整次 `ask` 失败。[4]

答案校验包括：概率必须为有限的非布尔数且在 `[0, 1]`；choice 概率键必须与题目候选完全一致，概率和接近 1，返回的 `choice` 必须对应最大概率；score 的等级表和概率键也必须匹配。[5]

这意味着温度和权重继续由本插件在接收到合法概率之后处理，不能让上游返回一个非最高概率的 `choice` 来实现随机性。Boolean 可归一化为 `{true: p, false: 1-p}` 复用采样，但仍需保留“p_true 阈值”和“选中项最低概率”两种不同的接纳策略，才能兼容当前行为。

两个固定版本的 `decision.py` 与 `system_one_decision_source.py` 内容完全相同；上述判断接口在本次比较中没有差异。[1][4]

## 2. 工具判断应在本轮请求中缩小工具集合

两个版本的 `ProviderRequest.func_tool` 都是 `ToolSet | None`。`ToolSet` 支持 `add_tool`、`remove_tool`、`get_tool`，其 `tools` 保存实际 `FunctionTool` 对象。[6][7]

`v4.29.4` 的官方入站路径先 `build_main_agent`，完成请求、人格、插件和内置工具收集，再执行 `OnLLMRequestEvent`；之后才 reset / 运行 Agent。当前 master 也在构造原生 runner 前触发请求钩子。因此本轮工具判断可以在 `@filter.on_llm_request()` 读取最终候选后进行，并给本轮请求赋一个新集合。[8][9]

示意代码，只展示已核实的接口：

```python
from astrbot.core.agent.tool import ToolSet

# 在 on_llm_request(event, req) 中，根据已计算的本轮策略执行。
if req.func_tool is not None:
    req.func_tool = ToolSet(
        tools=[tool for tool in req.func_tool.tools if tool.name not in denied_names]
    )
```

这里复制的是集合，不修改共享 `FunctionTool.active`，不调用 `context.deactivate_llm_tool_async()` 之类全局开关。过滤以已授权的候选为起点，不主动把原本因人格、插件范围或其他设置被排除的工具加回来。后续其他插件的请求钩子仍可能修改同一请求，需在联调中确认钩子顺序；这不是对任意第三方 Agent runtime 的统一强制约束。[6][8][9]

本插件目前较早在消息处理函数中做首次判断，当时尚无官方组合后的 `req.func_tool`。实际实施宜分成两个阶段：

1. `pre_reply`：现有发言、对象、表情与回复风格，保留早期批量判断。
2. `before_agent`：在请求钩子里，使用真实 `req.func_tool` 的名称与描述批量判断受控工具，并过滤本轮集合。

这样只有启用工具判断时才增加一次判断请求，避免为了取得工具目录而复制官方人格/内置工具收集逻辑。工具参数继续由官方 Agent 生成和校验；是否“允许”不等于保证“必定调用”。要求必调某工具没有在此钩子上得到统一、跨提供商的强制语义，本轮不应承诺。

## 3. `on_using_llm_tool` 不是可靠的单次拒绝接口

官方确实有 `@filter.on_using_llm_tool()`，参数为 `event, tool, tool_args`。但在两个所查版本里，`MainAgentHooks.on_tool_start` 仅调用事件钩子，没有消费其布尔返回值；runner 捕获 `on_tool_start` 的一般异常并记录后继续执行工具。因此不能把 `return False`、抛一般异常或简单 `event.stop_event()` 当作“拒绝这一个工具调用并安全继续 Agent”的正式协议。[10][11][12]

若未来需要基于**具体工具参数**逐次判断（例如同一个搜索工具每次搜索内容不同），初始工具集过滤不足。需有明确的 executor 层拦截协议，返回与原 `tool_call_id` 对应的拒绝结果，由官方循环记入历史；当前插件钩子没有提供这样一个可直接复用的完整拦截接口。不建议通过修改共享 handler 或私自删历史绕过。[11]

官方 runner 会将 assistant 的工具调用与 tool 结果组成 `ToolCallsResult`，写入运行上下文，并追加到请求；找不到工具时会生成带同一调用 ID 的错误结果。历史保存由官方入站执行流程负责。因此应继续让官方 runner 处理调用、结果和历史，不由本插件伪造工具调用消息。[13]

## 4. 没有入站事件时的完整 Agent：两个版本区别很大

### v4.29.4

`context.llm_generate()` 文档明确：只调用模型，**不自动执行工具调用**。`context.tool_loop_agent()` 可以执行工具循环，但要求 `event: AstrMessageEvent`，其内部自行构造请求，默认使用基础空 hooks；调用方还要负责上下文、发送和历史等事项。它不是“给一个 UMO 就完整复用主 Agent”的入口。[14]

内部 `build_main_agent()` 同样要求事件，并处在 `astrbot.core.astr_main_agent` 内部模块。不能因为函数名含 main_agent 就把它当成稳定的无事件插件 SDK。[8]

因此保持本插件当前 `>=4.29.4` 最低版本时，冷场主动路径只能继续明确能力有限，或另做有事件适配器的专项实现与完整集成测试；不能仅把 `llm_generate` 替换为 `tool_loop_agent` 就宣称完全接入官方历史、装饰、TTS和 after-send。

### 调研时 master 的新入口

当前开发提交在 `Context` 中新增 `self.agent = AgentService(...)`，提供：

```python
run = await context.agent.run_turn(
    umo=umo,
    input_message=[PlainInput(text="结合当前对话自然主动开口。")],
    send_to_user=True,
    metadata={"quick_decision": {"decision_id": decision_id}},
)
result = await run.result()
```

`PlainInput` 位于 `astrbot.core.agent.input`。此示意是新版接口，不能直接放进当前最低版本分支使用。[15]

已沿源码确认该方法不是只执行模型：

- `source_event` 可以省略；服务通过 UMO 查找平台并构造 `AgentWakeupEvent`。没有来源事件时使用默认权限，不凭 UMO 获得管理员身份。[16]
- `metadata` 深拷到程序事件的 `agent_metadata`，不自动进入提示词或历史。可携带 JSON 形式的决策关联信息，但现有插件仅从 `event.extra['quick_decision']` 取结果，需增加新版 metadata 的识别/恢复方式。[17]
- 服务调用 `prepare_agent_input`，打开对应 runtime 会话并提交 turn；`send_to_user=True` 时执行框架输出，`AgentRun.result()` 等待执行、历史持久化及框架输出处理完成。[17][18]
- 输出处理使用 `ResultDecorateStage` 与 `RespondStage`；因此具备接回官方装饰与发送的实际源码路径，而非仅凭接口名称推断。[19]
- `send_to_user=False` 仍保存历史，也不拦截插件显式发送，不能当作本插件“试运行模式”。试运行应在提交 Agent turn 前停止。[17]

这是一条可用于未来冷场主动发言统一化的真实路径，但本次未确认其首个稳定发行版本。最小兼容方案是检测 `getattr(context, 'agent', None)` 及 `run_turn` 能力：新版本采用它，旧版本保留当前文字路径并不启用工具；另一个选择是在确认稳定版本后提高插件最低版本。不要导入已被 master 重组移走的旧内部 `astrbot.core.astr_main_agent` 路径。[15][20]

## 5. 建议交给主方案的实现约束

1. 对外继续暴露现有 `ask_decision()`，内部统一题目定义和答案选择；先不扩展 score 配置。
2. 保留发言、阈值、温度、权重和 fallback 的旧默认语义；共享引擎按阶段批量执行。
3. 工具第一版只做明确的“保留/排除”策略，放在 `on_llm_request` 对本轮 `ToolSet` 生效，参数与调用历史继续交给官方 Agent。
4. 工具和绘图的结果绑定必须指向已存在且已授权的能力；没有可用候选时跳过题目，不让配置动态导入任意 Python 处理函数。
5. 新版主动 Agent 路径以能力检测与源码固定提交为依据；旧版不得宣称支持完整工具循环或与官方装饰等价。
6. 测试覆盖本轮工具过滤不改变其他会话/全局工具，拒绝未知能力，缺工具时降级，两个 Agent 分支，metadata 关联，以及工具调用/结果对齐；真实工具和聊天供应商仍需集成验证。

## 来源

[1] [DecisionService，v4.29.4，L25–63](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/decision.py#L25-L63)。

[2] [官方题目与答案数据结构，L43–132](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/entities.py#L43-L132)。

[3] [插件 SDK 导出，包括 ScoreQuestion/Answer](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/api/provider/__init__.py)。

[4] [System One 批量构造、约束及请求，L95–179](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/sources/system_one_decision_source.py#L95-L179)。

[5] [System One 答案验证，L222–345](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/sources/system_one_decision_source.py#L222-L345)。

[6] [ProviderRequest.func_tool，L193–217](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/entities.py#L193-L217)。

[7] [ToolSet 方法，L78–120](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/tool.py#L78-L120)。

[8] [v4.29.4 请求钩子在 build 后、reset 前，L283–327](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py#L283-L327)。

[9] [master 原生 builder 请求钩子，L235–264](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/astr_agent_runtime/main_agent/builder.py#L235-L264)。

[10] [工具前钩子注册说明，L561–582](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/star/register/star_handler.py#L561-L582)。

[11] [v4.29.4 调用工具前通知后仍执行，L1279–1292](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1279-L1292)；[MainAgentHooks.on_tool_start](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/astr_agent_hooks.py#L45-L58)。

[12] [master 工具前通知后仍执行，L1648–1659](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1648-L1659)。

[13] [ToolCallsResult 组装与上下文追加，L1095–1144](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1095-L1144)；[未知工具生成配对错误结果，L1222–1248](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/agent/runners/tool_loop_agent_runner.py#L1222-L1248)。

[14] [Context.llm_generate / tool_loop_agent 实现，L184–368](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/star/context.py#L184-L368)。

[15] [master Context.agent 构造](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/star/context.py#L187)；[AgentService.run_turn，L341–414](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/agent_service.py#L341-L414)。

[16] [无来源事件的程序唤醒与权限，L267–339](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/agent_service.py#L267-L339)。

[17] [run_turn 输出/历史/metadata 语义，L341–414](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/agent_service.py#L341-L414)。

[18] [服务提交与输出处理，L437–506](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/agent_service.py#L437-L506)；[AgentRun.result，L129–141](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/agent_service.py#L129-L141)。

[19] [deliver_agent_turn 装饰和发送，L332–382](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/agent_output.py#L332-L382)。

[20] [master 官方 Agent runtime 重组后的 builder](https://github.com/AstrBotDevs/AstrBot/blob/06fc50b43ea1c13093c91762f3b9361911362f9a/astrbot/core/astr_agent_runtime/main_agent/builder.py)。
