"""预设比例（prompt_list[].aspect_ratio）的归一化、落库与生效优先级测试。"""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


class _NoopLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _TestConfig(dict):
    def save(self):
        pass


def _install_import_stubs():
    if "preset_aspect_ratio_test_plugin" in sys.modules:
        return

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = type("ClientTimeout", (), {})
    aiohttp.ClientSession = type("ClientSession", (), {})
    aiohttp.TCPConnector = type("TCPConnector", (), {})
    aiohttp.FormData = type("FormData", (), {})
    sys.modules.setdefault("aiohttp", aiohttp)

    if "PIL" not in sys.modules:
        image_module = types.ModuleType("PIL.Image")
        image_module.open = lambda value: (_ for _ in ()).throw(ValueError("unused"))
        pil_module = types.ModuleType("PIL")
        pil_module.Image = image_module
        sys.modules["PIL"] = pil_module
        sys.modules["PIL.Image"] = image_module

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = _NoopLogger()
    sys.modules.setdefault("astrbot", astrbot)

    class _Filter:
        EventMessageType = types.SimpleNamespace(ALL="all")

        @staticmethod
        def command(*args, **kwargs):
            return lambda function: function

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda function: function

    event_module = types.ModuleType("astrbot.api.event")
    event_module.filter = _Filter
    sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    sys.modules["astrbot.api.event"] = event_module

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = object
    star_module.Star = type("Star", (), {})
    star_module.StarTools = object
    star_module.register = lambda *args, **kwargs: lambda cls: cls
    sys.modules["astrbot.api.star"] = star_module

    core_module = types.ModuleType("astrbot.core")
    core_module.AstrBotConfig = dict
    sys.modules["astrbot.core"] = core_module

    components_module = types.ModuleType("astrbot.core.message.components")
    for name in ("At", "Image", "Reply", "Plain", "Node", "Nodes"):
        setattr(components_module, name, type(name, (), {}))
    sys.modules["astrbot.core.message"] = types.ModuleType("astrbot.core.message")
    sys.modules["astrbot.core.message.components"] = components_module

    platform_module = types.ModuleType("astrbot.core.platform.astr_message_event")
    platform_module.AstrMessageEvent = object
    sys.modules["astrbot.core.platform"] = types.ModuleType("astrbot.core.platform")
    sys.modules["astrbot.core.platform.astr_message_event"] = platform_module

    web_module = types.ModuleType("astrbot.api.web")
    web_module.request = types.SimpleNamespace(username="", query={})
    web_module.json_response = lambda payload: payload
    web_module.error_response = lambda message, *, status_code=403, data=None: {
        "ok": False,
        "error": message,
        "message": message,
        "status": status_code,
        "data": data,
    }
    sys.modules["astrbot.api.web"] = web_module


