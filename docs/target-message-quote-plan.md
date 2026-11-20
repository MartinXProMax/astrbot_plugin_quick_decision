# 指定回复目标与平台引用方案

## 群聊中的预期行为

收到群消息后，保留「是否发言」作为入口。选了发言，才采用「这次是否要针对某一条具体消息回复？」的答案。这道题只有“是”和“否”，不指定消息。选“是”才采用「回复哪一条？」的答案。选“否”时，机器人可以自然接话。接到甲的最新消息却决定回答乙的旧消息时，会话模型得到乙的原文，QQ 引用也指向乙的消息 ID。

建议在群聊策略用一个「指定消息的引用方式」选择框替换「判断发言后直接在群里发送」。下表假设同一份模型正文被后处理拆成三条，选定的消息 ID 为 42。“—”表示没有引用气泡。

| 设置 | 单条正文 | 正文第一段 | 正文第二段 | 正文第三段 |
| --- | --- | --- | --- | --- |
| 无引用 | — | — | — | — |
| 仅首段引用（建议默认） | 42 | 42 | — | — |
| 每段都引用 | 42 | 42 | 42 | 42 |

“无引用”只控制 QQ 气泡，聊天模型仍会得到所选消息的发送者、时间和完整原文。自然接话、没有有效目标时，三种设置下的正文都没有引用。另一次独立生成的回复重新计算首段。单独发送的表情包、工具通知、错误提示均不占用正文首段。

## 1. 把实际消息时间放在选项旁

