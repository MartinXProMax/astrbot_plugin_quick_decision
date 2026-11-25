"""外部插件适配器目录。

每个适配器对接一个市场插件：声明它向判断配置提供的候选来源和选项动作，并自带一组默认问题。
「外部插件」页按这里的顺序展示；各自的设置保存在配置项 external_plugins.<id> 下，至少包含 enabled。
停用后视同对方插件不可用：不再读取它的候选，依赖这些候选的问题自动跳过。

新增适配器时：
1. 在 _conf_schema.json 的 external_plugins 下加 <id> 分组，至少包含 enabled 开关；
2. 候选来源加到 candidate_sources.CATALOGS，动作加到 workflow_actions.ACTIONS；
3. 默认问题加到 config_util.default_questions() 的默认问题集，再在这里登记它们的 ID；
4. 插件主类上挂一个带 status() 的适配器实例，把属性名填到 adapter_attr。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExternalPlugin:
    id: str
    name: str
    repo: str
    summary: str
    # 插件主类上的适配器属性名，适配器的 status() 需提供 available、plugin_loaded、plugin_version、message。
    adapter_attr: str
    catalogs: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    default_questions: tuple[str, ...] = ()
    # 建议在对方插件里调整的设置，避免两边重复执行同一件事。
    tips: tuple[str, ...] = ()
    # 把适配器状态整理成页面上的若干「名称：值」。
    details: Callable[[dict[str, Any]], list[dict[str, str]]] | None = None


def _meme_details(status: dict[str, Any]) -> list[dict[str, str]]:
    if not status.get("available"):
        return []
    packs = [item for item in status.get("packs") or [] if isinstance(item, dict)]
    names = []
    for pack in packs:
        name = str(pack.get("name") or pack.get("id") or "")
        if pack.get("enabled") is False:
            name += "（已停用）"
        if pack.get("present") is False:
            name += "（文件缺失）"
        names.append(name)
    return [
        {"label": "图包", "value": "、".join(names) if names else "没有安装图包"},
        {"label": "可选表情", "value": f"{status.get('candidate_count') or 0} 张带描述的表情"},
    ]


EXTERNAL_PLUGINS: dict[str, ExternalPlugin] = {
    plugin.id: plugin
    for plugin in (
        ExternalPlugin(
            id="meme_manager",
            name="表情包管理器",
            repo="anka-afk/astrbot_plugin_meme_manager",
            summary="按逐图描述挑选表情，在判断后或正文发送后配图。图库和描述在表情包管理器页面里管理，这里只读取。",
            adapter_attr="meme_adapter",
            catalogs=("meme_categories", "memes"),
            actions=("send_meme",),
            default_questions=("use_meme", "meme_category", "meme_id"),
            tips=("关闭「启用主动发送表情工具」", "把「表情发送概率」设为 0"),
            details=_meme_details,
        ),
    )
}
