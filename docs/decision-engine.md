# 统一判断引擎

> 下文记录 1.3.x 的实现。1.4 起，配置保存完整问题列表，按用途生成本轮题目，选中的答案可以安排工具。当前配置和示例见 [README](../README.md#问题和工具)。


已完成第一阶段：统一现有判断，不增加配置项或判断请求。旧配置无需迁移。工具准入、生图、TTS 和完整主动 Agent 迁移仍属于[后续方案](decision-extension-design.md)。

## 入口与数据流

`ask_decision()` 的参数和返回类型保持不变，内部依次执行：

1. `_build_questions()` 保留原有题目、文案、候选及提问顺序。
2. `_build_specs()` 将内置规则和自定义配置映射成 `DecisionSpec`，固定本轮候选、阈值、温度、权重及提示。
3. `evaluate_specs(context.decision, specs, state=..., provider_id=...)` 批量提问，返回按问题 ID 索引的 `DecisionOutcome`。空批次跳过请求。
4. `_assemble_result()` 将统一结果投影一次，得到旧调用者使用的 `DecisionResult` 字段。

`DecisionSpec` 位于 `decision_engine.py`，因为它包含官方 Question 对象；`DecisionOutcome` 和 `DecisionResult` 位于 `models.py`，不依赖 AstrBot SDK。

判断请求发出后修改配置或刷新候选，会从下一轮评估生效；本轮继续使用已经提问的快照。

## 题目规则

| 判断 | 选择规则 | 回退值 | 用途 |
| --- | --- | --- | --- |
| `action` | 温度选择后，以选中项原始概率过全局阈值；兼容旧 `join` | `none` | 发言许可 |
| 私聊被动 `action` | 不提问，固定值 | `reply` | 放行私聊默认回复 |
| `target_message_id` | 温度选择，只接受本轮消息候选，不使用全局阈值 | `latest` | 旧回复目标字段 |
| 自定义 choice | 温度及选项权重，不使用全局阈值 | 不产生提示 | 临时会话提示，兼容 `length` |
| 自定义 boolean | 默认以 0.5 为界；非默认权重或正温度时采样 | 不产生提示 | 临时会话提示 |
| `use_meme` | `P(true) >= choice_threshold`，不采样 | `false` | 表情许可 |
| `meme_id` | 温度选择后，以选中项原始概率过全局阈值 | `none` | 表情候选 |

全 1 权重、零温度时，选择题保留提供商原选。采样仍使用原 `_select_option()`，不预先移除低于阈值的选项。判断是否接受使用 `probability`，`confidence` 仅保留在原始数据中。

提问顺序保持原样；评估时回复目标仍最后抽样。这保证同一配置、模型答案和随机输入下，不会因抽样次序变化而选中不同的结果。

每道题的 `decision_mode` 可设为 `model` 或 `random`。省略时继承插件的 `decision_mode` 默认值。同一批次的随机题本地抽取，模型题合并为一次提供商请求，因此可以混用而不为随机题调用模型。日志中的 `decision_mode` 在两者混用时记为 `mixed`。

当前版本默认在 `pre_reply` 阶段评估。配置了可达的 `post_reply` 问题时，官方回复钩子和冷场生成流程在拿到正文后调度后台判断，向判断模型提供 `assistant_reply` 和 `previous_answers`。该阶段按原回合 ID 将结果写入日志的 `post_decision`。

## 统一结果与兼容字段

`result.outcomes[id]` 包含 `value`、原始 `probabilities`、发生采样时的 `selection_probabilities`、兼容用 `raw`，以及状态和原因：

| 状态 | 含义 | 原因示例 |
| --- | --- | --- |
| `ok` | 正常得到选项或是否值 | 空 |
| `missing` | 已提问但答案缺失 | `missing_answer` |
| `invalid` | 类型不符、非法选项或没有可采样项 | `answer_type`、`unknown_option`、`no_selectable_option` |
| `skipped` | 固定值或本轮没有提问 | `private_passive_default_reply`、`not_asked` |
| `fallback` | 原始概率不达阈值或整批失败 | `below_threshold`、`batch_error` |

`outcomes` 表示评估结果，不代表某个动作已经执行。例如 `meme_id` 可以选中一张图，但 `use_meme=false` 时，最终兼容字段仍是 `use_meme=false/meme_id=None`。文件是否仍可用继续由发送端检查。

现有 `action/target_message_id/length/use_meme/meme_id/custom_answers/session_hints/raw`、日志键和管理页面保持原有含义。`outcomes` 暂存于本轮结果中，当前日志不额外序列化它。原有调用者也可继续直接构造 `DecisionResult(...)`，新增字段默认为空字典。

只有实际发出的题目才接受答案或记为 `batch_error`。固定值和未提问项始终为 `skipped`，提供商多返回的答案也不参与抽样。

批次异常由 `evaluate_specs()` 向调用者抛出。主入口将结果标为 `degraded`；异常没有消息时，日志记录异常类型。群聊和冷场主动判断失败时不发送；正常模式下的被动私聊放行官方默认回复，不附加失败的决策。事件没有唤醒官方流水线或本轮合并了多条消息时，默认回复不会覆盖全部消息，改用 `request_llm` 提交本轮全部消息。试运行在所有场景均不放行回复。

## 后续如何新增判断

仅增加长短、语气等提示类问题时，继续使用现有自定义 choice/boolean 配置即可，无需改引擎。

新增内置判断时，将官方题目加入构造函数，并映射成 `DecisionSpec`，明确默认值、原始概率阈值、温度及用途。选择和解析继续复用 `evaluate_specs()`；如需新增结果用途，再扩充 `Binding` 和投影，执行代码放在具备相应能力的原流程中。

工具准入必须在 `on_llm_request` 获得实际工具集后再判断；输出转换必须等正文产生。引擎只评估题目，不执行工具、不转语音、不发送消息。当前版本尚未接入这些用途，也未改变官方入站回复对目标消息的使用方式。

## 验证记录

- 单元测试覆盖请求顺序、候选与配置快照、并发隔离、未提问项、批次失败，以及插件入口在普通模式和试运行下如何处理有消息或无消息的异常。
- 第一阶段重构时，与基线 `14bb895` 的旧解析器进行过 7,200 组差分比较（场景、阈值、温度、权重、答案和随机种子）。该记录早于上述异常及多余答案处理修复。
- 测试使用官方接口替身，尚未在真实 AstrBot、判断服务或 QQ 会话上验证；后续能力仍需各自的集成测试。
