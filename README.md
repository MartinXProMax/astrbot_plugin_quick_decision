# AstrBot 快速决策插件 (`astrbot_plugin_quick_decision`)

<div align="center">

**用独立的判断模型决定发言时机、回复策略与表情包配图，无缝融入 AstrBot 官方主 Agent 流程。**

</div>

## ✨ 项目简介

**快速决策（`astrbot_plugin_quick_decision`）** 是一款面向 AstrBot（`aiocqhttp` 平台）的独立决策插件。它在主对话模型回复之前引入轻量、独立的决策层（支持 **模型判断 / 纯随机采样** 以及 **按题混用**），智能控制机器人在 QQ 群聊与私聊中的**发言时机、插话节奏、冷场主动开口以及表情包发送策略**。

插件深度兼容 AstrBot 官方主 Agent 流程与分段回复（`segmented_reply`），无需任何额外 Python 第三方依赖，并内置可视化 WebUI 用于编辑多级判断问题与追溯决策日志。

## 🚀 核心特性

- **双模式决策引擎（模型判断 / 纯随机，支持按题混用）**
  - **模型判断（`model`）**：使用在 WebUI「模型提供商 → 判断」配置的独立判断模型（System One）进行决策，上下文与主会话模型完全隔离，不污染人设与对话记忆。
  - **纯随机模式（`random`）**：无需配置判断模型，直接按选项权重随机采样决定是否发言与配图。
  - **单题覆盖与同层混用**：支持针对特定的单个问题单独设置为随机决策，同一层的问题可以自由混用「模型判断」与「随机决策」两种模式。执行时，**随机题在本地按权重直接采样，不会调用判断模型**；**同层的模型题会自动合并到一次判断请求中完成评估**，兼顾策略灵活性与请求开销。无论采用哪种模式，最终回复正文均由主会话模型生成。
- **精细化群聊与私聊策略**
  - **群聊防刷屏与自然插话**。即使被 `@` 或引用也会先判断是否发言。针对单条消息时可引用选中的原消息，自然接话时直接发送。支持消息防抖合并（`debounce_seconds`）。
  - **私聊可控发言**：私聊默认正常回复，也可在控制发言的判断问题中开启「私聊也判断是否发言」。
  - **冷场主动开口**：支持群聊与私聊在沉默达到设定时长后主动发起话题，并配合每会话每小时上限与冷却时间防止过度打扰。
- **可编辑多级判断问题与答案工具**
  - 支持在 WebUI 中自由新增或编辑判断问题（选择题 / 是否题），可为单个问题独立设置随机决策，并为每个选项配置**选择权重**、**注入会话模型的临时提示（`<quick_decision>`）**、**后继级联问题**以及**工具绑定**（内置 `send_meme` 发送本轮选中的表情）。
  - 支持温度采样（`choice_temperature`）与生效阈值（`choice_threshold`），灵活调节决策的确定性与随机多样性。
  - 支持为判断模型单独填写**自定义上下文（`decision_custom_context`）**（如机器人昵称、群内定位），仅进入判断模型而不传给主会话模型。
- **官方 Agent 流程集成与分段/表情发送**
  - 私聊与普通 `@` 放行官方默认主 Agent；群聊未 `@` 插话与防抖合并后的入站消息通过 `event.request_llm(..., conversation=当前会话)` 接入官方主 Agent。
  - 兼容官方 `segmented_reply` 分段回复（含正则与发送间隔）；支持纯文字或「先发文字、再另发一条独立表情包」（同气泡不图文混排，不包含语音）。
- **表情包管理器无缝兼容**
  - 对接插件市场中的「表情包管理器」（`anka-afk/astrbot_plugin_meme_manager`），直接读取其 `semantic_metadata.json` 中的逐图语义描述生成候选并按 `id` 取图。
  - 未安装或缺失依赖时自动降级为纯文字回复并给出提示，不回退旧版自制图库。
- **试运行模式与异常降级保护**
  - **试运行（`dry_run`）**：仅执行决策并写入日志，不向 QQ 实际发送消息，也不放行官方默认回复，适合上线前安全调参。
  - **失败降级（`DecisionError`）**：判断提供商未配置、超时、HTTP 错误或返回异常时，群聊被动/插话及所有主动发言均保持沉默；私聊收到消息时，仅在发言题已经放行、后续判断失败时回退到官方纯文字回复。
