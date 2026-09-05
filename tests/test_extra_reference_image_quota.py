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
    if "grok_parameter_test_plugin" in sys.modules:
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
            b"landscape": (1920, 1080),
            b"portrait": (1080, 1920),
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
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "extra_quota_test_plugin"
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


class ExtraReferenceImageQuotaTests(unittest.TestCase):
    @staticmethod
    def make_plugin(model_parameters, max_images_count=10):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = {
            "model_parameter_list": model_parameters,
            "max_images_count": max_images_count,
        }
        return plugin

    # ---- _get_extra_reference_image_charge -------------------------------

    def test_disabled_when_limit_is_zero(self):
        plugin = self.make_plugin([
            {"model": "m", "extra_reference_image_quota": 2},
        ])
        # limit defaults to 0 → feature not triggered
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 5), 0)

    def test_disabled_when_quota_is_zero(self):
        plugin = self.make_plugin([
            {"model": "m", "reference_image_limit": 2},
        ])
        # quota defaults to 0 → feature not triggered
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 5), 0)

    def test_disabled_for_unknown_model(self):
        plugin = self.make_plugin([])
        self.assertEqual(plugin._get_extra_reference_image_charge("nope", [b"x"] * 5), 0)

    def test_disabled_when_no_images(self):
        plugin = self.make_plugin([
            {"model": "m", "reference_image_limit": 2, "extra_reference_image_quota": 2},
        ])
        self.assertEqual(plugin._get_extra_reference_image_charge("m", []), 0)
        self.assertEqual(plugin._get_extra_reference_image_charge("m", None), 0)

    def test_no_extra_when_within_soft_limit(self):
        plugin = self.make_plugin([
            {"model": "m", "reference_image_limit": 2, "extra_reference_image_quota": 2},
        ])
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 1), 0)
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 2), 0)

    def test_tier_ladder_matches_spec(self):
        """limit=2, quota=2, 每阶梯 1 元: 3/4→1000, 5/6→2000, 7→3000."""
        plugin = self.make_plugin([
            {
                "model": "m",
                "reference_image_limit": 2,
                "extra_reference_image_quota": 2,
                "extra_reference_image_charge": 1,
            },
        ])
        cases = {3: 1000, 4: 1000, 5: 2000, 6: 2000, 7: 3000}
        for count, expected in cases.items():
            with self.subTest(count=count):
                self.assertEqual(
                    plugin._get_extra_reference_image_charge("m", [b"x"] * count),
                    expected,
                )

    def test_charge_per_step_is_configurable(self):
        """extra_reference_image_charge=0.05 时每阶梯加收 50 厘。"""
        plugin = self.make_plugin([
            {
                "model": "m",
                "reference_image_limit": 2,
                "extra_reference_image_quota": 2,
                "extra_reference_image_charge": 0.05,
            },
        ])
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 5), 100)

    def test_zero_charge_per_step_is_free(self):
        """每阶梯加费显式填 0 表示阶梯免费。"""
        plugin = self.make_plugin([
            {
                "model": "m",
                "reference_image_limit": 2,
                "extra_reference_image_quota": 2,
                "extra_reference_image_charge": 0,
            },
        ])
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 5), 0)
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 7), 0)

    def test_ladder_default_is_free(self):
        """未配置加费金额时默认 0：超出阶梯不额外扣费。"""
        plugin = self.make_plugin([
            {"model": "m", "reference_image_limit": 2, "extra_reference_image_quota": 2},
        ])
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 7), 0)
        self.assertEqual(plugin._get_required_invocation_cost("m", image_bytes_list=[b"x"] * 7), 1000)

    def test_extra_is_independent_of_base_charge_amount(self):
        """阶梯加费按阶梯数计算，不随 charge_amount 放大。"""
        plugin = self.make_plugin([
            {
                "model": "m",
                "reference_image_limit": 2,
                "extra_reference_image_quota": 2,
                "charge_amount": 5,
                "extra_reference_image_charge": 1,
            },
        ])
        # 5 张 → 2 阶梯 → 额外 2×1000 厘（不随 charge_amount=5000 放大）
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 5), 2000)

    def test_global_hard_cap_clamps_sent_count(self):
        """传入超过 max_images_count 时，按硬上限计算 sent。"""
        plugin = self.make_plugin(
            [{"model": "m", "reference_image_limit": 2, "extra_reference_image_quota": 2,
              "extra_reference_image_charge": 1}],
            max_images_count=4,
        )
        # sent = min(10, 4) = 4; soft = min(2, 4) = 2; excess = 2; tiers = 1
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 10), 1000)

    def test_legacy_dict_config_with_chinese_labels(self):
        plugin = self.make_plugin({
            "m": {
                "参考图数量限制": 2,
                "超限参考图阶梯额度": 2,
                "超限参考图每阶梯加费": 1,
            },
        })
        self.assertEqual(plugin._get_extra_reference_image_charge("m", [b"x"] * 5), 2000)

    # ---- _limit_reference_images -----------------------------------------

    def test_limit_relaxed_to_global_cap_when_quota_enabled(self):
        plugin = self.make_plugin(
            [{"model": "m", "reference_image_limit": 2, "extra_reference_image_quota": 2}],
            max_images_count=4,
        )
        images = [b"x"] * 10
        # 超限计费模式：发到硬上限 4，而不是软限 2
        self.assertEqual(len(plugin._limit_reference_images("m", images)), 4)

    def test_limit_keeps_soft_truncation_when_quota_disabled(self):
        plugin = self.make_plugin([
            {"model": "m", "reference_image_limit": 2},
        ])
        images = [b"x"] * 6
        # quota=0 → 不启用超限计费，仍按软限 2 截断
        self.assertEqual(len(plugin._limit_reference_images("m", images)), 2)

    def test_limit_inherits_global_when_limit_zero(self):
        plugin = self.make_plugin(
            [{"model": "m"}],
            max_images_count=3,
        )
        images = [b"x"] * 6
        # limit=0 继承全局 max_images_count
        self.assertEqual(len(plugin._limit_reference_images("m", images)), 3)

    # ---- 集成：_get_required_invocation_cost 包含 extra ------------------

    def test_required_cost_includes_extra(self):
        plugin = self.make_plugin([
            {
                "model": "m",
                "reference_image_limit": 2,
                "extra_reference_image_quota": 2,
                "charge_amount": 1,
                "extra_reference_image_charge": 1,
            },
        ])
        # 5 张图：base=1000 + extra=2×1000 = 3000
        cost = plugin._get_required_invocation_cost("m", image_bytes_list=[b"x"] * 5)
        self.assertEqual(cost, 3000)

    def test_violation_cost_includes_extra(self):
        plugin = self.make_plugin([
            {
                "model": "m",
                "reference_image_limit": 2,
                "extra_reference_image_quota": 2,
                "charge_amount": 1,
                "extra_reference_image_charge": 1,
            },
        ])
        # 违规也收 extra：base=1000 + extra=2×1000 = 3000
        cost = plugin._get_violation_deduction_cost(
            "m",
            image_bytes_list=[b"x"] * 5,
        )
        self.assertEqual(cost, 3000)

    def test_violation_cost_without_images_has_no_extra(self):
        plugin = self.make_plugin([
            {
                "model": "m",
                "reference_image_limit": 2,
                "extra_reference_image_quota": 2,
                "charge_amount": 1,
            },
        ])
        # 文生图（无图）：base=1000 + extra=0 = 1000
        cost = plugin._get_violation_deduction_cost("m")
        self.assertEqual(cost, 1000)


if __name__ == "__main__":
    unittest.main()