def _load_plugin_module():
    _install_import_stubs()
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "preset_aspect_ratio_test_plugin"
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(f"{package_name}.main", plugin_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin_module()
FigurineProPlugin = PLUGIN_MODULE.FigurineProPlugin


class _FakeWorkflow:
    def __init__(self, images=None, avatar=None):
        self._images = list(images or [])
        self._avatar = avatar
        self.max_videos = 2

    async def get_images(self, event):
        return list(self._images)

    async def get_assets_ordered(self, event, max_images=10):
        return [
            {"kind": "image", "bytes": b, "source": "current", "filename": ""}
            for b in self._images
        ]

    async def _get_avatar(self, user_id):
        return self._avatar


class _FakeEvent:
    def __init__(self, message_str=""):
        self.message_str = message_str
        self.is_at_or_wake_command = True
        self.stopped = False

    def get_sender_id(self):
        return "sender"

    def get_group_id(self):
        return None

    def plain_result(self, text):
        return [text]

    def stop_event(self):
        self.stopped = True


class PresetAspectRatioTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()

    # ---- 构造工具 ----------------------------------------------------

    @staticmethod
    def make_plugin(prompt_list=None, **extra_conf):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({
            "model": "m1",
            "model_list": ["m1"],
            "generic_api_url": "https://generic.example/v1",
            "gemini_model_list": [],
            "chat_completions_model_list": ["m1"],
            "images_generations_model_list": [],
            "images_edits_model_list": [],
            "extra_prefix": [{"__template_key": "prefix", "prefix": "bnn"}],
            "prompt_list": list(prompt_list or []),
            "command_model_list": [],
            "model_mapping_list": [],
            "model_prompt_template_list": [],
            "prefix": False,
            **extra_conf,
        })
        plugin.prompt_map = {}
        plugin.preset_ratio_map = {}
        plugin._dashboard_config_lock = asyncio.Lock()
        return plugin

    def make_flow_plugin(self, prompt_list=None, images=None, avatar=None):
        """带最小桩的插件，可驱动 on_figurine_request 走到扣费计算点。"""
        plugin = self.make_plugin(prompt_list=prompt_list)
        plugin.iwf = _FakeWorkflow(images=images, avatar=avatar)
        captured = []
        plugin._get_all_models = lambda: []
        plugin._get_command_model = lambda cmd: None
        plugin._get_model_failover_candidates = lambda model, rotate=False: ["m1"]
        plugin._get_request_context = lambda a, b, has_images: {
            "actual_model": b,
            "parameters": {},
        }
        plugin._limit_reference_images = lambda model, imgs, params: list(imgs)

        def cost(model, resolution=None, **kwargs):
            captured.append(kwargs.get("aspect_ratio"))
            return 0

        plugin._get_required_invocation_cost = cost
        plugin._resolve_generation_access = lambda *args, **kwargs: (False, "", "")
        plugin._get_maintenance_message = lambda: None
        asyncio.run(plugin._load_prompt_map())
        return plugin, captured

    @staticmethod
    def collect(plugin, event):
        async def run():
            return [item async for item in plugin.on_figurine_request(event)]

        return asyncio.run(run())

    # ---- 1. 归一化 ---------------------------------------------------

    def test_normalize_preset_aspect_ratio(self):
        normalize = FigurineProPlugin._normalize_preset_aspect_ratio
        self.assertEqual(normalize("1：1"), "1:1")
        self.assertEqual(normalize(" 1:1 "), "1:1")
        self.assertEqual(normalize("16：9"), "16:9")
        self.assertEqual(normalize("4:3"), "4:3")
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize("abc"), "")
        self.assertEqual(normalize("1-1"), "")
        self.assertEqual(normalize("1:0"), "")
        self.assertEqual(normalize("0:1"), "")

    # ---- 2. 保存校验 -------------------------------------------------

    def test_dashboard_validate_presets_empty_ratio_omits_key(self):
        plugin = self.make_plugin()
        for raw in ({}, {"aspect_ratio": ""}, {"aspect_ratio": "   "}, {"aspect_ratio": None}):
            with self.subTest(raw=raw):
                presets = plugin._dashboard_validate_presets([
                    {"command": "自定义", "prompt": "p", **raw},
                ], prefixes=["bnn"])
                self.assertNotIn("aspect_ratio", presets[0])

    def test_dashboard_validate_presets_rejects_invalid_ratio(self):
        plugin = self.make_plugin()
        for bad in ("abc", "1-1", "1:0", "0:0", "1:1:1"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "预设比例格式无效，示例: 1:1"):
                    plugin._dashboard_validate_presets([
                        {"command": "自定义", "prompt": "p", "aspect_ratio": bad},
                    ], prefixes=["bnn"])

    def test_dashboard_validate_presets_stores_normalized_ratio(self):
        plugin = self.make_plugin()
        presets = plugin._dashboard_validate_presets([
            {"command": "自定义", "prompt": "p", "aspect_ratio": " 16：9 "},
        ], prefixes=["bnn"])
        self.assertEqual(presets[0]["aspect_ratio"], "16:9")

    # ---- 3. 仪表盘 GET -----------------------------------------------

    def test_dashboard_preset_items_always_include_ratio_key(self):
        plugin = self.make_plugin(prompt_list=[
            {"__template_key": "preset", "command": "无比例", "prompt": "p1"},
            {"__template_key": "preset", "command": "有比例", "prompt": "p2", "aspect_ratio": "1：1"},
            {"__template_key": "preset", "command": "非法比例", "prompt": "p3", "aspect_ratio": "bad"},
        ])
        items = plugin._dashboard_preset_items()
        by_cmd = {item["command"]: item for item in items}

        self.assertEqual(set(by_cmd), {"无比例", "有比例", "非法比例"})
        for item in items:
            self.assertIn("aspect_ratio", item)
        self.assertEqual(by_cmd["无比例"]["aspect_ratio"], "")
        self.assertEqual(by_cmd["有比例"]["aspect_ratio"], "1:1")
        self.assertEqual(by_cmd["非法比例"]["aspect_ratio"], "")

    # ---- 4. 落库归一化 -----------------------------------------------

    def test_normalized_preset_items_drop_invalid_or_empty_ratio(self):
        plugin = self.make_plugin()
        presets = plugin._normalized_preset_items([
            {"command": "c1", "prompt": "p1", "aspect_ratio": "1：1"},
            {"command": "c2", "prompt": "p2", "aspect_ratio": ""},
            {"command": "c3", "prompt": "p3", "aspect_ratio": "bad"},
            {"command": "c4", "prompt": "p4"},
        ])
        by_cmd = {preset["command"]: preset for preset in presets}

        self.assertEqual(by_cmd["c1"]["aspect_ratio"], "1:1")
        self.assertNotIn("aspect_ratio", by_cmd["c2"])
        self.assertNotIn("aspect_ratio", by_cmd["c3"])
        self.assertNotIn("aspect_ratio", by_cmd["c4"])

    # ---- 5. schema 默认条目 ------------------------------------------

    def test_schema_default_preset_items_carry_normalized_ratio(self):
        original = FigurineProPlugin.__dict__["_dashboard_schema"]

        def fake_schema(cls):
            return {
                "prompt_list": {
                    "default": [
                        {"command": "c1", "prompt": "p1", "aspect_ratio": "1：1"},
                        {"command": "c2", "prompt": "p2", "aspect_ratio": "bad"},
                        {"command": "c3", "prompt": "p3"},
                    ]
                }
            }

        FigurineProPlugin._dashboard_schema = classmethod(fake_schema)
        try:
            items = FigurineProPlugin._schema_default_preset_items()
        finally:
            FigurineProPlugin._dashboard_schema = original

        by_cmd = {item["command"]: item for item in items}
        self.assertEqual(by_cmd["c1"]["aspect_ratio"], "1:1")
        self.assertNotIn("aspect_ratio", by_cmd["c2"])
        self.assertNotIn("aspect_ratio", by_cmd["c3"])

    # ---- 6. 运行时映射与 bnn 三元组 ----------------------------------

    def test_load_prompt_map_builds_ratio_map_and_resolve_triple(self):
        plugin = self.make_plugin(prompt_list=[
            {"__template_key": "preset", "command": "自定义", "prompt": "p1", "aspect_ratio": "16：9"},
            {"__template_key": "preset", "command": "无比例", "prompt": "p2"},
        ])
        asyncio.run(plugin._load_prompt_map())

        self.assertEqual(plugin.preset_ratio_map, {"自定义": "16:9"})
        self.assertEqual(plugin._get_custom_preset_ratio("自定义"), "16:9")
        self.assertIsNone(plugin._get_custom_preset_ratio("无比例"))

        self.assertEqual(plugin._resolve_bnn_prompt("自定义", True, "%"), ("p1", "自定义", "16:9"))
        self.assertEqual(plugin._resolve_bnn_prompt("无比例", True, "%"), ("p2", "无比例", ""))
        self.assertEqual(
            plugin._resolve_bnn_prompt("自定义%补充", True, "%"),
            ("p1补充", "自定义", "16:9"),
        )
        self.assertEqual(plugin._resolve_bnn_prompt("自由描述", True, "%"), ("自由描述", "", ""))

    def test_get_custom_preset_ratio_ignores_builtin_preset_commands(self):
        default_preset = next(
            preset for preset in FigurineProPlugin._schema_default_preset_items()
            if preset.get("legacy_alias")
        )
        plugin = self.make_plugin(prompt_list=[default_preset])
        builtin = default_preset["command"]
        self.assertIn(builtin, plugin._get_default_preset_commands())
        plugin.preset_ratio_map = {builtin: "1:1"}
        self.assertIsNone(plugin._get_custom_preset_ratio(builtin))

    # ---- 7. 生效优先级（端到端驱动 on_figurine_request） --------------

    def test_preset_ratio_used_as_fallback_for_direct_preset_hit(self):
        plugin, captured = self.make_flow_plugin(
            prompt_list=[{"__template_key": "preset", "command": "自定义", "prompt": "p", "aspect_ratio": "16：9"}],
            images=[b"img"],
        )
        self.collect(plugin, _FakeEvent(message_str="自定义"))
        self.assertEqual(captured, ["16:9"])

    def test_command_ratio_wins_over_preset_ratio_for_direct_hit(self):
        plugin, captured = self.make_flow_plugin(
            prompt_list=[{"__template_key": "preset", "command": "自定义", "prompt": "p", "aspect_ratio": "16：9"}],
            images=[b"img"],
        )
        self.collect(plugin, _FakeEvent(message_str="自定义=1:1"))
        self.assertEqual(captured, ["1:1"])

    def test_preset_ratio_used_as_fallback_for_bnn_preset(self):
        plugin, captured = self.make_flow_plugin(
            prompt_list=[{"__template_key": "preset", "command": "自定义", "prompt": "p", "aspect_ratio": "16:9"}],
            images=[],
        )
        self.collect(plugin, _FakeEvent(message_str="bnn 自定义"))
        self.assertEqual(captured, ["16:9"])

    def test_command_ratio_wins_over_preset_ratio_for_bnn(self):
        plugin, captured = self.make_flow_plugin(
            prompt_list=[{"__template_key": "preset", "command": "自定义", "prompt": "p", "aspect_ratio": "16:9"}],
            images=[],
        )
        self.collect(plugin, _FakeEvent(message_str="bnn=1:1 自定义"))
        self.assertEqual(captured, ["1:1"])

    def test_no_ratio_anywhere_keeps_request_unset(self):
        plugin, captured = self.make_flow_plugin(
            prompt_list=[{"__template_key": "preset", "command": "自定义", "prompt": "p"}],
            images=[b"img"],
        )
        self.collect(plugin, _FakeEvent(message_str="自定义"))
        self.assertEqual(captured, [None])


if __name__ == "__main__":
    unittest.main()