- **内置 WebUI 管理与决策日志**
  - 在插件详情页提供「**判断配置**」（可视化编辑全部问题、用途、单题随机开关、权重、当轮提示与工具绑定，支持 `Ctrl+S` 保存即时生效）与「**决策日志**」（按日期分组，支持按发言 / 沉默 / 失败或降级状态筛选及关键词搜索）。

## 📋 环境与依赖要求

| 项目 | 要求 |
| :--- | :--- |
| **AstrBot 版本** | `>= 4.29.4, < 5` |
| **支持平台** | `aiocqhttp`（QQ 群聊 / 私聊） |
| **Python 第三方依赖** | 无（无需安装 `requirements.txt`） |
| **可选配套插件** | `anka-afk/astrbot_plugin_meme_manager`（表情包管理器，仅需配图时安装） |

> [!WARNING]
> **兼容性与冲突注意事项**
>
> 1. **请勿同时开启 AstrBot 官方「概率主动回复」**，以免与本插件的插话及冷场主动发言策略产生冲突。
> 2. **搭配「表情包管理器」使用时**：请进入 `anka-afk/astrbot_plugin_meme_manager` 配置，关闭「**启用主动发送表情工具**」，并将「**表情发送概率**」设为 `0`，避免同一轮回复重复发送表情包。
> 3. 本插件不依赖其他读空气插件；自 `1.2.0` 起已停用旧版自制图库及相关 `/meme` 指令与 `vision_provider_id` 配置。

## 📦 安装与快速上手

### 1. 安装插件

#### 方式一：AstrBot WebUI 在线安装（推荐）

1. 打开 AstrBot WebUI，进入 **插件** 页面。
2. 点击 **从链接安装**，粘贴本仓库地址：
   ```text
   https://github.com/MartinXProMax/astrbot_plugin_quick_decision
   ```
3. （或）下载本仓库 ZIP 压缩包，通过 **从文件安装** 上传（请确保解压后的根目录直接包含 `metadata.yaml`）。

#### 方式二：手动部署

将插件目录复制到 `<AstrBot数据目录>/plugins/astrbot_plugin_quick_decision`，例如在容器环境中：

```bash
cp -a astrbot_plugin_quick_decision /AstrBot/data/plugins/
```

### 2. 初始配置

1. **配置判断方式**
   - **使用模型判断（默认）**：在 WebUI「**模型提供商 → 判断**」中配置判断模型（System One），并在插件配置中选择对应的 `decision_provider_id`。
   - **使用纯随机模式**：在插件配置或插件详情页「判断配置」中，将 `decision_mode` 切换为 `random` 并保存。
   - **按题混用**：在全局使用模型判断时，也可以在插件详情页「判断配置」中为特定问题单独设为随机决策；同层的随机题不会调用判断模型，其余模型题会合并为一次请求发送。
2. **开启试运行验证（推荐）**
   - 首次上线建议先将 `dry_run` 设为 `true`，在群聊中观察一段时间。
   - 前往插件详情页的 **决策日志**，确认触发时机、概率阈值与问题流转符合预期后，再关闭 `dry_run` 正式启用。

### 3. 更新与重载

更新代码后，在 AstrBot WebUI 的 **插件** 页面找到「**快速决策**」，点击卡片上的刷新图标即可完成重载，无需重启整个服务。

## 💬 指令说明

| 指令 | 权限要求 | 说明 |
| :--- | :--- | :--- |
| `/decision status` | AstrBot 管理员 | 查看插件运行状态、当前会话最近一次决策结果、判断阈值以及表情包管理器对接状态 |

> [!NOTE]
> 除 `/decision status` 外，插件无其他聊天指令（原旧版自制图库的 `/meme` 指令已移除）。判断问题与决策日志均在 WebUI 插件详情页中管理。

## ⚙️ 配置项说明

以下配置项对应 `_conf_schema.json`，均可在 AstrBot WebUI 插件配置页中调整：

### 基础与决策控制

