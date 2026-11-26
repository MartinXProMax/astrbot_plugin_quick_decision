"""表情包管理器适配、迁移与发图降级。使用隔离临时目录，不访问网络，不发 QQ。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v11_config import _install_astrbot_stubs

_install_astrbot_stubs()


class MessageChain:
    def __init__(self) -> None:
        self.chain: list[object] = []

    def message(self, message: str):
        self.chain.append(type("Plain", (), {"text": message})())
        return self


class Image:
    def __init__(self, path: str = "") -> None:
        self.file = path

    @classmethod
    def fromFileSystem(cls, path: str) -> Image:
        return cls(path)


class Reply:
    def __init__(self, id: str = "") -> None:
        self.id = id


sys.modules["astrbot.api.event"].MessageChain = MessageChain
sys.modules["astrbot.api.message_components"].Image = Image
sys.modules["astrbot.api.message_components"].Reply = Reply

import astrbot_plugin_quick_decision.reply_generator as reply_generator

importlib.reload(reply_generator)

from astrbot_plugin_quick_decision.config_util import PluginSettings
from engine_helpers import _build_questions, _parse_answers, build_meme_chain
from astrbot_plugin_quick_decision.legacy_meme_index import LegacyMemeIndex
from astrbot_plugin_quick_decision.meme_adapter import (
    MemeManagerAdapter,
    UNAVAILABLE_HINT,
    content_sha256,
    semantic_entry_id,
)
from astrbot_plugin_quick_decision.meme_migrate import (
    migrate_legacy_library,
    rollback_migration,
)
from astrbot_plugin_quick_decision.models import DecisionResult
from astrbot_plugin_quick_decision.reply_generator import build_send_chain
from astrbot.api.provider import DecisionBooleanAnswer, DecisionChoiceAnswer

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20243e2924222c33383c"
    "3e3f322a404b44403a393e47565c4a414c554e3d3e58675a4f6166625e6567647c8271"
    "64787d767f817d6bdb00ffd9"
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_pack(
    root: Path,
    pack_id: str,
    images: list[tuple[str, bytes, str]],
    *,
    enabled: bool = True,
    name: str | None = None,
) -> None:
    pack_dir = root / "packs" / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    semantic_images = {}
    categories: dict[str, dict[str, str]] = {}
    for filename, data, caption in images:
        category = "imported"
        rel = f"memes/{category}/{filename}"
        dest = pack_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        digest = content_sha256(data)
        entry_id = semantic_entry_id(digest, category, rel)
        semantic_images[entry_id] = {
            "entry_id": entry_id,
            "content_sha256": digest,
            "relative_path": rel,
            "category": category,
            "caption": caption,
            "caption_status": "done" if caption.strip() else "pending",
        }
        categories[category] = {"description": "测试分类"}
    _write_json(
        pack_dir / "manifest.json",
        {
            "schema_version": 1,
            "id": pack_id,
            "name": name or pack_id,
            "version": "1.0.0",
            "categories": categories,
            "extensions": {"semantic": {"version": 1, "file": "semantic_metadata.json"}},
        },
    )
    _write_json(pack_dir / "memes_data.json", {key: "测试分类" for key in categories})
    _write_json(
        pack_dir / "semantic_metadata.json",
        {"schema_version": "2.0", "pack_id": pack_id, "images": semantic_images},
    )
    registry_path = root / "registry.json"
    registry = {"schema_version": 1, "installed_packs": []}
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["installed_packs"] = [
        item
        for item in registry.get("installed_packs", [])
        if item.get("id") != pack_id
    ]
    registry["installed_packs"].append(
        {
            "id": pack_id,
            "name": name or pack_id,
            "version": "1.0.0",
            "enabled": enabled,
            "installed_at": "2026-11-13T00:00:00+00:00",
        }
    )
    _write_json(registry_path, registry)


def _write_legacy(plugin_dir: Path, items: list[tuple[str, bytes, str, str]]) -> None:
    memes_dir = plugin_dir / "memes"
    memes_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for meme_id, data, filename, description in items:
        (memes_dir / filename).write_bytes(data)
        records.append(
            {
                "id": meme_id,
                "filename": filename,
                "original_name": filename,
                "mime": "image/png" if filename.endswith(".png") else "image/jpeg",
                "description": description,
                "description_source": "manual",
                "created_at": "2026-11-13T00:00:00+08:00",
                "updated_at": "2026-11-13T00:00:00+08:00",
            }
        )
    _write_json(plugin_dir / "memes.json", {"memes": records})


class FakeStar:
    version = "5.3.8"

    async def compat_send_message(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def get_pack_status(self, pack_id: str):  # noqa: ARG002
        return "ready"


class FakeContext:
    def __init__(self, star=None) -> None:
        self._star = star

    def get_registered_star(self, name: str):
        if name == "meme_manager":
            return self._star
        return None


class MemeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager_dir = self.root / "meme_manager"
        self.plugin_dir = self.root / "quick_decision"
        self.manager_dir.mkdir()
        self.plugin_dir.mkdir()
        _write_json(
            self.manager_dir / "registry.json",
            {"schema_version": 1, "installed_packs": []},
        )
        _write_json(
            self.manager_dir / "selection_rules.json",
            {"schema_version": 1, "rules": [{"id": "default", "scope": "default", "pack_id": "builtin-default"}]},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_plugin_degrades_to_text_only(self) -> None:
        adapter = MemeManagerAdapter(
            context=FakeContext(None), data_dir=self.manager_dir, require_plugin=True
        )
        status = adapter.status()
        self.assertFalse(status.available)
        self.assertIn("不会回退到旧自制图库", status.message)
        self.assertEqual(status.message, UNAVAILABLE_HINT)
        self.assertEqual(adapter.described_catalog(), [])
        decision = DecisionResult(action="reply", use_meme=True, meme_id="a" * 64)
        chain = build_send_chain("hello", adapter, decision, quote_target=False)
        self.assertFalse(any(getattr(item, 'file', None) for item in chain.chain))
        self.assertTrue(any(getattr(item, "text", None) == "hello" for item in chain.chain))
        meme_chain = build_meme_chain(adapter, decision)
        self.assertFalse(meme_chain.chain)

    def test_does_not_read_legacy_store_when_unavailable(self) -> None:
        _write_legacy(self.plugin_dir, [("legacy000000001", PNG, "old.png", "旧库描述")])
        adapter = MemeManagerAdapter(
            context=FakeContext(None), data_dir=self.manager_dir, require_plugin=True
        )
        self.assertEqual(adapter.described_catalog(), [])
        self.assertTrue(LegacyMemeIndex(self.plugin_dir).exists())

    def test_catalog_maps_description_to_exact_file(self) -> None:
        _make_pack(
            self.manager_dir,
            "pack-a",
            [("smile.png", PNG, "微笑点头"), ("cry.jpg", JPEG, "大哭"), ("blank.png", PNG, "")],
        )
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        catalog = adapter.described_catalog()
        self.assertEqual({item["description"] for item in catalog}, {"微笑点头", "大哭"})
        self.assertEqual({item["category"] for item in catalog}, {"pack-a/imported"})
        by_desc = {item["description"]: item["id"] for item in catalog}
        smile = adapter.resolve_image(by_desc["微笑点头"])
        cry = adapter.resolve_image(by_desc["大哭"])
        self.assertIsNotNone(smile)
        self.assertIsNotNone(cry)
        self.assertEqual(smile.read_bytes(), PNG)
        self.assertEqual(cry.read_bytes(), JPEG)
        self.assertNotEqual(smile, cry)

    def test_catalog_cache_reloads_only_after_metadata_changes(self) -> None:
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头")])
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        from astrbot_plugin_quick_decision import meme_adapter as module

        with patch.object(module, "_semantic_captions", wraps=module._semantic_captions) as load:
            adapter.described_catalog()
            adapter.described_catalog()
            self.assertEqual(load.call_count, 1)

    def test_content_derived_id_follows_replaced_image(self) -> None:
        # 元数据没有 entry_id 时，ID 由图片内容算出；原地替换图片后缓存必须失效。
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头")])
        semantic_path = self.manager_dir / "packs/pack-a/semantic_metadata.json"
        data = json.loads(semantic_path.read_text(encoding="utf-8"))
        for rec in data["images"].values():
            rec.pop("entry_id")
        semantic_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        rel = "memes/imported/smile.png"
        self.assertEqual(adapter.list_candidates()[0].id, semantic_entry_id(content_sha256(PNG), "imported", rel))
        (self.manager_dir / "packs/pack-a" / rel).write_bytes(JPEG)
        self.assertEqual(adapter.list_candidates()[0].id, semantic_entry_id(content_sha256(JPEG), "imported", rel))

    def test_existing_content_hashes_read_current_images(self) -> None:
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头"), ("blank.png", JPEG, "")])
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        self.assertEqual(set(adapter.existing_content_hashes()), {content_sha256(PNG), content_sha256(JPEG)})
        (self.manager_dir / "packs/pack-a/memes/imported/smile.png").write_bytes(JPEG + b"\0")
        self.assertEqual(set(adapter.existing_content_hashes()),
                         {content_sha256(JPEG + b"\0"), content_sha256(JPEG)})

    def test_catalog_does_not_read_images_when_metadata_has_ids(self) -> None:
        # 图库可能有几百兆；选图和发图只靠元数据，不能每次缓存失效都整库读一遍。
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头"), ("cry.jpg", JPEG, "大哭")])
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        from astrbot_plugin_quick_decision import meme_adapter as module

        with patch.object(module, "content_sha256", wraps=module.content_sha256) as digest:
            catalog = adapter.described_catalog()
            self.assertEqual(len(catalog), 2)
            self.assertIsNotNone(adapter.resolve_image(catalog[0]["id"]))
            self.assertEqual(adapter.status().candidate_count, 2)
            self.assertEqual(digest.call_count, 0)
            adapter.existing_content_hashes()
            self.assertEqual(digest.call_count, 2)

    def test_unreadable_images_are_not_offered(self) -> None:
        # 不读内容时也要排除读不了的图片，否则选中后发送失败。
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头"), ("cry.jpg", JPEG, "大哭")])
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        from astrbot_plugin_quick_decision import meme_adapter as module

        real_access = module.os.access
        with patch.object(module.os, "access", side_effect=lambda p, mode: Path(p).name != "cry.jpg" and real_access(p, mode)):
            self.assertEqual([item["description"] for item in adapter.described_catalog()], ["微笑点头"])

    def test_missing_file_and_updated_description_sync(self) -> None:
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头")])
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        first = adapter.described_catalog()
        meme_id = first[0]["id"]
        semantic_path = self.manager_dir / "packs/pack-a/semantic_metadata.json"
        data = json.loads(semantic_path.read_text(encoding="utf-8"))
        rec = data["images"][meme_id]
        rec["caption"] = "更新后的描述"
        semantic_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(adapter.described_catalog()[0]["description"], "更新后的描述")
        self.assertEqual(adapter.described_catalog()[0]["id"], meme_id)
        (self.manager_dir / "packs/pack-a" / rec["relative_path"]).unlink()
        self.assertEqual(adapter.described_catalog(), [])
        self.assertIsNone(adapter.resolve_image(meme_id))

    def test_reload_does_not_keep_stale_star_or_paths(self) -> None:
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头")])
        class Meta:
            name = "meme_manager"
            version = "5.3.8"
            activated = True
            star_cls = FakeStar()

        ctx = FakeContext(Meta())
        adapter = MemeManagerAdapter(
            context=ctx, data_dir=self.manager_dir, require_plugin=True
        )
        self.assertEqual(adapter.status().plugin_version, "5.3.8")
        self.assertIn("compat_send_message", adapter.status().registered_apis)
        self.assertIn("get_pack_status", adapter.status().registered_apis)
        self.assertEqual(len(adapter.described_catalog()), 1)
        ctx._star = None
        self.assertFalse(adapter.status().available)
        self.assertEqual(adapter.described_catalog(), [])

    def test_action_none_does_not_append_image(self) -> None:
        _make_pack(self.manager_dir, "pack-a", [("smile.png", PNG, "微笑点头")])
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        meme_id = adapter.described_catalog()[0]["id"]
        decision = DecisionResult(action="none", use_meme=False, meme_id=None)
        chain = build_send_chain("should-not-matter", adapter, decision, quote_target=False)
        self.assertFalse(any(getattr(item, 'file', None) for item in chain.chain))
        self.assertFalse(build_meme_chain(adapter, decision).chain)
        selected = DecisionResult(use_meme=True, meme_id=meme_id, action="reply")
        self.assertTrue(build_meme_chain(adapter, selected).chain)

    def test_reply_attaches_exactly_one_image(self) -> None:
        _make_pack(
            self.manager_dir,
            "pack-a",
            [("smile.png", PNG, "微笑点头"), ("cry.jpg", JPEG, "大哭")],
        )
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        catalog = adapter.described_catalog()
        smile_id = next(item["id"] for item in catalog if item["description"] == "微笑点头")
        decision = DecisionResult(
            action="reply",
            use_meme=True,
            meme_id=smile_id,
            target_message_id="123",
        )
        chain = build_send_chain("hello", adapter, decision, quote_target=False)
        self.assertFalse(any(getattr(item, 'file', None) for item in chain.chain))
        self.assertTrue(any(getattr(item, "text", None) == "hello" for item in chain.chain))
        self.assertFalse(any(getattr(item, 'id', None) and getattr(item, 'text', None) is None for item in chain.chain))
        meme_chain = build_meme_chain(adapter, decision)
        images = [item for item in meme_chain.chain if getattr(item, 'file', None)]
        self.assertEqual(len(images), 1)
        self.assertEqual(Path(images[0].file).read_bytes(), PNG)
        self.assertFalse(any(getattr(item, "text", None) for item in meme_chain.chain))
        self.assertFalse(any(getattr(item, 'id', None) and getattr(item, 'text', None) is None for item in meme_chain.chain))

    def test_settings_keep_threshold_binary_cooldown_and_direct_send(self) -> None:
        settings = PluginSettings(
            {
                "choice_threshold": 0.7,
                "cooldown_seconds": 300,
                "decision_custom_context": "机器人叫小狼",
                "group": {"direct_send": True, "enable_interrupt": True},
            }
        )
        self.assertEqual(settings.choice_threshold, 0.7)
        self.assertEqual(settings.cooldown_seconds, 300)
        self.assertEqual(settings.decision_custom_context, "机器人叫小狼")
        self.assertEqual(settings.target_quote_mode, "first")
        questions = _build_questions(
            "group_passive",
            [],
            [{"id": "a" * 64, "description": "微笑"}],
            settings.decision_questions,
        )
        self.assertEqual(set(questions["action"].criteria), {"none", "reply"})
        self.assertIn("use_meme", questions)
        self.assertIn("meme_id", questions)

        memes = [{"id": "a" * 64, "description": "微笑"}]
        result = _parse_answers(
            {
                "action": DecisionChoiceAnswer(
                    choice="reply",
                    probability=0.8,
                    probabilities={"none": 0.2, "reply": 0.8},
                    confidence=0.8,
                ),
                "use_meme": DecisionBooleanAnswer(probability=0.9),
                "meme_category": DecisionChoiceAnswer(choice="未分类", probability=1, probabilities={"未分类": 1}, confidence=1),
                "meme_id": DecisionChoiceAnswer(
                    choice="a" * 64,
                    probability=0.95,
                    probabilities={"none": 0.05, "a" * 64: 0.95},
                    confidence=0.95,
                ),
            },
            scene="group_passive",
            settings=settings,
            memes=memes,
        )
        self.assertEqual(result.action, "reply")
        self.assertTrue(result.use_meme)
        self.assertEqual(result.meme_id, "a" * 64)
        below = _parse_answers(
            {
                "action": DecisionChoiceAnswer(
                    choice="reply",
                    probability=0.6,
                    probabilities={"none": 0.4, "reply": 0.6},
                    confidence=0.6,
                ),
                "use_meme": DecisionBooleanAnswer(probability=0.9),
                "meme_category": DecisionChoiceAnswer(choice="未分类", probability=1, probabilities={"未分类": 1}, confidence=1),
                "meme_id": DecisionChoiceAnswer(
                    choice="a" * 64,
                    probability=0.95,
                    probabilities={"none": 0.05, "a" * 64: 0.95},
                    confidence=0.95,
                ),
            },
            scene="group_passive",
            settings=settings,
            memes=memes,
        )
        self.assertEqual(below.action, "none")
        self.assertFalse(below.should_speak())

    def test_migrate_preserves_descriptions_dedups_and_can_rollback(self) -> None:
        _make_pack(self.manager_dir, "builtin-default", [])
        existing_hash_pack = [
            ("exists.png", PNG, ""),
        ]
        _make_pack(self.manager_dir, "already-there", existing_hash_pack)
        unique = JPEG
        _write_legacy(
            self.plugin_dir,
            [
                ("legacy000000001", PNG, "d32974bb59d74c89.png", "旧图一微笑"),
                ("legacy000000002", unique, "second.jpg", "旧图二大哭"),
            ],
        )
        before_rules = (self.manager_dir / "selection_rules.json").read_text(encoding="utf-8")
        existing_ids = {"builtin-default", "already-there"}
        result = migrate_legacy_library(self.plugin_dir, self.manager_dir)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(result.get("filled_existing"), 1)
        pack_id = result["pack_id"]
        self.assertIsNotNone(pack_id)
        self.assertNotIn(pack_id, existing_ids)
        # 旧库仍在
        self.assertTrue((self.plugin_dir / "memes/second.jpg").is_file())
        self.assertTrue((self.plugin_dir / "memes.json").is_file())
        # 未改选包规则、未覆盖已有图包
        self.assertEqual(
            (self.manager_dir / "selection_rules.json").read_text(encoding="utf-8"),
            before_rules,
        )
        self.assertTrue((self.manager_dir / "packs/already-there").is_dir())
        self.assertTrue((self.manager_dir / "packs/builtin-default").is_dir())
        adapter = MemeManagerAdapter(data_dir=self.manager_dir, require_plugin=False)
        catalog = {item["description"]: item for item in adapter.described_catalog()}
        self.assertIn("旧图二大哭", catalog)
        self.assertIn("旧图一微笑", catalog)
        path = adapter.resolve_image(catalog["旧图二大哭"]["id"])
        self.assertEqual(path.read_bytes(), JPEG)
        # 重复图只补描述，不复制文件到新包
        new_files = list((self.manager_dir / "packs" / pack_id / "memes").rglob("*"))
        new_images = [p for p in new_files if p.is_file()]
        self.assertEqual(len(new_images), 1)
        # 回滚只移除新图包
        rolled = rollback_migration(self.plugin_dir, self.manager_dir)
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertFalse((self.manager_dir / "packs" / pack_id).exists())
        self.assertTrue((self.manager_dir / "packs/already-there").is_dir())
        self.assertTrue((self.plugin_dir / "memes/second.jpg").is_file())
        # 回滚后已填到已有图包的描述仍可能保留；旧库描述始终还在
        leftover = LegacyMemeIndex(self.plugin_dir).list_records()
        self.assertEqual({rec.item.description for rec in leftover}, {"旧图一微笑", "旧图二大哭"})


if __name__ == "__main__":
    unittest.main()
