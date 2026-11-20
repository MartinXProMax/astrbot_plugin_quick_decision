# 发言判断、触发条件与选项结果更新方案

调研基线为插件 v1.5.0（`8db78b7`），核对日期为 2026-11-20。拟议更新把“答案何时生效”和“何时向模型提问”分别配置。发言题最先生效，独立的后续题可以与它共用一次请求。

## 现有行为

| 核对项 | 源码和测试中的行为 |
| --- | --- |
| 收到消息与冷场 | 消息经过合并和群聊插话开关筛选后发起判断。冷场由每 15 秒的巡检发起，已有群私聊开关、沉默时长、同一冷场只评估一次、冷却和小时上限。两条路径把不同的 `scene` 传给同一个 `ask_decision`。 [消息监听](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L114) · [群私聊筛选](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L268) · [冷场巡检](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L419) |
| 共用的发言题 | 默认配置只有一个控制发言的 `action`，同时保存 `none`、`reply`、`proactive` 及冷场专用文案。运行时按本轮场景删去另一个发言选项。私聊收到消息在默认配置下固定放行，管理员可开启私聊判断。 [默认题](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/config_util.py#L77) · [私聊固定答案](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L289) · [动态选项](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L355) |
| 默认的首批判断 | 当前把没有父题指向的问题视为入口。新装默认将“是否发言、回复对象、篇幅、是否配图”放在同一层，具体表情由“是否配图＝是”触发。现有测试也覆盖了“沉默 → 篇幅”的分支。 [默认流程](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/config_util.py#L83) · [入口推断](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L284) · [逐层执行](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L391) · [沉默分支测试](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/tests/test_official_inbound_handoff.py#L1007) |
| 选项的信息 | 选择题传给判断模型的是题干和各选项的判断标准。选项另存会话提示、权重和工具，发言、选择目标等行为由用途和选项键在汇总时解释。布尔题只向官方判断接口传一段题干。 [选项定义](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/models.py#L109) · [布尔题](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L332) · [选择题](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L357) · [结果汇总](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L493) |
| 回复对象的具体言论 | 只有收到群消息时会询问目标。候选来自最近 8 条缓冲消息中有 ID 的用户消息。指定某条会向正文模型临时提供发送人和原文前 40 个字符，汇总另有 160 字符上限。`latest` 当前只沿用会话上下文，配置中却同时出现“针对最近一条消息回复”和“不针对某一条特定消息”两种说明。平台引用另受群聊直接发送开关控制。 [候选构造](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L319) · [默认文案](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/config_util.py#L34) · [目标落地](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L509) · [临时摘要](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/reply_generator.py#L25) · [群聊引用](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L548) |
| 管理页 | 问题卡片的入口条件由入边反推，只能阅读。每个选项最多选择一道“接着问”的题。选项效果一栏可编辑提示、工具和后继，发言与目标原文的内置效果没有列在这里。两种发言场景在同一张卡片切换，共用沉默选项的权重和效果。 [入口显示](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L752) · [场景切换](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L952) · [效果编辑](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L1013) |

本地以默认配置和模拟判断提供商复现：群消息选“沉默”时，首批仍提交 `action`、`target_message_id`、`length`、`use_meme`，四个答案均进入结果。群聊冷场选“沉默”也提交了三个问题。把唯一标记放到目标消息第 41 个字符，选中该消息后，会话模型的临时提示里找不到标记。当前基线通过 148 项 Python 测试和 4 项 Node 测试。

## 默认执行流程

1. 收到符合现有开关的消息时，先采用“收到消息时是否发言”的答案。达到冷场条件时，先采用“冷场时是否发言”的答案。两题可各自编辑题干、权重、模式和选项提示。日志保留事件和实际题目 ID。
2. “保持沉默”结束本轮。发言题缺答、无效、未过阈值，或本事件没有启用的发言题，也结束本轮。私聊“默认回复”作为消息发言题的固定“发言”结果记录。当前私聊判断异常可交回官方纯文字回复，建议只在发言题已经放行、后续判断出错时保留这个回退。 [当前异常处理](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L619) · [私聊交接](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L187)
3. 新装默认把回复对象、篇幅和是否配图随发言题提交，冷场省去回复对象。选“沉默”后丢弃这些预问答案。选“发言”后才采用它们，配图为“是”再问具体表情。两条路径复用现有正文生成与发送机制，工具按当前时序在正文确认发出后执行。 [消息正文交接](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L228) · [冷场自行生成](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L459) · [工具执行](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/reply_generator.py#L192)

## 选项结果如何表述

| 选项 | 管理页和判断模型应能读到的结果 | 执行时的内容 |
| --- | --- | --- |
| 收到消息时 · 发言 | 继续评估回复策略，并按本轮结果生成回复 | 允许收到消息后的正文流程 |
| 冷场时 · 主动开口 | 继续评估发言策略，并根据最近会话生成一条主动消息 | 允许冷场正文流程 |
| 保持沉默 | 结束本轮，丢弃同批预问的答案 | 生效的后续答案、后续请求、正文生成及工具调用均为零 |
| 指定某条消息 | 将选中消息的发送人和具体言论交给正文模型 | 保存本轮选中消息的 ID、发送人和缓冲区原文。选择列表可保留短预览，正文模型从同一次判断的快照读取具体内容，群聊引用遵循发送开关 |
| 默认目标（旧键 `latest`） | 自然参与当前话题，沿用本轮会话上下文 | 推荐保留当前不锁定消息 ID 的行为，并将界面中的“针对最近一条”改为与之相符的说明 |
| 其他选择与是非题 | 显示本选项记录的答案、交给会话模型的提示、安排的工具及会触发的判断。回复后题显示记入日志的结果和可继续执行的回复后题 | 只列出该题在当前阶段实际支持的效果。工具说明区分“选中表情”和“正文发送成功后尝试发送表情” |

建议由后端从选项用途和配置生成效果说明。WebUI 在“选中后”列显示内置结果及可编辑的提示、工具和后继。模型模式把简明的效果追加到各选项的选择标准，是非题将两种结果写入题干，沿用现有判断接口。随机模式执行同样的效果。沉默选项明确显示“结束本轮”，隐藏与之冲突的工具、正文提示和后继控件。

目标题的配置页可以写“把【发送人】说的【原文】交给会话模型”。实际选中时，从本轮冻结的候选里按 ID 取发送人和原文，放进临时结构化提示，避免候选摘要的 40 字和汇总的 160 字截断。只有附件时标记为“无文本”。保留标签转义、候选 ID 校验和现有的日志粒度。 [缓冲文本](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/event_utils.py#L88) · [临时内容](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/reply_generator.py#L47) · [未知目标用例](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/tests/test_decision_engine.py#L397)

## 每道题如何配置触发条件

保留一个“添加问题”。新增和编辑都在问题卡片内选择“生效条件”。选“收到消息”“会话达到冷场条件”等外部事件，问题就有了事件入口。选“某问题 → 某选项”，它就成为后继。用户可以在同一张卡片上更换事件或改接到上游答案。从选项旁新建后继时自动填好条件。 [现有添加菜单](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/index.html#L110) · [新建后继](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L1172)

- **发言优先级。** 默认配置每种事件只有对应的发言题直接启动。用户另设“收到消息”触发的普通自定义题时，它在该轮发言获准后执行，界面显示“收到消息且本轮已允许发言”。设为“控制发言”用途的自定义题可以接替该事件的发言题，同一事件的适用范围重叠时提示冲突。以后接入其他实际事件源，复用同一菜单和规则。现阶段也可利用已有的 @／引用标记细分收到消息的条件。 [现有会话范围与提及信息](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L268)
- **共享后继。** 每道题保存自己的触发条件列表，命中任一条件即生效一次。例如“篇幅”同时接受“收到消息时选发言”和“冷场时选主动开口”。多道题可以引用同一个父题选项。选项旁的“接着问”和流程图按同一份关系生成。 [当前单一后继限制](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L391) · [当前前端序列化](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L264)
- **触发和时机。** “什么时候触发”决定本轮要问哪些题，“回复前／回复写完后”决定何时有足够信息作答。已触发的回复后题等到获得正文再执行。保存时检查循环、不存在的问题或选项、回复后指回回复前、沉默分支仍接后继等冲突。删除父题或复制问题时显式保留或重选条件，失去来源的后继显示“待设置”，不会意外变成每轮都执行。 [现有校验](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/config_util.py#L435) · [复制问题](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L1178) · [正文后判断](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L455)

## 怎样设置“与上题一起问”

题卡展开后，每一行并列显示 **生效条件** 和 **提问时机**。例如在“篇幅”卡片上选择“收到消息时是否发言 → 发言”，旁边直接选“命中后再问”或“与上题一起问”。选后者时就地显示“上题选发言才采用这个答案”。折叠后仍能读到“收到消息选发言 · 与上题一起问”。“回复写完后”继续放在阶段字段，避免两个时间设置混用。

| 使用情形 | 编辑行为 |
| --- | --- |
| 一道题有多个来源 | 每个来源分别设置。例如收到消息时的“篇幅”可与发言题同批，冷场时的“篇幅”可等发言后再问。给整道题加一个开关无法表达这两种意图。 |
| 从上级选项添加后继 | 沿用“接着问”入口，预填刚才点的题和选项，直接显示提问时机。全页仍只有一个“添加问题”菜单。父卡摘要可跳回后继的对应条件行。 |
| 同一选项已有多个后继 | 在该选项的后继列表中提供“把这几题一起问”，一次更新这些子题对应的条件行，保留其他来源的设置。仅对本阶段可提前的模型题生效，复用表单现有的保存与撤销。 |
| 新增自定义题或更换来源 | 新条件默认“命中后再问”，用户可在同一行改为提前问。发言入口直接显示“收到消息时”或“冷场时”。普通自定义题直接选事件时，显示其隐含条件“本轮已允许发言”，提供“与发言题一起问”。 |
| 不能提前的问题 | 子题使用已选目标原文、候选要由上题生成，或要等回复正文时，显示“需先取得选中消息”或“需等正文生成”。同一阶段的回复后题仍可一起问。固定答案和纯随机题显示“本地判断”。对自由文本写出的依赖，提示作者选“命中后再问”，由作者决定，避免猜测题意。 |
| 看清改动影响 | 流程图保留真实的选项触发线，线旁显示“同批”或“命中后问”。沿一条示例路径预览每次请求包含哪些题，以及“已问但丢弃”的数量。仅显示发送次数和题目，不预估金额。 |

## 默认流程会发几次请求

下表是同一条可插话群消息、五道模型题、候选充足且请求成功时的推演。每格为“判断请求数 / 提交题数”，不计正文生成。三个基础后继分别是回复对象、篇幅、是否配图，第五题为具体选图。

| 提问方式 | 沉默 | 发言、不配图 | 发言、配图 |
| --- | ---: | ---: | ---: |
| 全部命中后再问 | 1 / 1 | 2 / 4 | 3 / 5 |
| **建议默认：三个基础后继随发言题，具体选图等配图为是** | **1 / 4** | **1 / 4** | **2 / 5** |
| 四个后继都与各自上题一起问 | 1 / 5 | 1 / 5 | 1 / 5 |

默认方案下，沉默时丢弃三个答案，发言且不配图时一次请求就拿齐四个有效答案。若只需要部分合批，可单独把“篇幅”等某一题提前，其他条件照旧。群聊中绝大多数消息都沉默、或目标候选很长时，可以把相应题切回“命中后再问”。实际延迟还包含每次请求的服务耗时。按实际提供商用量比较费用时要同时计入重复发送的上下文和丢弃题的输入。

## 配置与运行时实现

**配置只存一份触发关系。** 建议持久化格式升到 3，编辑器协议升到 5。每道题存 `triggers`，一个来源写事件或“上题 ID＋选项”。仅提前提问的来源写 `ask_with_parent: true`。下面是共享“篇幅”题的字段示例，省略既有题干、选项等字段。

```json
{
  "id": "length",
  "triggers": [
    {"question": "action", "option": "reply", "ask_with_parent": true},
    {"question": "idle_action", "option": "proactive"}
  ]
}
```

发言题分别写 `{"event":"message_received"}` 和 `{"event":"silence_elapsed"}`。在合法的事件、题型和阶段内，配置校验沿用现有选项、无环和去重检查，并校验调度字段、发言题用途与适用范围。前端编辑时沿用问题对象引用处理改名和排序，保存时转 ID。 [配置解析](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/config_util.py#L378) · [表单引用](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L233)

**调度按以下顺序实现。**

1. 以本轮事件、已启用问题和阶段确定唯一发言入口，固定配置、消息和候选快照。先结算已到达的本地固定题和随机题，例如私聊固定“发言”无需请求。已知为沉默就直接结束。 [现有快照及私聊默认值](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L279)
2. 从本次必须问的模型题出发，把同事件、同阶段、输入已就绪且设为“与上题一起问”的后继加入同一批，沿连续勾选的关系递推并按题 ID 去重。遇到“命中后再问”就停在该边。每一批只调用一次 `decision.ask`。子题题干写明假设，例如“假设本轮决定发言，回复应当多长？”。共享的 `previous_answers` 只包含已生效答案。举例说，要依据上级实际选中的那条消息分析语气时，应等上级结果。 [现有批量请求](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L216) · [上游答案传递](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L420)
3. 将官方返回的按 ID 索引的原始答案暂存于本轮。按依赖顺序结算，先对已激活的上级使用原有权重、温度、阈值和兼容键（例如 `join → reply`），再判断选中了哪条分支。只有命中才调用子题的答案解析或随机抽样。沉默、缺答、无效或低于阈值都不能激活发言分支。分开当前 `evaluate_specs` 的请求与解析两步，复用 `_evaluate_answer`，保留同层生效题原有的稳定抽样顺序。 [答案处理](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L100) · [分支匹配](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L391)
4. 连续同批的多级后继逐层采用。多来源满足“任一命中”即可生效一次。一次请求中共享子题只提交一次，预问结果记录来源。若其中一条来源失败，先等本批其他相关来源结算。仅另一条标明“命中后再问”的来源最终命中时，用它已确认的上下文正式提问。相同来源不会重复发问或绑定两次工具。已失去全部有效来源的答案只记录丢弃状态，不进入 `answers`、`raw` 生效答案图、下一批上下文、会话提示、目标引用、工具计划或输出后判断。既有 `_assemble_result` 也要检查生效状态，覆盖当前按用途直接设置动作和目标的路径。 [结果汇总](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L493) · [答案与日志](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/models.py#L229)
5. 已提交的混合批若被提供商整体拒绝，记录批次失败；未获得发言许可时结束。第一版不自动拆批重试，以保证实际调用次数可解释。已明确允许的私聊在后续判断失败时沿用官方纯文字回退。重判、并发会话、插件重载时，缓存按单次评估快照隔离；回复后批次等到实际正文到达，在本轮的计划内结算。 [重判及异常交接](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L161) · [回复后快照](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L455)

**成本与日志。** 日志按批记录耗时、提交的题目以及哪些被采用、丢弃或出错，既有生效答案与概率仍供旧页面读取。避免把预问内容或目标长原文额外落盘。当前 `build_state` 会给整个流程带上 `meme_catalog`，仅延后选图未必能节省这部分输入。按批复用共享状态时，保留旧自定义题已能读取的信息；内置题独立成批时可只传其所需数据，尤其要核对“是否配图”是否用到图库描述。对选图候选规模和上下文超限保留可见原因，不能静默删候选改变概率。 [现有共享状态](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L235) · [日志写入](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/main.py#L385)

## 实施顺序与验收

| 顺序 | 改动与完成条件 |
| --- | --- |
| 1. 配置和迁移 | 在 `models.py`、`config_util.py`、`_conf_schema.json` 加入明确的触发条件和两道发言默认题。同步 Web API 的编辑器版本和持久化格式。当前编辑器可报版本 2、3、4，落盘统一为版本 2，需分别处理。旧数组先按旧规则展开，完整旧配置按实际用途定位发言题，保留改名和用户删除。首次读取只在内存迁移，保存新的格式后旧页面提交应提示刷新。 [加载旧配置](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/config_util.py#L461) · [Web 读写](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/web_api.py#L51) · [默认配置一致性测试](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/tests/test_v11_config.py#L163) |
| 2. 发言优先与分批执行 | 一套调度逻辑覆盖在线请求、本地答案解析和输出后判断。按上表三种配置和三条结果核对每一批的题目 ID、调用数、生效答案与丢弃数。另测四种场景、禁用父题、阈值回落、混用本地随机和模型、共享后继、连续同批与中间等待、一个不可用预问题导致整批失败、并发会话和试运行。给丢弃答案绑定提示和工具作为反例，断言没有泄漏、额外抽样或发送。 [在线调度](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L404) · [本地解析入口](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L542) · [已有分支测试](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/tests/test_decision_engine.py#L435) |
| 3. 选项效果与回复原文 | 接入后端生成的选项说明与目标消息快照。判断请求能看见选项后果，会话请求能收到已选长消息的末尾。合并消息或判断期间有新消息到达时，发送人、正文和 ID 仍来自同一候选。未知 ID、无文本、`latest`、直接发送开关和工具执行时机均单独验证。 [候选快照测试](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/tests/test_decision_engine.py#L314) · [临时提示](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/reply_generator.py#L54) |
| 4. 编辑器和联调 | 更新表单、流程图与日志，覆盖新增、逐来源改时机、删除、撤销、重命名、旧编辑器保存保护和重载。预览用默认分支与后端调度的相同用例对照。用同一批群聊和冷场输入做两类验证：先在试运行对照每轮请求数、题数、丢弃数和耗时，再用测试会话核对正文提示、引用和实际发送次数。按提供商用量记录比较费用，在足量样本上比较中位和 P95 延迟。 [流程图](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L581) · [表单保存](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/views/meme_manager/app.js#L1243) · [旧编辑器保护测试](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/tests/test_official_inbound_handoff.py#L741) |

迁移旧发言题时，分别带入收到消息与冷场的提示、权重、决策模式和启用状态。共用的沉默标准按原来的冷场覆盖规则展开，原 `reply` 和 `proactive` 分支分别归到对应入口。原先独立执行的策略题挂在适用的“发言”选项下，已有正常父子关系、后继阶段及动态候选 `*` 条件保留。遇到原配置让“沉默”继续追问、或把发言题藏在其他题后面的情况，保留原配置并在编辑页标出受影响题，处理前只停用冲突路径。显式空列表、已停用或已删除的发言题按新规则表示相应场景不发言，包括私聊。 [旧分支语义](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/decision_engine.py#L391) · [空列表测试](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/8db78b7ba9e66fe7202786181fe1096599103e21/tests/test_question_tools.py#L105)

旧版原本与发言题同批的独立策略题，迁移为随适用的发言入口同批提交、选中发言才生效。原本由显式分支触发的题按“命中后再问”迁移，以保留它读取上题实际答案的能力。拆分发言题时，保留原 ID 供消息入口使用，给冷场入口分配不与现有题冲突的 ID，然后分别重连原选项。恢复新装默认和迁移旧配置各自做往返测试。

本次对交互示意中的四条触发关系遍历了 16 种提问设置和三种分支结果，在 Chromium 中验证了 48 组请求数、题数、丢弃数和批次显示。插件现有 148 项 Python 测试与 4 项 Node 测试均通过。这些是当前基线与设计验证，真实提供商的延迟、用量以及更新后插件行为属于上述联调验收项。

## 官方批量接口核查

- AstrBot v4.29.4 的 `DecisionService.ask` 按题目 ID 接收一组题、选定一个提供商，返回按 ID 索引的答案。System One 适配器把一个 `model`、一份 `state` 和整组 `questions` 放进同一个 POST。请求须有题，题干须非空，选择题至少两个选项。布尔题在该版本只发送 `instructions`。[官方服务](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/decision.py#L31-L63) · [请求构造](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/sources/system_one_decision_source.py#L95-L179)
- TypeSafe Jev 同批各题独立、并行地读取同一份状态。单纯依赖某个选项才能**生效**的题，可以预问，由插件按有效分支采用答案。需要上游真实答案来确定题干、候选或补充材料的题，须等该答案到手才能提交。Jev 按上下文大小限制批次，题数本身没有单独上限；每道选择题最多 255 项。1.13 和 1.14 默认要求“状态＋全部题”在 64k token 内，“状态＋任一题”在 32k token 内。[并行与依赖说明](https://docs.typesafe.ai/primitives#ask-multiple-questions-together) · [候选数](https://docs.typesafe.ai/primitives/choice) · [模型限额](https://docs.typesafe.ai/models#context-length)
- 超时、HTTP 错误、响应格式非法、漏答任何一道题，或任一题的概率未通过校验，都会使该次 `ask` 整批失败。选择题还须覆盖全部候选，概率之和接近 1，返回的选中项须为最高概率之一。故未激活题若随批预问，也须收到合规答案才能取回同批的发言判断。[请求错误](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/sources/system_one_decision_source.py#L178-L232) · [答案校验](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/sources/system_one_decision_source.py#L282-L345)
- 2026-11-20 核对的 TypeSafe 直连价为 Jev 1.13／1.14 每百万输入 token 0.042 美元，输出免费。合批时共享状态只计一次，每道额外预问的题干、选项仍占输入。分批能省掉未触发题的这一部分，后续已触发批次会重传状态。哪种更省取决于聊天上下文、题目长度及分支命中率。插件可直接记录判断请求数和题数，精确费用需核对实际提供商的用量记录，因为此版 AstrBot 向插件返回的只有答案映射。[计费](https://docs.typesafe.ai/models) · [同批成本](https://docs.typesafe.ai/patterns/noul-screen) · [适配器返回值](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/sources/system_one_decision_source.py#L316-L345)