| 配置键 | 类型 | 默认值 | 取值范围 / 可选项 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `enabled` | `bool` | `true` | `true` / `false` | 是否启用快速决策插件 |
| `dry_run` | `bool` | `false` | `true` / `false` | 试运行模式：仅记录决策日志，不发送消息也不放行官方默认回复 |
| `decision_mode` | `string` | `"model"` | `"model"` / `"random"` | 全局决策模式：`model`（模型判断）或 `random`（按权重纯随机）；亦可在「判断配置」中针对单个问题单独设为随机决策 |
| `decision_provider_id` | `string` | `""` | Provider 选择器 | 判断模型提供商 ID（存在模型判断问题时使用） |
| `vision_provider_id` | `string` | `""` | Provider 选择器 | *(已停用，1.2.0 起废弃)* 原自制图库自动描述提供商 ID |
| `choice_threshold` | `float` | `0.5` | `0` ~ `1`（步长 `0.05`） | 判断选项生效阈值 |
| `choice_temperature` | `float` | `0` | `0` ~ `2`（步长 `0.05`） | 判断选项采样温度（调高可让次高概率选项也有机会被选中） |
| `context_message_limit` | `int` | `20` | `5` ~ `50`（步长 `1`） | 传入判断上下文的最近消息条数 |
| `debounce_seconds` | `float` | `1.5` | `0` ~ `8`（步长 `0.1`） | 决策前合并连续消息的防抖等待秒数 |
| `cooldown_seconds` | `int` | `300` | `0` ~ `3600`（步长 `10`） | 同一会话的决策冷却时间（秒） |

### 群聊策略 (`group`)

| 配置键 | 类型 | 默认值 | 取值范围 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `group.enable_interrupt` | `bool` | `true` | `true` / `false` | 是否允许在未被 `@` 时主动插话 |
| `group.target_quote_mode` | `string` | `first` | `none` / `first` / `all` | 指定消息的引用方式。可选无引用、仅首段引用、每段都引用。升级也默认仅首段引用，原先要求始终隐藏气泡的用户请选择 `none`。 |
| `group.enable_proactive_after_silence` | `bool` | `true` | `true` / `false` | 是否允许群聊冷场后主动开口 |
| `group.silence_seconds` | `int` | `600` | `30` ~ `21600`（步长 `30`） | 触发群聊冷场主动开口的沉默时长（秒） |
| `group.hourly_proactive_limit` | `int` | `3` | `0` ~ `30`（步长 `1`） | 群聊每会话每小时主动发言次数上限 |

### 私聊策略 (`private`)

| 配置键 | 类型 | 默认值 | 取值范围 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `private.enable_proactive_after_silence` | `bool` | `true` | `true` / `false` | 是否允许私聊冷场后主动开口 |
| `private.silence_seconds` | `int` | `1800` | `30` ~ `43200`（步长 `30`） | 触发私聊冷场主动开口的沉默时长（秒） |
| `private.hourly_proactive_limit` | `int` | `3` | `0` ~ `30`（步长 `1`） | 私聊每会话每小时主动发言次数上限 |

### 自定义上下文与问题配置

| 配置键 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `decision_custom_context` | `text` | `""` | 判断模型专属自定义上下文（如机器人身份、群聊背景），仅进入判断模型 |
| `decision_questions` | `text` | 默认问题 JSON | 判断问题列表配置（推荐在 WebUI 插件详情页「判断配置」中可视化编辑） |

### 自定义选项动作

在 AstrBot 配置项 `decision_questions` 中可直接填写版本 5 的完整 JSON。下面是从空配置重建发言判断的最小示例。删除 `join_chat` 后，`is_question` 仍会在收到消息时独立运行。

```json
{
  "version": 5,
  "questions": [
    {
      "id": "join_chat", "type": "boolean", "instructions": "现在适合加入讨论吗？",
      "triggers": [{"event": "message_received"}],
      "actions": {"true": [{"tool": "request_reply"}]}
    },
    {
      "id": "is_question", "type": "boolean", "instructions": "有人在提问吗？",
      "triggers": [{"event": "message_received"}]
    }
  ]
}
```

让后继题随发言题同批提问，在后继题填写 `"triggers": [{"question": "join_chat", "option": "true", "ask_with_parent": true}]`。只有上游选项实际命中才采用预答。冷场入口可订阅 `silence_elapsed`，后判断设置 `stage: "post_reply"` 并订阅 `reply_generated`，或依赖一个已生效的选项。完整动作参数、迁移行为和前端接口见[选项动作设计与接口](docs/decision-actions-redesign.md)。

