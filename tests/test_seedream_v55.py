import re
import unittest
from pathlib import Path

try:  # python3 -m unittest discover -s tests
    from test_resolution_deduction import PLUGIN_MODULE, _TestConfig, _install_import_stubs
except ImportError:  # python3 -m unittest tests.test_seedream_v55
    from tests.test_resolution_deduction import PLUGIN_MODULE, _TestConfig, _install_import_stubs

FigurineProPlugin = PLUGIN_MODULE.FigurineProPlugin
LayerImageBundle = PLUGIN_MODULE.LayerImageBundle


class SeedreamModelFamilyTests(unittest.TestCase):
    def test_family_detection(self):
        cases = {
            "seedream-v5.5": "5.5",
            "seedream-5-5": "5.5",
            "doubao-seedream-5-5-261028": "5.5",
            "seedream_v5_5": "5.5",
            "seedream-v5-pro": "5.0pro",
            "seedream-5.0-pro": "5.0pro",
            "doubao-seedream-5-0-pro-260628": "5.0pro",
            "seedream-4.0": "legacy",
            "seedream-4-5": "legacy",
            "seedream-5-0-lite": "legacy",
            "nano-banana": "legacy",
            "": "legacy",
        }
        for model_name, expected in cases.items():
            with self.subTest(model=model_name):
                self.assertEqual(
                    FigurineProPlugin._seedream_model_family(model_name),
                    expected,
                )

    def test_capabilities_only_for_55_and_50pro(self):
        plugin = object.__new__(FigurineProPlugin)
        caps_55 = plugin._seedream_capabilities("seedream-v5.5")
        self.assertTrue(caps_55["background"])
        self.assertTrue(caps_55["background_auto"])
        self.assertTrue(caps_55["layer_decomposition"])
        self.assertTrue(caps_55["text_recognition"])
        self.assertFalse(caps_55["fast_prompt"])
        self.assertFalse(caps_55["web_search"])
        self.assertEqual(caps_55["max_layers"], 99)

        caps_pro = plugin._seedream_capabilities("seedream-v5-pro")
        self.assertTrue(caps_pro["background"])
        self.assertFalse(caps_pro["background_auto"])
        self.assertTrue(caps_pro["fast_prompt"])
        self.assertTrue(caps_pro["layer_decomposition"])
        self.assertFalse(caps_pro["text_recognition"])
        self.assertEqual(caps_pro["max_layers"], 16)

        caps_legacy = plugin._seedream_capabilities("seedream-4.0")
        self.assertFalse(caps_legacy["background"])
        self.assertFalse(caps_legacy["layer_decomposition"])
        self.assertFalse(caps_legacy["safety_identifier"])
        self.assertFalse(caps_legacy["fast_prompt"])


class SeedreamGenerationPayloadTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()

    @staticmethod
    def make_plugin(model_parameters, **extra_conf):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({"model_parameter_list": model_parameters, **extra_conf})
        plugin.prompt_map = {}
        return plugin

    def build_payload(self, model, entry, image_bytes=None):
        entry = {"model": model, **entry}
        plugin = self.make_plugin([entry])
        parameters = plugin._get_model_parameter_map()[model]
        return plugin, plugin._build_generic_images_payload(
            model,
            "prompt",
            [image_bytes] if image_bytes else [],
            parameters=parameters,
        )

    def test_v55_sends_background_auto_and_skips_web_search(self):
        plugin, payload = self.build_payload(
            "seedream-v5.5",
            {
                "parameter_mode": "seedream",
                "seedream_web_search": True,
                "seedream_optimize_prompt_mode": "fast",
                "seedream_resolution": "2K",
            },
            image_bytes=b"small",
        )
        self.assertEqual(payload["background"], "auto")
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["optimize_prompt_options"], {"mode": "standard"})
        self.assertEqual(payload["size"], "2K")
        self.assertEqual(payload["watermark"], False)

    def test_v55_transparent_forces_png(self):
        plugin, payload = self.build_payload(
            "seedream-v5.5",
            {
                "parameter_mode": "seedream",
                "seedream_background": "transparent",
                "seedream_send_output_format": True,
                "seedream_output_format": "jpeg",
            },
            image_bytes=b"small",
        )
        self.assertEqual(payload["background"], "transparent")
        self.assertEqual(payload["output_format"], "png")

    def test_v55_safety_identifier_truncated(self):
        plugin, payload = self.build_payload(
            "seedream-v5.5",
            {
                "parameter_mode": "seedream",
                "seedream_safety_identifier": "u" * 80,
            },
            image_bytes=b"small",
        )
        self.assertEqual(len(payload["safety_identifier"]), 64)

    def test_50pro_keeps_fast_and_supports_opaque(self):
        plugin, payload = self.build_payload(
            "seedream-v5-pro",
            {
                "parameter_mode": "seedream",
                "seedream_optimize_prompt_mode": "fast",
                "seedream_background": "opaque",
            },
            image_bytes=b"small",
        )
        self.assertEqual(payload["optimize_prompt_options"], {"mode": "fast"})
        self.assertEqual(payload["background"], "opaque")

    def test_legacy_model_gets_no_new_parameters(self):
        plugin, payload = self.build_payload(
            "seedream-4.0",
            {
                "parameter_mode": "seedream",
                "seedream_web_search": True,
                "seedream_optimize_prompt_mode": "fast",
                "seedream_background": "transparent",
                "seedream_safety_identifier": "abc",
            },
            image_bytes=b"small",
        )
        self.assertNotIn("background", payload)
        self.assertNotIn("tools", payload)
        self.assertNotIn("safety_identifier", payload)
        self.assertEqual(payload["optimize_prompt_options"], {"mode": "standard"})


class SeedreamLayerPayloadTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()

    def make_layer_plugin(self, model="seedream-v5.5", **entry_extra):
        entry = {"model": model, "parameter_mode": "seedream_layer", **entry_extra}
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({"model_parameter_list": [entry]})
        plugin.prompt_map = {}
        return plugin

    def test_layer_payload_shape(self):
        plugin = self.make_layer_plugin(
            seedream_layer_resolution="1.5K",
            seedream_layer_max_images=12,
            seedream_layer_text_recognition=True,
            seedream_layer_output_format="jpeg",
            seedream_layer_watermark=True,
        )
        parameters = plugin._get_model_parameter_map()["seedream-v5.5"]
        payload = plugin._build_generic_images_payload(
            "seedream-v5.5",
            "拆图层",
            [b"small", b"small"],
            parameters=parameters,
        )
        self.assertTrue(payload["layer_decomposition"])
        self.assertEqual(payload["layer_decomposition_options"]["max_images"], 12)
        self.assertTrue(payload["layer_decomposition_options"]["text_recognition"])
        self.assertEqual(payload["size"], "1.5K")
        self.assertEqual(payload["output_format"], "jpeg")
        self.assertTrue(payload["watermark"])
        self.assertNotIn("background", payload)
        self.assertNotIn("n", payload)
        self.assertIsInstance(payload["image"], str)  # 只发 1 张

    def test_layer_defaults(self):
        plugin = self.make_layer_plugin()
        parameters = plugin._get_model_parameter_map()["seedream-v5.5"]
        payload = plugin._build_generic_images_payload(
            "seedream-v5.5",
            "拆图层",
            [b"small"],
            parameters=parameters,
        )
        self.assertEqual(payload["size"], "auto")
        self.assertEqual(payload["output_format"], "png")
        self.assertEqual(payload["layer_decomposition_options"]["max_images"], 30)
        self.assertNotIn("text_recognition", payload["layer_decomposition_options"])

    def test_layer_max_images_clamped(self):
        plugin = self.make_layer_plugin(seedream_layer_max_images=500)
        parameters = plugin._get_model_parameter_map()["seedream-v5.5"]
        payload = plugin._build_generic_images_payload(
            "seedream-v5.5", "拆", [b"small"], parameters=parameters
        )
        self.assertEqual(payload["layer_decomposition_options"]["max_images"], 100)

    def test_layer_mode_caps_reference_images(self):
        plugin = self.make_layer_plugin()
        parameters = plugin._get_model_parameter_map()["seedream-v5.5"]
        self.assertEqual(plugin._get_reference_image_limit("seedream-v5.5", parameters), 1)
        limited = plugin._limit_reference_images(
            "seedream-v5.5", [b"a", b"b", b"c"], parameters
        )
        self.assertEqual(limited, [b"a"])


class SeedreamLayerResponseTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()
        self.plugin = object.__new__(FigurineProPlugin)

    def test_parse_layer_items_sorted_and_metadata(self):
        response = {
            "model": "doubao-seedream-5-5-261028",
            "data": [
                {
                    "url": "https://x/layer1.png",
                    "z_index": 1,
                    "size": "1273x265",
                    "output_format": "png",
                    "name": "标题文字",
                    "description": "黄色衬线标题",
                    "bounding_box": {"absolute": [383, 120, 1655, 384], "normalized": [187, 59, 808, 188]},
                    "text_recognition": {"text": "Seedream", "lines": []},
                },
                {"url": "https://x/base.png", "z_index": 0, "size": "2048x2048", "output_format": "png"},
                {"url": "https://x/layer2.png", "z_index": 2, "name": "鹦鹉"},
            ],
            "usage": {"input_images": 1, "generated_images": 3},
        }
        items = self.plugin._extract_seedream_layer_items(response)
        self.assertEqual([item["z_index"] for item in items], [0, 1, 2])
        self.assertEqual(items[1]["name"], "标题文字")
        self.assertEqual(items[1]["bounding_box"]["absolute"], [383, 120, 1655, 384])
        self.assertEqual(items[1]["text_recognition"]["text"], "Seedream")
        self.assertEqual(items[0]["size"], "2048x2048")

    def test_parse_ignores_non_layer_payload(self):
        self.assertEqual(self.plugin._extract_seedream_layer_items({"data": []}), [])
        self.assertEqual(self.plugin._extract_seedream_layer_items({"choices": []}), [])
        self.assertEqual(self.plugin._extract_seedream_layer_items(None), [])

    def test_bundle_is_bytes_and_keeps_images(self):
        bundle = LayerImageBundle(b"base", [b"base", b"layer"], [{"z_index": 0}, {"z_index": 1}])
        self.assertIsInstance(bundle, bytes)
        self.assertEqual(bytes(bundle), b"base")
        self.assertEqual(len(bundle.images), 2)
        self.assertEqual(bundle.items[1]["z_index"], 1)

    def test_render_layer_metadata(self):
        text = FigurineProPlugin._render_layer_metadata(
            {
                "z_index": 1,
                "size": "1273x265",
                "name": "标题",
                "description": "黄色标题",
                "bounding_box": {"absolute": [1, 2, 3, 4]},
                "text_recognition": {"text": "A\nB"},
            },
            0,
            3,
        )
        self.assertIn("z_index=1", text)
        self.assertIn("名称=标题", text)
        self.assertIn("坐标=1,2,3,4", text)
        self.assertIn("文字=A / B", text)


class SeedreamLayerBillingTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()

    def test_flat_price_with_extra_reference_charge(self):
        entry = {
            "model": "seedream-v5.5",
            "parameter_mode": "seedream",
            "charge_amount": 0.175,
            "charge_amount_2k": 0.175,
            "reference_image_limit": 2,
            "extra_reference_image_quota": 1,
            "extra_reference_image_charge": 0.05,
        }
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({"model_parameter_list": [entry]})
        plugin.prompt_map = {}
        parameters = plugin._get_model_parameter_map()["seedream-v5.5"]

        self.assertEqual(
            plugin._get_required_invocation_cost("seedream-v5.5", image_bytes_list=[], parameters=parameters),
            175,
        )
        self.assertEqual(
            plugin._get_required_invocation_cost("seedream-v5.5", image_bytes_list=[b"a", b"b"], parameters=parameters),
            175,
        )
        self.assertEqual(
            plugin._get_required_invocation_cost("seedream-v5.5", image_bytes_list=[b"a", b"b", b"c"], parameters=parameters),
            225,
        )
        self.assertEqual(
            plugin._get_required_invocation_cost("seedream-v5.5", image_bytes_list=[b"a", b"b", b"c", b"d"], parameters=parameters),
            275,
        )


if __name__ == "__main__":
    unittest.main()


class SeedreamLayerRoutingTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()

    def test_layer_mode_forces_images_generations(self):
        entry = {"model": "seedream-v5.5", "parameter_mode": "seedream_layer"}
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({
            "model_parameter_list": [entry],
            "model": "seedream-v5.5",
            "model_list": ["seedream-v5.5"],
            "images_generations_model_list": ["seedream-v5.5"],
            "images_edits_model_list": ["seedream-v5.5"],
            "chat_completions_model_list": [],
            "gemini_model_list": [],
            "generic_api_url": "https://generic.example/v1",
        })
        plugin.prompt_map = {}
        self.assertEqual(
            plugin._get_generic_endpoint_type_for_model("seedream-v5.5", has_images=True),
            "images_generations",
        )

    def test_generation_mode_with_images_prefers_edits(self):
        entry = {"model": "seedream-v5.5", "parameter_mode": "seedream"}
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({
            "model_parameter_list": [entry],
            "model": "seedream-v5.5",
            "model_list": ["seedream-v5.5"],
            "images_generations_model_list": ["seedream-v5.5"],
            "images_edits_model_list": ["seedream-v5.5"],
            "chat_completions_model_list": [],
            "gemini_model_list": [],
            "generic_api_url": "https://generic.example/v1",
        })
        plugin.prompt_map = {}
        self.assertEqual(
            plugin._get_generic_endpoint_type_for_model("seedream-v5.5", has_images=True),
            "images_generations",
        )



class RegisterDecoratorPlacementTests(unittest.TestCase):
    """回归：@register 必须挂在插件类上，不能被中间插入的辅助类截胡。"""

    def setUp(self):
        self.source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")

    def test_register_decorates_plugin_class(self):
        match = re.search(r"@register\((?:.|\n)*?\)\s*\nclass\s+(\w+)", self.source)
        self.assertIsNotNone(match, "@register 装饰器缺失")
        self.assertEqual(match.group(1), "FigurineProPlugin")

    def test_helper_class_defined_before_register(self):
        self.assertLess(
            self.source.index("class LayerImageBundle"),
            self.source.index("@register("),
        )



class LayerOnlyBaseTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()
        self.plugin = object.__new__(FigurineProPlugin)

    def test_detect_layers(self):
        self.assertFalse(FigurineProPlugin._layer_items_have_layers([{"z_index": 0}]))
        self.assertFalse(FigurineProPlugin._layer_items_have_layers([]))
        self.assertTrue(FigurineProPlugin._layer_items_have_layers([{"z_index": 0}, {"z_index": 1}]))
        self.assertTrue(FigurineProPlugin._layer_items_have_layers([{"z_index": "3"}]))

    def test_hint_text(self):
        hint = FigurineProPlugin._layer_only_base_hint()
        self.assertIn("layer_decomposition", hint)
        self.assertIn("请求体透传", hint)



