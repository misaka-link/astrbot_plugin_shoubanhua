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
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = type("ClientTimeout", (), {})
    aiohttp.ClientSession = type("ClientSession", (), {})
    aiohttp.TCPConnector = type("TCPConnector", (), {})
    aiohttp.FormData = type("FormData", (), {})
    sys.modules["aiohttp"] = aiohttp

    image_module = types.ModuleType("PIL.Image")

    def open_image(value):
        data = value.getvalue()
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
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("grok_parameter_test_plugin", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin_module()
FigurineProPlugin = PLUGIN_MODULE.FigurineProPlugin


class GrokParameterTests(unittest.TestCase):
    @staticmethod
    def make_plugin(model_parameters):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = {"model_parameter_list": model_parameters}
        return plugin

    def test_disabled_grok_parameters_are_omitted(self):
        plugin = self.make_plugin([{"model": "grok"}])

        self.assertEqual(plugin._get_grok_request_parameters("grok", []), {})
        self.assertNotIn("resolution", plugin._get_image_request_parameters("grok", []))

    def test_grok_defaults_to_2k_and_auto_aspect_ratio(self):
        plugin = self.make_plugin([
            {"model": "grok", "enable_grok_parameters": True},
        ])

        self.assertEqual(
            plugin._get_grok_request_parameters("grok", [b"landscape"]),
            {"resolution": "2k", "aspect_ratio": "auto"},
        )

    def test_adaptive_grok_ratio_uses_first_reference_image(self):
        plugin = self.make_plugin([
            {
                "model": "grok",
                "enable_grok_parameters": True,
                "grok_resolution": "1k",
                "grok_adaptive_aspect_ratio": True,
            },
        ])

        self.assertEqual(
            plugin._get_grok_request_parameters("grok", [b"landscape"]),
            {"resolution": "1k", "aspect_ratio": "16:9"},
        )

    def test_explicit_ratio_overrides_first_reference_image(self):
        plugin = self.make_plugin([
            {
                "model": "grok",
                "enable_grok_parameters": True,
                "grok_adaptive_aspect_ratio": True,
            },
        ])

        self.assertEqual(
            plugin._get_grok_request_parameters(
                "grok",
                [b"landscape"],
                aspect_ratio="9:16",
                force_aspect_ratio=True,
            ),
            {"resolution": "2k", "aspect_ratio": "9:16"},
        )

    def test_invalid_reference_image_falls_back_to_auto(self):
        plugin = self.make_plugin([
            {
                "model": "grok",
                "enable_grok_parameters": True,
                "grok_adaptive_aspect_ratio": True,
            },
        ])

        self.assertEqual(
            plugin._get_grok_request_parameters("grok", [b"invalid"]),
            {"resolution": "2k", "aspect_ratio": "auto"},
        )

    def test_legacy_configuration_normalizes_grok_defaults(self):
        plugin = self.make_plugin({
            "grok": {
                "Grok参数设置": "开启",
                "Grok分辨率": "invalid",
            },
        })

        self.assertEqual(
            plugin._get_grok_request_parameters("grok", []),
            {"resolution": "2k", "aspect_ratio": "auto"},
        )


if __name__ == "__main__":
    unittest.main()