### 旧版编辑器的生效条件和提问时机

在「判断配置」点击「添加问题」。选择“收到消息”“达到冷场条件”或某个上游选项作为生效条件。两种事件各自使用一道发言题。当前事件没有启用的发言题时保持沉默，包括私聊。私聊默认回复通过已启用的消息入口固定放行。

每个条件都可以选择**命中后再问**或**与上题一起问**。默认群聊把「是否发言」「是否针对某条具体消息」「回复哪一条」放在同一批判断，只有前两题都通过才采用目标。候选显示发送时间和距今时长，协议端缺少时间时标为未知。篇幅和是否配图也随发言题一起问，选中配图才另问具体表情。需要上题实际答案来理解题干时，应选“命中后再问”。

展开「判断流程 → 预览示例路径」可用草稿配置与示例答案查看批次。真实请求的耗时、采用和丢弃的题目显示在决策日志。选中一条具体消息时，会话模型会收到该消息的发送人和缓冲区原文。群聊引用仍遵循发送设置。

旧配置读取时转换，保存后写为版本 4。默认目标题会自动接到新的是否题。旧图中自行配置的目标权重、自然接话效果及分支会保留，可在判断编辑器调整。

## 🛠️ 工作原理简述

1. **消息监听与防抖**：插件监听 `aiocqhttp` 全量入站消息，在 `debounce_seconds` 窗口内合并连续刷屏消息。
2. **独立决策评估**：根据最近 `context_message_limit` 条聊天记录与 `decision_custom_context` 按生效条件与提问时机处理判断。同一层支持「模型判断」与「随机决策」混用——**随机题直接在本地按选项权重采样，不会调用判断模型**；**同层的所有模型题会自动合并到一次判断请求中完成评估**，最终产出是否发言（`none` / `reply`）、临时上下文提示（`<quick_decision>`）以及是否触发内置工具（如 `send_meme`）。
3. **主 Agent 接管与回复**：
   - 判定沉默（`none`）则拦截本次回复；
   - 判定发言（`reply`）则放行或通过 `event.request_llm` 接入 AstrBot 官方主 Agent 生成回复，并在 `on_llm_request`、`on_llm_response`、`on_decorating_result` 与 `after_message_sent` 生命周期钩子中完成上下文注入、分段回复兼容与独立表情包补发。
   - 沉默超时后，由后台定时器触发冷场主动发言流程并受每小时上限与冷却时间约束。

## 📁 项目结构

```text
astrbot_plugin_quick_decision/
├── main.py                  # 插件主入口（QuickDecisionPlugin）与生命周期钩子
├── decision_engine.py       # 决策引擎核心逻辑（模型判断、温度采样、纯随机与级联问题）
├── decision_tools.py        # 决策答案工具注册与执行（内置 send_meme）
├── official_handoff.py      # 官方主 Agent 入站流程接管逻辑
├── official_segment.py      # 官方分段回复（segmented_reply）兼容处理
├── reply_generator.py       # 主动回复生成器
├── meme_adapter.py          # 表情包管理器（semantic_metadata.json）适配器
├── config_util.py           # 配置解析与默认判断问题定义
├── session_store.py         # 会话状态、冷却计数与决策日志存储
├── web_api.py               # WebUI 管理后端接口
├── views/meme_manager/      # WebUI 前端界面（判断配置与决策日志）
├── docs/                    # 设计与架构文档（决策引擎、扩展设计、工具执行等）
├── tests/                   # 单元测试与官方管线验证脚本
├── _conf_schema.json        # AstrBot WebUI 配置 Schema
├── metadata.yaml            # 插件元数据
└── LICENSE                  # MIT 开源协议
```

如需深入了解内部架构与扩展机制，可查阅仓库 `docs/` 目录下的技术文档（如 `docs/decision-engine.md`、`docs/decision-extension-design.md`、`docs/decision-tool-execution.md` 等）。

## 📄 开源协议

本项目基于 [MIT License](https://github.com/MartinXProMax/astrbot_plugin_quick_decision/blob/main/LICENSE) 开源（Copyright © 2026 狼与葡萄酒）。