class LayerCamelCasePayloadTests(unittest.TestCase):
    """中转网关（如 apilio）返回 camelCase 字段：zindex / boundingBox / outputType。"""

    def setUp(self):
        _install_import_stubs()
        self.plugin = object.__new__(FigurineProPlugin)

    def test_parse_camel_case_payload(self):
        response = {
            "model": "seedream-v5.5/layer-decomposition",
            "data": [
                {"url": "https://x/base.png", "size": "1024x1024", "outputType": "png"},
                {
                    "url": "https://x/l1.png",
                    "zindex": 1,
                    "name": "标题",
                    "description": "黄色标题",
                    "size": "1723x609",
                    "outputType": "png",
                    "boundingBox": {"absolute": [71, 0, 943, 308], "normalized": [69, 0, 921, 301]},
                },
            ],
            "usage": {"total_tokens": 10},
        }
        items = self.plugin._extract_seedream_layer_items(response)
        self.assertEqual([item["z_index"] for item in items], [0, 1])
        self.assertEqual(items[1]["name"], "标题")
        self.assertEqual(items[1]["bounding_box"]["absolute"], [71, 0, 943, 308])
        self.assertEqual(items[1]["output_format"], "png")
        self.assertTrue(FigurineProPlugin._layer_items_have_layers(items))

    def test_missing_zindex_falls_back_to_order(self):
        response = {"data": [{"url": "https://x/a.png"}, {"url": "https://x/b.png", "name": "图层"}]}
        items = self.plugin._extract_seedream_layer_items(response)
        self.assertEqual([item["z_index"] for item in items], [0, 1])
        self.assertTrue(FigurineProPlugin._layer_items_have_layers(items))



class SeedreamExactRatioTests(unittest.TestCase):
    """精确比例模式：不吸附 8 档，按源比例反算 WxH。"""

    def setUp(self):
        _install_import_stubs()
        self.plugin = object.__new__(FigurineProPlugin)

    @staticmethod
    def params(**overrides):
        base = {
            "enable_seedream_parameters": True,
            "seedream_resolution": "2K",
            "seedream_send_detailed_resolution": True,
            "seedream_exact_aspect_ratio": True,
            "seedream_max_side_2000": False,
            "seedream_side_over_2000_auto_2k": True,
            "seedream_pixel_limit": 0,
        }
        base.update(overrides)
        return base

    def size_ratio(self, size):
        width, height = FigurineProPlugin._parse_seedream_size(size)
        return width, height, width / height

    def test_exact_size_keeps_source_ratio(self):
        width, height, ratio = self.size_ratio(
            self.plugin._compute_seedream_exact_size(1.4066, "2K", self.params())
        )
        self.assertAlmostEqual(ratio, 1.4066, places=2)
        self.assertTrue(FigurineProPlugin.SEEDREAM_MIN_PIXELS <= width * height <= FigurineProPlugin.SEEDREAM_MAX_PIXELS)

    def test_exact_size_1k_budget(self):
        width, height, ratio = self.size_ratio(
            self.plugin._compute_seedream_exact_size(0.75, "1K", self.params(seedream_resolution="1K"))
        )
        self.assertAlmostEqual(ratio, 0.75, places=2)
        self.assertTrue(900_000 <= width * height <= 1_200_000)

    def test_ratio_clamped_to_official_range(self):
        width, height, ratio = self.size_ratio(
            self.plugin._compute_seedream_exact_size(30.0, "1K", self.params())
        )
        self.assertLessEqual(ratio, 16.01)
        self.assertGreaterEqual(width * height, FigurineProPlugin.SEEDREAM_MIN_PIXELS)

    def test_pixel_limit_respected(self):
        width, height, _ = self.size_ratio(
            self.plugin._compute_seedream_exact_size(1.0, "2K", self.params(seedream_pixel_limit=1000))
        )
        self.assertLessEqual(width * height, 1_000_000 + 1000)

    def test_build_size_uses_exact_when_enabled(self):
        # 桩 PIL 把 b"small" 解析成 1600x1200（4:3）
        size = self.plugin._build_seedream_adaptive_size(
            "seedream-v5.5", [b"small"], None, self.params()
        )
        width, height, ratio = self.size_ratio(size)
        self.assertAlmostEqual(ratio, 1600 / 1200, places=2)
        self.assertNotEqual(size, "2368x1776")  # 不是 8 档表里的固定值

    def test_build_size_falls_back_to_table_when_disabled(self):
        size = self.plugin._build_seedream_adaptive_size(
            "seedream-v5.5", [b"small"], None, self.params(seedream_exact_aspect_ratio=False)
        )
        self.assertEqual(size, "2368x1776")

    def test_command_ratio_overrides_image(self):
        size = self.plugin._build_seedream_adaptive_size(
            "seedream-v5.5", [b"small"], "16:9", self.params()
        )
        _, _, ratio = self.size_ratio(size)
        self.assertAlmostEqual(ratio, 16 / 9, places=2)

    def test_no_side_upgrade_when_max_side_disabled(self):
        self.assertFalse(
            self.plugin._seedream_side_upgrade_hits_2k(
                "seedream-v5.5",
                [b"small"],
                None,
                self.params(seedream_max_side_2000=False, seedream_resolution="1.5K"),
            )
        )

    def test_ratio_source_reports_image_pixels(self):
        ratio, source = self.plugin._get_seedream_source_ratio_info([b"small"], None, fallback_to_square=False)
        self.assertAlmostEqual(ratio, 1600 / 1200, places=4)
        self.assertIn("第1张参考图 1600x1200", source)

    def test_ratio_source_reports_command(self):
        ratio, source = self.plugin._get_seedream_source_ratio_info([b"small"], "16:9", fallback_to_square=False)
        self.assertAlmostEqual(ratio, 16 / 9, places=4)
        self.assertIn("命令比例 16:9", source)



