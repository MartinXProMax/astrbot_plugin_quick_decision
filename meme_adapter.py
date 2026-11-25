"""对接市场表情包管理器（meme_manager）的只读适配器。

官方联动 API（compat_send_message / compat_prepare_message 等）按分类标记
或检索结果发送，并不提供「列出每张图的描述、再按精确 id 取图」的接口。
因此本适配器读取已加载实例（若可用）以及已文档化的存储协议：

- data/plugin_data/meme_manager/registry.json
- packs/<pack_id>/manifest.json
- packs/<pack_id>/semantic_metadata.json（逐图描述）
- packs/<pack_id>/memes/<category>/<file>

判断模型只根据描述选图，适配器不会读取图片像素内容。
缺依赖时只报告不可用，调用方应改发纯文字，不得回退到旧自制图库。
按图库注册表和语义元数据的修改时间缓存候选；管理器改描述或图包后会自动失效。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .plugin_paths import MEME_MANAGER_DATA_NAME, load_plugin_data_dir

MEME_MANAGER_STAR_NAME = "meme_manager"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
ENTRY_ID_RE = re.compile(r"^[0-9a-f]{64}$")
UNAVAILABLE_HINT = (
    "未检测到可用的「表情包管理器」(meme_manager)。"
    "本轮只发送文字，不会回退到旧自制图库。"
    "请在 AstrBot 插件市场安装 anka-afk/astrbot_plugin_meme_manager，"
    "并在其页面管理图库与逐图描述。"
)
# 文档中的跨插件 API。它们不能代替精确描述选图，仅用于状态核验。
DOCUMENTED_STAR_APIS = (
    "compat_send_message",
    "compat_prepare_message",
    "compat_send_prepared_message",
    "get_pack_status",
    "install_pack",
)


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def semantic_entry_id(content_sha: str, category: str, relative_path: str) -> str:
    """按官方表情包规范计算 entry_id。"""
    payload = (
        content_sha.encode("utf-8")
        + b"\x00"
        + category.encode("utf-8")
        + b"\x00"
        + relative_path.encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取 %s 失败：%s", path, exc)
        return default
    return data if data is not None else default


def _safe_pack_dir(packs_root: Path, pack_id: str) -> Path | None:
    if not PACK_ID_RE.match(pack_id):
        return None
    pack_dir = (packs_root / pack_id).resolve()
    try:
        pack_dir.relative_to(packs_root.resolve())
    except ValueError:
        return None
    if not pack_dir.is_dir():
        return None
    return pack_dir


def _safe_image_path(pack_dir: Path, relative_path: str) -> Path | None:
    if not relative_path or "\\" in relative_path or ".." in relative_path.split("/"):
        return None
    if not relative_path.startswith("memes/"):
        return None
    path = (pack_dir / relative_path).resolve()
    try:
        path.relative_to((pack_dir / "memes").resolve())
    except ValueError:
        return None
    if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
        return None
    return path


def unwrap_star(registered: Any) -> Any:
    """兼容 get_registered_star 返回实例或带 star/instance 的包装对象。"""
    if registered is None:
        return None
    for attr in ("star_cls", "star", "instance", "plugin", "star_instance"):
        inner = getattr(registered, attr, None)
        if inner is not None and inner is not registered:
            return inner
    return registered


def inspect_star_apis(registered: Any) -> list[str]:
    instance = unwrap_star(registered)
    if instance is None:
        return []
    found: list[str] = []
    for name in DOCUMENTED_STAR_APIS:
        if callable(getattr(instance, name, None)):
            found.append(name)
    return found


def read_star_version(registered: Any) -> str | None:
    instance = unwrap_star(registered)
    if registered is None and instance is None:
        return None
    for obj in (registered, instance):
        if obj is None:
            continue
        for attr in ("version", "md", "metadata", "info"):
            value = getattr(obj, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict) and value.get("version"):
                return str(value.get("version"))
            ver = getattr(value, "version", None)
            if isinstance(ver, str) and ver.strip():
                return ver.strip()
    return None


@dataclass(frozen=True)
class MemeCandidate:
    id: str
    pack_id: str
    category: str
    relative_path: str
    description: str
    path: Path
    content_sha256: str
    caption_status: str = ""


@dataclass
class AdapterStatus:
    available: bool
    plugin_loaded: bool
    plugin_version: str | None
    data_dir: str | None
    pack_count: int
    candidate_count: int
    registered_apis: list[str] = field(default_factory=list)
    message: str = ""
    packs: list[dict[str, Any]] = field(default_factory=list)
    # 表情包管理器里与本插件重复的两项设置；未加载或读不到时为 None。
    send_tool_enabled: bool | None = None
    send_probability: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "plugin_loaded": self.plugin_loaded,
            "plugin_version": self.plugin_version,
            "data_dir_name": MEME_MANAGER_DATA_NAME if self.data_dir else None,
            "pack_count": self.pack_count,
            "candidate_count": self.candidate_count,
            "registered_apis": list(self.registered_apis),
            "message": self.message,
            "packs": list(self.packs),
            "send_tool_enabled": self.send_tool_enabled,
            "send_probability": self.send_probability,
        }


def meme_manager_settings(registered: Any) -> dict[str, Any]:
    """读取表情包管理器的「启用主动发送表情工具」和「表情发送概率」。

    两项在它的配置 generation.emotion 下；缺省值与它的 _conf_schema.json 一致（开启、100）。
    """
    instance = unwrap_star(registered)
    config = getattr(instance, "config", None) if instance is not None else None
    if not isinstance(config, Mapping):
        return {"send_tool_enabled": None, "send_probability": None}
    generation = config.get("generation")
    emotion = generation.get("emotion") if isinstance(generation, Mapping) else None
    emotion = emotion if isinstance(emotion, Mapping) else {}
    tool = emotion.get("send_tool_enabled", True)
    if isinstance(tool, str):
        tool = tool.strip().lower() not in ("false", "0", "")
    try:
        probability = int(emotion.get("probability", 100))
    except (TypeError, ValueError):
        probability = None
    return {"send_tool_enabled": bool(tool), "send_probability": probability}


def discover_meme_manager_data_dir(
    context: Any | None = None, explicit: Path | None = None
) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    registered = None
    if context is not None and hasattr(context, "get_registered_star"):
        try:
            registered = context.get_registered_star(MEME_MANAGER_STAR_NAME)
        except Exception:  # noqa: BLE001
            registered = None
    instance = unwrap_star(registered)
    for obj in (instance, registered):
        if obj is None:
            continue
        for attr in ("PLUGIN_DATA_DIR", "plugin_data_dir", "data_dir"):
            value = getattr(obj, attr, None)
            if value:
                path = Path(value)
                if path.is_dir():
                    return path
        module = getattr(obj, "__module__", "") or ""
        if module.startswith("meme_manager"):
            try:
                import meme_manager.config as mm_config  # type: ignore

                value = getattr(mm_config, "PLUGIN_DATA_DIR", None)
                if value:
                    path = Path(value)
                    if path.is_dir():
                        return path
            except Exception:  # noqa: BLE001
                pass
    try:
        path = load_plugin_data_dir(MEME_MANAGER_DATA_NAME, create=False)
        if path.is_dir():
            return path
    except Exception:  # noqa: BLE001
        return None
    return None


def _semantic_captions(pack_dir: Path) -> dict[str, dict[str, Any]]:
    data = _load_json(pack_dir / "semantic_metadata.json", {})
    if not isinstance(data, dict):
        return {}
    images = data.get("images") or {}
    if not isinstance(images, dict):
        return {}
    by_path: dict[str, dict[str, Any]] = {}
    for key, rec in images.items():
        if not isinstance(rec, dict):
            continue
        rel = str(rec.get("relative_path") or "").strip()
        if not rel:
            continue
        by_path[rel] = rec
        entry_id = str(rec.get("entry_id") or key).strip()
        if entry_id:
            by_path.setdefault(f"id:{entry_id}", rec)
    return by_path


def iter_pack_images(
    pack_dir: Path, pack_id: str, *, include_undesc: bool = False
) -> list[MemeCandidate]:
    memes_dir = pack_dir / "memes"
    if not memes_dir.is_dir():
        return []
    captions = _semantic_captions(pack_dir)
    candidates: list[MemeCandidate] = []
    for category_dir in sorted(memes_dir.iterdir()):
        if not category_dir.is_dir():
            continue
        category = category_dir.name
        for image in sorted(category_dir.iterdir()):
            if not image.is_file() or image.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            relative_path = f"memes/{category}/{image.name}"
            safe = _safe_image_path(pack_dir, relative_path)
            if safe is None:
                continue
            rec = captions.get(relative_path) or {}
            caption = str(rec.get("caption") or "").strip()
            if not caption and not include_undesc:
                continue
            description = caption or "（暂无描述）"
            try:
                file_sha = content_sha256(safe.read_bytes())
            except OSError:
                continue
            entry_id = str(rec.get("entry_id") or "").strip().lower()
            if not ENTRY_ID_RE.match(entry_id):
                entry_id = semantic_entry_id(file_sha, category, relative_path)
            candidates.append(
                MemeCandidate(
                    id=entry_id,
                    pack_id=pack_id,
                    category=f"{pack_id}/{category}",
                    relative_path=relative_path,
                    description=description,
                    path=safe,
                    content_sha256=file_sha,
                    caption_status=str(rec.get("caption_status") or ""),
                )
            )
    return candidates


def list_registry_packs(data_dir: Path) -> list[dict[str, Any]]:
    registry = _load_json(
        data_dir / "registry.json",
        {"schema_version": 1, "installed_packs": []},
    )
    if not isinstance(registry, dict):
        return []
    packs = registry.get("installed_packs") or []
    if not isinstance(packs, list):
        return []
    return [item for item in packs if isinstance(item, dict) and item.get("id")]


class MemeManagerAdapter:
    """运行时图库来源：只读 meme_manager，绝不回退旧自制库。"""

    def __init__(
        self,
        context: Any | None = None,
        data_dir: Path | None = None,
        *,
        require_plugin: bool | None = None,
    ) -> None:
        self.context = context
        self._explicit_data_dir = Path(data_dir) if data_dir is not None else None
        self.require_plugin = (
            context is not None if require_plugin is None else require_plugin
        )
        self._registry_cache_key: tuple[str, int | None] | None = None
        self._registry_cache: list[dict[str, Any]] = []
        self._candidate_cache: dict[
            bool, tuple[tuple[Any, ...], list[MemeCandidate]]
        ] = {}

    @staticmethod
    def _mtime(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def _registry(self, data_dir: Path) -> list[dict[str, Any]]:
        path = data_dir / "registry.json"
        key = (str(data_dir), self._mtime(path))
        if key != self._registry_cache_key:
            self._registry_cache_key = key
            self._registry_cache = list_registry_packs(data_dir)
        return self._registry_cache

    def _candidate_cache_key(
        self, data_dir: Path, packs: list[dict[str, Any]]
    ) -> tuple[Any, ...]:
        packs_root = data_dir / "packs"
        key: list[Any] = [str(data_dir), self._mtime(data_dir / "registry.json")]
        for item in packs:
            if not bool(item.get("enabled", True)):
                continue
            pack_id = str(item.get("id") or "")
            pack_dir = _safe_pack_dir(packs_root, pack_id)
            key.extend(
                (
                    pack_id,
                    self._mtime(pack_dir) if pack_dir else None,
                    self._mtime(pack_dir / "semantic_metadata.json") if pack_dir else None,
                    self._mtime(pack_dir / "memes") if pack_dir else None,
                )
            )
            if pack_dir:
                memes_dir = pack_dir / "memes"
                if memes_dir.is_dir():
                    for category_dir in sorted(memes_dir.iterdir()):
                        if category_dir.is_dir():
                            key.extend((str(category_dir), self._mtime(category_dir)))
                            for image in sorted(category_dir.iterdir()):
                                if image.is_file() and image.suffix.lower() in IMAGE_EXTENSIONS:
                                    # Image replacement can keep the directory mtime unchanged.
                                    key.extend((str(image), self._mtime(image)))
        return tuple(key)

    def get_registered_star(self) -> Any | None:
        if self.context is None or not hasattr(self.context, "get_registered_star"):
            return None
        try:
            registered = self.context.get_registered_star(MEME_MANAGER_STAR_NAME)
        except Exception:  # noqa: BLE001
            logger.warning("查询 meme_manager 注册实例失败", exc_info=True)
            return None
        if registered is not None and getattr(registered, "activated", True) is False:
            return None
        return registered

    def resolve_data_dir(self) -> Path | None:
        return discover_meme_manager_data_dir(self.context, self._explicit_data_dir)

    def status(self) -> AdapterStatus:
        registered = self.get_registered_star()
        plugin_loaded = registered is not None
        version = read_star_version(registered)
        apis = inspect_star_apis(registered)
        settings = meme_manager_settings(registered)
        data_dir = self.resolve_data_dir()
        if self.require_plugin and not plugin_loaded:
            return AdapterStatus(
                available=False,
                plugin_loaded=False,
                plugin_version=version,
                data_dir=str(data_dir) if data_dir else None,
                pack_count=0,
                candidate_count=0,
                registered_apis=apis,
                message=UNAVAILABLE_HINT,
                **settings,
            )
        if data_dir is None or not data_dir.is_dir():
            return AdapterStatus(
                available=False,
                plugin_loaded=plugin_loaded,
                plugin_version=version,
                data_dir=None,
                pack_count=0,
                candidate_count=0,
                registered_apis=apis,
                message=UNAVAILABLE_HINT,
                **settings,
            )
        packs = self._registry(data_dir)
        candidates = self.list_candidates()
        pack_summaries = []
        packs_root = data_dir / "packs"
        for item in packs:
            pack_id = str(item.get("id") or "")
            pack_dir = _safe_pack_dir(packs_root, pack_id)
            pack_summaries.append(
                {
                    "id": pack_id,
                    "name": item.get("name") or pack_id,
                    "enabled": bool(item.get("enabled", True)),
                    "present": pack_dir is not None,
                }
            )
        return AdapterStatus(
            available=True,
            plugin_loaded=plugin_loaded,
            plugin_version=version,
            data_dir=str(data_dir),
            pack_count=len(packs),
            candidate_count=len(candidates),
            registered_apis=apis,
            message="",
            packs=pack_summaries,
            **settings,
        )

    def list_candidates(self, *, include_undesc: bool = False) -> list[MemeCandidate]:
        if self.require_plugin and self.get_registered_star() is None:
            return []
        data_dir = self.resolve_data_dir()
        if data_dir is None:
            return []
        packs = self._registry(data_dir)
        cache_key = self._candidate_cache_key(data_dir, packs)
        cached = self._candidate_cache.get(include_undesc)
        if cached and cached[0] == cache_key:
            return cached[1]
        packs_root = data_dir / "packs"
        candidates: list[MemeCandidate] = []
        seen_ids: set[str] = set()
        for item in packs:
            if not bool(item.get("enabled", True)):
                continue
            pack_id = str(item.get("id") or "")
            pack_dir = _safe_pack_dir(packs_root, pack_id)
            if pack_dir is None:
                continue
            for candidate in iter_pack_images(
                pack_dir, pack_id, include_undesc=include_undesc
            ):
                if candidate.id in seen_ids:
                    continue
                seen_ids.add(candidate.id)
                candidates.append(candidate)
        self._candidate_cache[include_undesc] = (cache_key, candidates)
        return candidates

    def described_catalog(self) -> list[dict[str, str]]:
        return [
            {"id": item.id, "description": item.description, "category": item.category}
            for item in self.list_candidates()
        ]

    def resolve_image(self, meme_id: str) -> Path | None:
        if not meme_id or not ENTRY_ID_RE.match(meme_id):
            return None
        for item in self.list_candidates():
            if item.id == meme_id:
                return item.path if item.path.is_file() else None
        # Keep previously selected images usable if their description was removed.
        for item in self.list_candidates(include_undesc=True):
            if item.id == meme_id:
                return item.path if item.path.is_file() else None
        return None

    def existing_content_hashes(self) -> dict[str, MemeCandidate]:
        return {
            item.content_sha256: item
            for item in self.list_candidates(include_undesc=True)
        }
