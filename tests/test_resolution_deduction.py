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


class _FakeImage:
    def __init__(self, size):
        self.size = size
        self.width, self.height = size

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def _install_import_stubs():
    if "resolution_deduction_test_plugin" in sys.modules:
        # Reuse already-installed stubs from the sibling test module if present.
        return

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = type("ClientTimeout", (), {})
    aiohttp.ClientSession = type("ClientSession", (), {})
    aiohttp.TCPConnector = type("TCPConnector", (), {})
    aiohttp.FormData = type("FormData", (), {})
    sys.modules["aiohttp"] = aiohttp

    image_module = types.ModuleType("PIL.Image")

    def open_image(value):
        data = value if isinstance(value, bytes) else value.getvalue()
        dimensions = {
            b"small": (1600, 1200),      # 最长边 1600 <= 2000
            b"wide_2k": (3000, 2000),    # 边长 > 2000 → 2K 档
            b"tall_2k": (1500, 2500),    # 边长 > 2000 → 2K 档
        }
        if data not in dimensions:
            raise ValueError("invalid test image")
        return _FakeImage(dimensions[data])

    image_module.open = open_image
    pil_module = types.ModuleType("PIL")
    pil_module.Image = image_module
    sys.modules["PIL"] = pil_module
    sys.modules["PIL.Image"] = image_module

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = _NoopLogger()
    sys.modules["astrbot"] = astrbot

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
    sys.modules["astrbot.api"] = types.ModuleType("astrbot.api")
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