当前插件在判断状态里传递整数时间戳，目标选项只展示发送者和前 40 个字符。AstrBot v4.29.4 的 aiocqhttp 普通群消息转换用本机当前时间填写 `message_obj.timestamp`，并把原始 OneBot 事件留在 `raw_message`。延迟上报时，把前一个字段格式化会让旧消息看起来像刚发。OneBot 在群消息事件上提供 `time` 字段，含义是协议端报告的事件发生时间。[当前入站处理](../event_utils.py#L79-L110) · [目标选项构建](../decision_engine.py#L352-L414) · [适配器赋值](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L418-L425) · [OneBot 群消息格式](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/message.md#L42-L53)

在入站缓冲消息追加可空的发送时间，从原始群消息事件读取合法的 Unix 秒数。调度、合并窗口和冷场继续使用现有活动时间。缺失、非有限值、明显错位的单位或异常未来时间均标成“发送时间未知”。如展示接收时间，需明确标为“接收”。[缓冲消息](../models.py#L60-L98) · [会话活动计时](../session_store.py#L86-L99)

同一轮固定判断时刻和会话时区。两道题共用带时间的近期消息，选目标时再把时间直接列在候选旁。沿用最近八个缓冲条目中有 ID 的用户消息。列表用摘要，选定后取同一快照中的完整原文。举例，若本轮判断时刻为 2026-11-20 19:00:00 +08:00，选项可以这样显示：

| 消息 ID | 模型看到的选项 |
| --- | --- |
| 42 | 小林 · 2026-11-20 18:59:42 +08:00 · 18 秒前 · “今晚几点出发？” |
| 37 | 小周 · 2026-11-20 17:30:00 +08:00 · 1 小时 30 分钟前 · “午饭订好了吗？” |

默认提示让模型结合经过的时间和话题延续情况。已经换题时优先自然接话，有人明确重新提起旧问题时仍可针对旧消息。纯随机判断继续按权重抽样，时间提示只影响采用模型判断的题目。[会话时区](../decision_engine.py#L259-L278) · [现有随机语义](../decision_engine.py#L180-L207)

## 2. 接入现有并发判断

- 给布尔题增加可识别的用途「判断是否针对单条消息」，沿用现有编辑器的触发条件和「与上题一起问」。两条默认连接均选同批，即发言 → 是否针对单条、是 → 选择目标。长短和表情判断仍挂在各自的原条件下。两道目标题只在收到群消息且存在有效候选时参与，空列表直接自然接话。
- 选项说明应写出结果。“是”会采用有效的目标选择，“否”会自然接话。每条目标选项说明会向聊天模型提供该条原文，并按本轮的引用设置回复。保留现有 `latest` 兼容值，在选择器中显示为“没有合适的消息”。同批时模型仍可放弃不合适的候选，即使选“是”也只有真实候选 ID 才能生成引用。
- 复用调度器的条件采用逻辑。在现有公开判断入口内存接入这两条边，用判断服务替身运行三种情况。允许发言且布尔题为真时得到预选 ID；允许发言且为假时丢弃预选 ID；选择沉默时两道目标题都被丢弃。三种情况的目标题均与发言题同批，分别只调用一次判断服务，预览与执行一致。多一个布尔题会增加少量输入与输出。需要减少无效提问时，可在现有编辑器把选目标改为「命中后再问」。[调度与条件采用](../decision_engine.py#L437-L530) · [已有提问时机控件](../views/meme_manager/app.js#L851-L866)

## 3. 在发送正文时应用引用

1. 在本轮确定发言时保存选中的 ID 与引用方式。只对本插件接管的群聊模型正文处理 `Reply`，在最终发送链去除可能指向入站触发者的旧引用，再按上表把目标 ID 与正文放入同一消息。保留原有的显式 @ 和其他组件。插件直接发送与官方生成后发送使用同一条首段规则。[官方管线接入](../main.py#L539-L561) · [当前发送拦截](../reply_generator.py#L161-L189) · [插件直接发送](../reply_generator.py#L326-L389)
2. 非流式按本次模型结果及其装饰后的正文组件识别分段。首段以第一条包含非空正文且成功交给适配器的消息计算。第一段明确发送失败、后续段继续发送时，后续第一条成功正文仍能带引用。独立图片、语音等不能抢占首段。官方有时会将自动引用放在先发的图片上，需清理该结果自带的错误引用，同时放行其他来源的消息链。
3. 流式输出跳过装饰钩子，需在官方开始发送前安装本事件的处理。仅在属于该次模型结果的流式交付期间应用策略。缓存整条发送时只有一段，实时拆段时按实际出站正文计数，模型生成过程中插发的工具文字单独处理。结束或异常退出时解除本次状态。
4. 若最终待发链混有文件或合并转发，适配器会拆开发送每个组件。沿用这种拆分顺序，只把引用与属于正文的文本一起提交，附件仍独立交付。纯转发卡片没有可单独引用的正文，按原生格式发送。已核查普通图文和带文件混合链在模拟协议出口上的实际拆包差异，详见下方源码。

## 4. 升级与验收

- **群聊设置。** 新字段建议用 `group.target_quote_mode`，取值 `none / first / all`。已保存的新值优先。首次采用新版且缺少新值时，新装与升级统一使用 `first`。旧 `direct_send=true` 同时是过去的默认值，无法区分用户手选和沿用默认。升级说明写明：曾要求始终隐藏引用的用户应选择“无引用”。[旧配置说明](../_conf_schema.json#L100-L105)
- **已有判断图。** 对默认图做版本化、可重复执行的迁移。把原目标选择的上游触发条件及提问时机移给新增布尔题，目标选择改接“是”，默认同批。保留既有目标选择的 ID、候选配置和后继。对已删除或停用的目标题不重新启用；已有等价前置判断不重复插入。对 `latest` 上已有自定义提示、工具、后继或特殊权重等可能改变业务语义的图，保留用户配置并在编辑器指出可插入判断的位置。旧版本先走已有迁移，再应用本次迁移。配置默认值、导入、导出、预览和保存应使用同一版本，重复保存不会再添一道题。[已有迁移](../config_util.py#L151-L210) · [配置读写及预览](../web_api.py#L76-L153)
- **先验证候选和判定，再验证协议发送。** 为延迟上报、跨日、未知时间、零候选和同文不同 ID 建用例。覆盖同批与串行、沉默、明确目标、自然接话、放弃预选和来自旧配置的分支。对三种引用方式在单条和三段正文上检查最终 OneBot 消息中的 `reply.id` 及数量，分别开启和关闭 AstrBot 自带的自动引用。再覆盖中途有工具消息、首发图片、流式两种模式、插件直接发送以及发送异常。
- **QQ 联调案例。** 让甲发一条问题，乙随后插话，机器人选择回答甲，同时把回答拆成三段。分别查看三档模式下哪一段展示气泡，以及点击气泡是否定位到甲。再让机器人自然接话，核对没有误引乙。本次研究用本地替身验证判断批次及协议拆包，真实 QQ 客户端呈现待联调。

## 官方发送链路核查

核查范围为插件声明支持的 aiocqhttp 适配器和最低版本 AstrBot v4.29.4，固定提交 `61335414433db721b3f7ecc93f90deafe0a59d78`。下表描述发送给 OneBot 的请求，客户端最终呈现仍需端到端验证。[插件支持范围](../metadata.yaml#L7)

| 路径 | 消息何时带上引用 |
| --- | --- |
| 普通群聊文本 | 装饰阶段先调用 `on_decorating_result`，再加前缀、分段、执行转换。最后，链中所有组件均为 `Plain` 或 `Image` 且官方引用开关开启时，才会在前面插入指向**本次入站消息 ID** 的 `Reply`。此时也可能已经添加独立的 `At`。 [装饰顺序](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/result_decorate/stage.py#L158-L217) · [追加 Reply](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/result_decorate/stage.py#L414-L447) |
| 官方非流式分段 | 装饰阶段会为切出的每段文本新建 `Plain`。发送阶段分离出 `Reply` 和 `At`，将头部交给第一个发送成功的非 `Record` 组件；这一组件可能是先出现的图片。语音单发。关闭分段时，先单发语音，剩余组件一起送给适配器。 [重建文本段](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/result_decorate/stage.py#L217-L267) · [逐段或整链发送](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/respond/stage.py#L327-L403) |
| 混合附件与合并转发 | 普通图文可在同一次协议请求内带引用。只要待发链含 `File`、`Node` 或 `Nodes`，aiocqhttp 就改为逐组件调用协议；向这种链简单插入 `Reply`，会单独提交一个仅含引用的请求。长文本还可能在装饰时转成单个转发 `Node`。 [适配器分流](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L170-L208) · [自动合并转发](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/result_decorate/stage.py#L414-L431) |
| 流式 | `STREAMING_RESULT` 跳过装饰，直接交给 `event.send_streaming`。aiocqhttp 根据策略缓存并合并后发送，或按标点分段重建 `Plain`、非文本组件单发。流结束后，`STREAMING_FINISH` 在发送阶段直接返回。仅在装饰钩子中安装引用处理将错过已经发出的流式正文。 [装饰入口](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/result_decorate/stage.py#L126-L137) · [流式交付](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/respond/stage.py#L241-L309) · [aiocqhttp 两条流式路径](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L226-L261) · [结束处理](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/respond/stage.py#L198-L212) |

非流式可在真正发送前核对本轮结果对象、`result.is_llm_result()`、待发非空 `Plain` 与**装饰完成后** `result.chain` 中的组件是否为同一对象。官方的 `result.derive` 创建新的包裹链，但复用被发送的组件。这可识别普通分段，并避开同一事件中插件另建的提示消息。对面向未来版本的实现，应为这一引用关系保留兼容测试。 [派生消息链](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/message/message_event_result.py#L36-L47) · [结果类型](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/message/message_event_result.py#L265-L284) · [发送循环](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/respond/stage.py#L354-L398)

流式可利用固定版本的内部上下文变量 `streaming_response_event` 区分发送来源。平台发送期为本事件，推进生成器期间临时置空，工具或插件在生成期间额外发送的文字可由此排除。它只标识流式交付，具体能否引用仍须检查最终消息链含可引用的非空正文，且引用和正文能被一起提交到 OneBot。 [上下文变量切换](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/pipeline/respond/stage.py#L265-L299) · [新建流式文本段](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/platform/astr_message_event.py#L342-L353)