class SeedreamLayerPerImageBillingTests(unittest.TestCase):
    """图层拆分按「返回张数 × 单张价格」计费。"""

    def setUp(self):
        _install_import_stubs()

    def make_plugin(self, entry):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({"model_parameter_list": [entry]})
        plugin.prompt_map = {}
        return plugin

    def test_layer_cost_scales_with_image_count(self):
        plugin = self.make_plugin({
            "model": "seedream-v5.5-layer",
            "parameter_mode": "seedream_layer",
            "charge_amount": 0.175,
        })
        params = plugin._get_model_parameter_map()["seedream-v5.5-layer"]
        self.assertEqual(plugin._get_seedream_layer_unit_price("seedream-v5.5-layer", params), 175)
        self.assertEqual(plugin._get_seedream_layer_cost("seedream-v5.5-layer", params, 1), 175)
        self.assertEqual(plugin._get_seedream_layer_cost("seedream-v5.5-layer", params, 9), 1575)
        self.assertEqual(plugin._get_seedream_layer_cost("seedream-v5.5-layer", params, 0), 175)

    def test_required_cost_uses_single_image(self):
        plugin = self.make_plugin({
            "model": "seedream-v5.5-layer",
            "parameter_mode": "seedream_layer",
            "charge_amount": 0.175,
            "seedream_layer_max_images": 30,
        })
        params = plugin._get_model_parameter_map()["seedream-v5.5-layer"]
        self.assertEqual(plugin._get_required_invocation_cost("seedream-v5.5-layer", parameters=params), 175)
        self.assertEqual(plugin._get_violation_deduction_cost("seedream-v5.5-layer", parameters=params), 175)

    def test_generation_model_still_flat(self):
        plugin = self.make_plugin({
            "model": "seedream-v5.5",
            "parameter_mode": "seedream",
            "charge_amount": 0.26,
            "charge_amount_2k": 0.26,
            "charge_amount_4k": 0.26,
        })
        params = plugin._get_model_parameter_map()["seedream-v5.5"]
        self.assertEqual(plugin._get_required_invocation_cost("seedream-v5.5", parameters=params), 260)
        self.assertEqual(
            plugin._get_required_invocation_cost("seedream-v5.5", image_bytes_list=[b"a", b"b"], parameters=params),
            260,
        )