def _load_plugin_module():
    _install_import_stubs()
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("resolution_deduction_test_plugin", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin_module()
FigurineProPlugin = PLUGIN_MODULE.FigurineProPlugin


class ResolutionDeductionTests(unittest.TestCase):
    @staticmethod
    def make_plugin(model_parameters, **extra_conf):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = {"model_parameter_list": model_parameters, **extra_conf}
        return plugin

    # ---- _get_resolution_deduction_tier --------------------------------

    def test_no_signal_yields_no_tier(self):
        plugin = self.make_plugin([{"model": "m"}])
        self.assertIsNone(plugin._get_resolution_deduction_tier("m"))
        self.assertIsNone(plugin._get_resolution_deduction_tier("m", image_bytes_list=[b"small"]))

    def test_image_side_over_2000_triggers_2k(self):
        plugin = self.make_plugin([{"model": "m"}])
        self.assertEqual(plugin._get_resolution_deduction_tier("m", image_bytes_list=[b"wide_2k"]), "2K")

    def test_multiple_images_use_max_side(self):
        plugin = self.make_plugin([{"model": "m"}])
        # 第一张小图、第二张超限 → 按最大边长命中 2K
        self.assertEqual(
            plugin._get_resolution_deduction_tier("m", image_bytes_list=[b"small", b"wide_2k"]),
            "2K",
        )

    def test_invalid_image_is_skipped(self):
        plugin = self.make_plugin([{"model": "m"}])
        self.assertIsNone(plugin._get_resolution_deduction_tier("m", image_bytes_list=[b"invalid"]))

    def test_command_resolution_triggers_tier(self):
        plugin = self.make_plugin([{"model": "m"}])
        self.assertEqual(plugin._get_resolution_deduction_tier("m", resolution="2K"), "2K")
        self.assertEqual(plugin._get_resolution_deduction_tier("m", resolution="4K"), "4K")

    def test_configured_resolution_triggers_tier(self):
        plugin = self.make_plugin([
            {"model": "m2k", "adaptive_resolution": "2K"},
            {"model": "m4k", "adaptive_resolution": "4K"},
        ])
        self.assertEqual(plugin._get_resolution_deduction_tier("m2k"), "2K")
        self.assertEqual(plugin._get_resolution_deduction_tier("m4k"), "4K")

    def test_4k_takes_priority_over_2k(self):
        plugin = self.make_plugin([
            {"model": "m", "adaptive_resolution": "4K"},
        ])
        # 命令 x4 与边长 2K 信号同时存在 → 4K 优先
        self.assertEqual(
            plugin._get_resolution_deduction_tier(
                "m",
                resolution="2K",
                image_bytes_list=[b"wide_2k"],
            ),
            "4K",
        )

    # ---- _get_tiered_deduction_cost ------------------------------------

    def test_base_cost_when_no_tier(self):
        plugin = self.make_plugin([{"model": "m", "deduction_count": 3}])
        self.assertEqual(plugin._get_tiered_deduction_cost("m", None), 3)

    def test_2k_cost_replaces_base(self):
        plugin = self.make_plugin([
            {"model": "m", "deduction_count": 3, "deduction_count_2k": 5},
        ])
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "2K"), 5)

    def test_4k_cost_replaces_base(self):
        plugin = self.make_plugin([
            {"model": "m", "deduction_count": 3, "deduction_count_4k": 8},
        ])
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "4K"), 8)

    def test_2k_cost_falls_back_to_global(self):
        plugin = self.make_plugin([{"model": "m"}], resolution_2k_cost=7)
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "2K"), 7)

    def test_4k_cost_falls_back_to_global(self):
        plugin = self.make_plugin([{"model": "m"}], resolution_4k_cost=9)
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "4K"), 9)

    def test_global_defaults_when_absent(self):
        plugin = self.make_plugin([{"model": "m"}])
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "2K"), 2)
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "4K"), 4)

    def test_legacy_chinese_label_config(self):
        plugin = self.make_plugin({
            "m": {
                "扣除次数": 3,
                "2K扣除次数": 5,
                "4K扣除次数": 8,
            },
        })
        self.assertEqual(plugin._get_tiered_deduction_cost("m", None), 3)
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "2K"), 5)
        self.assertEqual(plugin._get_tiered_deduction_cost("m", "4K"), 8)

    # ---- 集成：_get_required_invocation_cost ---------------------------

    def test_required_cost_base_when_no_tier(self):
        plugin = self.make_plugin([{"model": "m", "deduction_count": 3}])
        self.assertEqual(plugin._get_required_invocation_cost("m"), 3)

    def test_required_cost_2k_by_side(self):
        plugin = self.make_plugin([{"model": "m", "deduction_count_2k": 5}])
        self.assertEqual(
            plugin._get_required_invocation_cost("m", image_bytes_list=[b"wide_2k"]),
            5,
        )

    def test_required_cost_4k_by_resolution(self):
        plugin = self.make_plugin([
            {"model": "m", "deduction_count": 3, "deduction_count_4k": 8},
        ])
        self.assertEqual(plugin._get_required_invocation_cost("m", resolution="4K"), 8)

    def test_required_cost_stacks_extra_quota(self):
        plugin = self.make_plugin([
            {
                "model": "m",
                "deduction_count": 1,
                "deduction_count_2k": 2,
                "reference_image_limit": 1,
                "extra_reference_image_quota": 1,
            },
        ])
        # 3 张图，边长超限 → 2K 档(2) + 阶梯额外(2) = 4
        cost = plugin._get_required_invocation_cost("m", image_bytes_list=[b"wide_2k"] * 3)
        self.assertEqual(cost, 4)

    def test_violation_cost_uses_tier(self):
        plugin = self.make_plugin([
            {"model": "m", "deduction_count": 3, "deduction_count_2k": 5},
        ])
        # 违规结算同样按档位替换：2K 档 = 5
        cost = plugin._get_violation_deduction_cost("m", image_bytes_list=[b"wide_2k"])
        self.assertEqual(cost, 5)

    def test_violation_cost_4k_by_command(self):
        plugin = self.make_plugin([
            {"model": "m", "deduction_count_4k": 8},
        ])
        cost = plugin._get_violation_deduction_cost("m", resolution="4K")
        self.assertEqual(cost, 8)


if __name__ == "__main__":
    unittest.main()
