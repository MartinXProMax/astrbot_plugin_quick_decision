# AstrBot 插件安装元数据核对

核对日期：2026-11-16；依据官方文档及当日 AstrBot `master` 源码。

## 官方支持的字段

- 发布指南给出 `name`、`display_name`、`desc`、`short_desc`、`version`、`author`、`repo`、`astrbot_version`、`support_platforms`、`social_link` 和 `tags`。[1]
- `desc` 支持多行 Markdown，可承载简短安装前提、配置步骤和 README 链接；`short_desc` 用于插件市场卡片的一句话介绍。[1][2]
- `repo` 是插件仓库地址。当前加载校验仅强制要求非空字符串 `name`、`desc`、`version`、`author`；更新器在未另给下载地址时使用 `plugin.repo`，因此仍应补齐仓库地址。[3]
- `astrbot_version` 使用 PEP 440 版本约束；`support_platforms` 使用平台适配器标识，本项目保留 `>=4.29.4,<5` 与 `aiocqhttp`。[2]
- `tags` 用于市场分类和搜索，`social_link` 是作者主页；两者虽出现在发布元数据中，但当前运行时加载器不会将其写入 `StarMetadata`。[1][4]

## 安装帮助与依赖

- 当前 `_load_plugin_metadata()` 没有读取 `help`、`dependencies` 或 `plugin_dependencies`，添加这些字段不能提供安装帮助或自动安装其他 AstrBot 插件。[4]
- 官方依赖机制是插件根目录的 `requirements.txt`，用于安装 Python 第三方库；加载器的 `_ensure_plugin_requirements()` 同样仅处理该文件。[2][4]
- 插件安装完成后，安装器会读取根目录 `README.md`（不存在则尝试 `readme.md`），并在安装结果中返回其内容。[4]
- 因此，安装说明应放在 `desc` 和 README；`meme_manager` 的安装与配置需要明确告知用户手动完成，不能写成 Python 包依赖。

## 本项目采用方式

1. 补充 `repo: https://github.com/MartinXProMax/astrbot_plugin_quick_decision`。
2. 在 `desc` 中说明私有仓库的 ZIP 安装方式、版本与平台要求、System One 判断提供商、可选表情包管理器及避免重复配图的设置，并链接 README。
3. 保留精简 `short_desc`，添加少量市场标签；README 同步说明私有仓库不能使用 WebUI 的远程链接安装。
4. 本项目无需额外 Python 包，不为安装另一 AstrBot 插件新增 `requirements.txt`，也不添加未被支持的安装字段。

## 官方来源

[1] [发布插件到插件市场](https://docs.astrbot.app/dev/star/plugin-publish.html)

[2] [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)

[3] [插件更新器与元数据校验](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/updater.py)

[4] [插件元数据加载、依赖安装与 README 返回逻辑](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/star_manager.py)

本次核对覆盖文档与加载器行为，未执行真实 AstrBot 安装或插件市场发布。
