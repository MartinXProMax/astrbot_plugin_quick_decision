# AstrBot 官方插件市场上架调研

核对日期：2026-11-17。范围：官方发布入口、当前发布页面和公开配置、本仓库的提交资料；未登录代为提交或触发审核。

## 应使用的新入口

本插件应通过 **[AstrBot Cloud 发布页](https://cloud.astrbot.app/publish)** 提交 GitHub 仓库，需注册 AstrBot Cloud 账号。当前 AstrBot 主仓库的发布文档已经明确给出此入口；WebUI 市场底部的「提交我的插件」也指向同一页面。官方插件集合仓库已经标记 `DEPRECATED`，要求改用新平台。[1][2][9]

旧的独立文档仓库 `AstrBotDevs/AstrBot-docs` 的 `v4` 分支仍保留 `plugins.astrbot.app` 右下角 `+` → 填表 → GitHub Issue 的流程。其对应文件最后一次修改为 2026-02-18；集合仓库改为新平台的公告提交为 2026-09-14。不要据此旧分支另开发布 Issue，也不需要修改旧的 `plugins.json`。[1][10]

## 当前平台可用状态

2026-11-17 读取 Cloud 的公开配置接口得到以下结果；这些是平台运行配置，今后可能调整。[3]

| 配置 | 本次观察 |
| --- | --- |
| 插件发布 | 开启：`publish_enabled: true` |
| GitHub App | 开启：`github_app_enabled: true` |
| ZIP 上传发布 | **关闭：`zip_upload_enabled: false`** |
| 单账号同时审核上限 | 3 个版本 |
| 单账号滚动 24 小时提交上限 | 3 次 |

注意：AstrBot WebUI 的「从文件安装」仍是用户自行安装插件的方式，但并不等于可以通过 ZIP 向当前官方市场发布。现行 Cloud 的发布入口以 GitHub 仓库为准。[3][4]

## 本项目的提交资料

| 项目 | 当前值 / 状态 |
| --- | --- |
| 仓库 | `https://github.com/MartinXProMax/astrbot_plugin_quick_decision` |
| 可见性 | GitHub API 确认为 `PUBLIC` |
| 默认分支 | `main` |
| 已推送提交 | `b82c914` |
| 匿名可读性 | 远程 `main/metadata.yaml` 返回 HTTP 200 |
| 元数据文件 | 仓库根目录 `metadata.yaml` |
| 插件标识 | `astrbot_plugin_quick_decision` |
| 展示名称 | 快速决策 |
| 作者 | 狼与葡萄酒 |
| 首次提交版本 | `1.3.1` |
| AstrBot 范围 | `>=4.29.4,<5` |
| 支持平台 | `aiocqhttp`（QQ / OneBot v11） |
| 标签 | `AI`、`群聊`、`主动回复`、`表情包` |
| 授权 | `LICENSE`，MIT |
| 说明文档 | `README.md`，已含安装、判断提供商、温度、权重及可选依赖说明 |
| 图标 | 暂无 `logo.png`；官方开发指南将其列为可选，建议 1:1、256×256，不是本次上架的已知阻断项。[5] |
| 仓库压缩包 | `git archive HEAD` 为 114,707 字节，约 0.109 MiB，低于官方 16MB 上限；包含根目录 `metadata.yaml`、`main.py`、`README.md` 和 `LICENSE`。[2] |

本次以完整插件名查询 Cloud 公开市场接口，返回 `items: []`、`total: 0`；只能确认这次搜索未检出，不能代替登录后的投稿记录或所有名称变体查询。

现行发布页会从仓库解析名称、展示名、作者、描述、版本、标签、AstrBot 版本范围和支持平台等信息。解析后的确认页不能直接编辑元数据；若信息不正确，应先修改仓库中的 `metadata.yaml`、推送后重新解析。Cloud 的发布空间与 GitHub 用户或组织关联，最终插件 ID 以解析页显示为准，不能仅凭 `author` 昵称推断。[4][6]

本插件需要在「模型提供商 → 判断」配置 System One 判断提供商；表情包管理器是可选插件，缺少时只发文字。这些安装前提已写入 README，市场描述只保留简短功能概要，应在上架后检查市场详情页是否正确展示。

## 实际提交流程

1. 注册并登录 [AstrBot Cloud](https://cloud.astrbot.app/)，打开[发布页](https://cloud.astrbot.app/publish)。用户登录后才能使用发布表单。[2][4][6]
2. 选择 GitHub 方式，按页面提示连接 GitHub 账号并安装 / 配置 GitHub App。将 `MartinXProMax/astrbot_plugin_quick_decision` 授权给该 App。连接账号和授权仓库是两个步骤；已连接账号但仓库列表为空时，检查 App 对该仓库的授权。[4][6]
3. 选择发布空间 `MartinXProMax`，再选择 `astrbot_plugin_quick_decision` 仓库。现行页面只展示已授权且具有写权限的公开仓库。[4]
4. 点击「解析」。核对展示名、版本 `1.3.1`、作者、仓库、支持平台和 AstrBot 版本范围。如果解析失败，先检查根目录 `metadata.yaml`、仓库可访问性与 GitHub App 授权。[4][6]
5. 确认解析信息后点击创建 / 发布按钮，进入版本审核。提交成功只是进入流程，不能当作已经上架；在插件详情的「版本」页查看审核状态和报告。[6][7]
6. 版本发布后，在 Cloud 市场搜索「快速决策」并打开详情，检查说明文档、版本及安装入口；再在 AstrBot WebUI 插件市场搜索安装并验证基本功能。

本次只做调研，没有创建发布 Issue、向 Cloud 提交插件、触发安全审核或代替用户授权 GitHub App。

## 发布前的验证

官方开发指南要求功能经过测试、包含错误处理、持久化数据放在 `data` 下，并要求提交前使用 Ruff 格式化代码。若借鉴其他项目的设计、功能创意或实现思路，应在 README 说明来源；使用、修改或移植他人代码或资源时，应遵守原许可证并保留所需声明。指南没有把 `logo.png`、GitHub Release 或额外 Python 依赖文件列为所有插件统一必需项；只有用到第三方 Python 包时才需要相应 `requirements.txt`。[5]

当前发布文档明确要求插件 ZIP **不得超过 16MB**，超限会被流水线拒绝；确有业务需要时可联系维护者人工处理。发布说明建议清理 `.git`、缓存、`node_modules` 和本地开发配置等不必要文件。本仓库当前已推送内容的归档体积远低于限制。[2]

本仓库已有本地单元测试及 JS 语法检查命令，见 README。它们不能代替真实 AstrBot 环境中的安装、判断提供商调用和消息发送验证；提交前建议至少验证文字回复、温度与权重、没有表情包管理器时的降级，以及存在管理器时的配图。

Cloud 现行页面支持审核中、通过、拒绝等版本状态，以及对已有版本重新提交安全检查。自动安全检查失败时先查看版本页的实际报告；平台问题可通过官方 [AstrBot Cloud 反馈仓库](https://github.com/AstrBotDevs/astrbot-cloud-issues)反馈。公开页面未给出保证的审核完成时限，本次不承诺几分钟或几小时内上架。[7][8]

## 后续版本如何更新

已确认的手动路径：修改代码与 `metadata.yaml` 版本号、推送 GitHub，再到同一发布页选择原仓库、重新解析。页面会识别已存在的插件并切换到更新模式；核对新版本后提交审核。已有插件更新页面检查语义版本格式，本项目的 `1.3.1` 符合该格式。不要只改本地文件而未推送，也不要把已有版本的「重新检查」当作已重新解析仓库新代码。[6][7]

尚未确认的部分：平台是否对每个 GitHub push 或 Release 自动创建审核任务、自动抓取的间隔、审核具体规则和固定时限。现行前端代码只能证明手动解析 / 更新及版本检查路径；旧集合仓库的定时同步工作流不能直接用于推断新 Cloud 的行为。本次未找到可据以保证这些自动行为的官方维护文档，不将“推送仓库就必然自动更新市场”写成已确认事实。

## 来源

[1] 官方插件集合仓库迁移公告：[README（固定提交 98b24ed，2026-09-14）](https://github.com/AstrBotDevs/AstrBot_Plugins_Collection/blob/98b24ed28c6d5347c003614b9bd1045289db76ce/README.md)。

[2] 当前官方发布指南：[发布插件到插件市场](https://docs.astrbot.app/dev/star/plugin-publish.html) / [AstrBot 主仓库 Markdown 源文件](https://github.com/AstrBotDevs/AstrBot/blob/master/docs/zh/dev/star/plugin-publish.md)。站点正文直接访问曾返回 HTTP 403，本次读取官方主仓库源文件。

[3] 当前平台公开配置：[GET /api/v1/market/options](https://cloud.astrbot.app/api/v1/market/options)，核对日期 2026-11-17。

[4] 现行 [Cloud 发布页面](https://cloud.astrbot.app/publish)及其前端文案。发布动作未执行。

[5] 官方开发指南：[页面](https://docs.astrbot.app/dev/star/plugin-new.html) / [官方 Markdown 源文件](https://github.com/AstrBotDevs/AstrBot/blob/master/docs/zh/dev/star/plugin-new.md)。

[6] 当前发布页加载的[官方前端脚本 publish-DwVEpXea.js](https://cloud.astrbot.app/_app/publish-DwVEpXea.js)：GitHub 连接、仓库授权与选择、解析、元数据确认、创建 / 更新及版本格式检查。脚本文件名会随平台发布变化。

[7] 当前插件详情页加载的[官方前端脚本 _name_-DZlFRXH7.js](https://cloud.astrbot.app/_app/_name_-DZlFRXH7.js)：版本列表、审核状态、重试审核与撤销等操作。本次只读取代码，未调用这些写入接口。

[8] [AstrBot Cloud 官方问题追踪仓库](https://github.com/AstrBotDevs/astrbot-cloud-issues)。其中普通用户反馈只作为问题线索，不作为平台规则或审核政策的依据。

[9] 当前官方用户文档：[AstrBot 插件](https://github.com/AstrBotDevs/AstrBot/blob/master/docs/zh/use/plugin.md)，其中「插件市场」说明「提交我的插件」指向 Cloud 发布页。

[10] 旧独立文档仓库的[发布指南（固定提交 e5848c0，2026-02-18）](https://github.com/AstrBotDevs/AstrBot-docs/blob/e5848c07627ef96f2716cd0a721d0a7220292024/zh/dev/star/plugin-publish.md)，仅用于解释过时 Issue 流程的来源。
