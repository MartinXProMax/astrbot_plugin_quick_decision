"""插件数据目录定位。图片和元数据不得放在插件代码目录。"""

from __future__ import annotations

from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_quick_decision"
MEME_MANAGER_DATA_NAME = "meme_manager"


def plugin_data_root() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    return Path(get_astrbot_plugin_data_path())


def load_plugin_data_dir(plugin_name: str = PLUGIN_NAME, *, create: bool = True) -> Path:
    """定位插件数据目录。"""
    data_dir = plugin_data_root() / plugin_name
    if create:
        data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
