import asyncio
import copy
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


class _TestConfig(dict):
    def __init__(self, *args, **kwargs):
        self.fail_save = False
        self.save_calls = 0
        super().__init__(*args, **kwargs)

    def save(self):
        self.save_calls += 1
        if self.fail_save:
            raise RuntimeError("simulated persistence failure")


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
    package_name = "resolution_deduction_test_plugin"
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


class ResolutionDeductionTests(unittest.TestCase):
    @staticmethod
    def make_plugin(model_parameters, **extra_conf):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({"model_parameter_list": model_parameters, **extra_conf})
        plugin.prompt_map = {}
        plugin._dashboard_config_lock = asyncio.Lock()
        return plugin

    def make_dashboard_plugin(self, **extra_conf):
        config = {
            "model": "m1",
            "model_list": ["m1"],
            "generic_api_url": "https://generic.example/v1",
            "gemini_model_list": [],
            "chat_completions_model_list": ["m1"],
            "images_generations_model_list": [],
            "images_edits_model_list": [],
            "extra_prefix": [{"__template_key": "prefix", "prefix": "bnn"}],
            "prompt_list": [],
            "command_model_list": [],
            "model_mapping_list": [],
            "model_prompt_template_list": [],
        }
        config.update(extra_conf)
        return self.make_plugin([], **config)

    @staticmethod
    def set_request_json(payload):
        async def read_json(default=None):
            return payload

        PLUGIN_MODULE.request.json = read_json

    # ---- _get_resolution_deduction_tier --------------------------------

    def test_failover_callback_only_records_intermediate_failures(self):
        plugin = self.make_plugin([])
        plugin._get_model_failover_candidates = lambda model: ["m1", "m2"]
        plugin._should_stop_model_failover = lambda status, result: False

        async def call_once(images, prompt, override_model=None, **kwargs):
            if override_model == "m1":
                return {"error_type": "upstream_error"}, 503
            return b"image", 200

        attempts = []

        async def record_attempt(model, result, status, succeeded):
            attempts.append((model, status, succeeded))

        plugin._call_api_once = call_once
        result, status, actual_model = asyncio.run(plugin._call_api(
            [],
            "prompt",
            override_model="source",
            return_actual_model=True,
            on_attempt=record_attempt,
        ))
        self.assertEqual((result, status, actual_model), (b"image", 200, "m2"))
        self.assertEqual(attempts, [("m1", 503, False)])

    def test_failover_callback_skips_final_failure(self):
        plugin = self.make_plugin([])
        plugin._get_model_failover_candidates = lambda model: ["m1"]
        plugin._should_stop_model_failover = lambda status, result: False

        async def call_once(images, prompt, override_model=None, **kwargs):
            return {"error_type": "upstream_error"}, 503

        attempts = []

        async def record_attempt(*args):
            attempts.append(args)

        plugin._call_api_once = call_once
        _, status, actual_model = asyncio.run(plugin._call_api(
            [],
            "prompt",
            override_model="source",
            return_actual_model=True,
            on_attempt=record_attempt,
        ))
        self.assertEqual((status, actual_model), (503, "m1"))
        self.assertEqual(attempts, [])

    def test_parameter_mode_legacy_inference_and_explicit_normalization(self):
        plugin = self.make_plugin([
            {
                "model": "legacy",
                "enable_gpt_parameters": True,
                "enable_gemini_parameters": True,
                "enable_grok_parameters": True,
                "enable_seedream_parameters": True,
            },
            {
                "model": "invalid",
                "parameter_mode": "unknown",
                "enable_gpt_parameters": True,
            },
        ])

        parameters = plugin._get_model_parameter_map()

        self.assertEqual(parameters["legacy"]["parameter_mode"], "gpt")
        self.assertTrue(parameters["legacy"]["enable_gpt_parameters"])
        self.assertFalse(parameters["legacy"]["enable_gemini_parameters"])
        self.assertFalse(parameters["legacy"]["enable_grok_parameters"])
        self.assertFalse(parameters["legacy"]["enable_seedream_parameters"])
        self.assertEqual(parameters["invalid"]["parameter_mode"], "none")
        self.assertFalse(parameters["invalid"]["enable_gpt_parameters"])

    def test_parameter_mode_recovers_legacy_enable_flag_after_none_mode(self):
        plugin = self.make_plugin([
            {
                "model": "recover-grok",
                "parameter_mode": "none",
                "enable_grok_parameters": True,
            },
            {
                "model": "none",
                "parameter_mode": "none",
                "enable_gpt_parameters": False,
                "enable_gemini_parameters": False,
                "enable_grok_parameters": False,
                "enable_seedream_parameters": False,
            },
        ])

        parameters = plugin._get_model_parameter_map()

        self.assertEqual(parameters["recover-grok"]["parameter_mode"], "grok")
        self.assertTrue(parameters["recover-grok"]["enable_grok_parameters"])
        self.assertEqual(parameters["none"]["parameter_mode"], "none")

    def test_dashboard_normalizes_one_parameter_mode_without_losing_values(self):
        plugin = self.make_dashboard_plugin()

        normalized = plugin._dashboard_normalize_model_parameters([{
            "model": "m1",
            "parameter_mode": "grok",
            "quality": "high",
            "grok_resolution": "1k",
            "seedream_watermark": True,
            "enable_gpt_parameters": True,
            "enable_gemini_parameters": True,
            "enable_grok_parameters": False,
            "enable_seedream_parameters": True,
        }], {"m1"})[0]

        self.assertEqual(normalized["parameter_mode"], "grok")
        self.assertTrue(normalized["enable_grok_parameters"])
        self.assertFalse(normalized["enable_gpt_parameters"])
        self.assertFalse(normalized["enable_gemini_parameters"])
        self.assertFalse(normalized["enable_seedream_parameters"])
        self.assertEqual(normalized["quality"], "high")
        self.assertEqual(normalized["grok_resolution"], "1k")
        self.assertTrue(normalized["seedream_watermark"])

    def test_actual_model_route_wins_over_source_route(self):
        plugin = self.make_plugin([],
            gemini_model_list=["source"],
            chat_completions_model_list=[],
            images_generations_model_list=[],
            images_edits_model_list=["target"],
        )

        context = plugin._get_request_context("source", "target", True)

        self.assertEqual(context["actual_model"], "target")
        self.assertEqual(context["route_model"], "target")
        self.assertEqual(context["api_route"], "generic")
        self.assertEqual(context["endpoint_type"], "images_edits")

    def test_source_route_is_inherited_only_when_target_has_no_route(self):
        plugin = self.make_plugin([],
            gemini_model_list=[],
            chat_completions_model_list=[],
            images_generations_model_list=["source"],
            images_edits_model_list=[],
        )

        inherited = plugin._get_request_context("source", "target", False)
        fallback = plugin._get_request_context("unrouted-source", "unrouted-target", True)

        self.assertEqual(inherited["route_model"], "source")
        self.assertEqual(inherited["api_route"], "generic")
        self.assertEqual(inherited["endpoint_type"], "images_generations")
        self.assertEqual(fallback["route_model"], "")
        self.assertEqual(fallback["api_route"], "generic")
        self.assertEqual(fallback["endpoint_type"], "chat_completions")

    def test_actual_parameter_entry_replaces_source_entry_wholesale(self):
        plugin = self.make_plugin([
            {
                "model": "source",
                "parameter_mode": "gpt",
                "deduction_count": 7,
                "reference_image_limit": 9,
                "max_output_tokens": 8192,
                "deduct_on_violation": True,
            },
            {
                "model": "target",
                "parameter_mode": "none",
                "deduct_on_violation": False,
                "reference_image_limit": 0,
                "max_output_tokens": 0,
            },
        ])

        target_parameters = plugin._get_effective_model_parameters("source", "target")
        inherited_parameters = plugin._get_effective_model_parameters("source", "unconfigured-target")

        self.assertEqual(target_parameters["parameter_mode"], "none")
        self.assertEqual(target_parameters["deduction_count"], 1)
        self.assertEqual(target_parameters["reference_image_limit"], 0)
        self.assertEqual(target_parameters["max_output_tokens"], 0)
        self.assertFalse(target_parameters["deduct_on_violation"])
        self.assertFalse(target_parameters["enable_gpt_parameters"])
        self.assertEqual(inherited_parameters["deduction_count"], 7)
        self.assertTrue(inherited_parameters["enable_gpt_parameters"])

    def test_parameter_modes_keep_vendor_payloads_exclusive(self):
        plugin = self.make_plugin([
            {
                "model": "gpt-target",
                "parameter_mode": "gpt",
                "quality": "high",
                "moderation": "low",
                "grok_resolution": "1k",
                "seedream_watermark": True,
            },
            {
                "model": "seedream-target",
                "parameter_mode": "seedream",
                "quality": "high",
                "moderation": "low",
                "seedream_watermark": True,
                "seedream_resolution": "2K",
            },
        ])
        parameters = plugin._get_model_parameter_map()

        gpt_payload = plugin._build_generic_images_payload(
            "gpt-target", "prompt", [], parameters=parameters["gpt-target"]
        )
        seedream_payload = plugin._build_generic_images_payload(
            "seedream-target", "prompt", [], parameters=parameters["seedream-target"]
        )

        self.assertEqual(gpt_payload["model"], "gpt-target")
        self.assertEqual(gpt_payload["quality"], "high")
        self.assertEqual(gpt_payload["moderation"], "low")
        self.assertNotIn("resolution", gpt_payload)
        self.assertNotIn("watermark", gpt_payload)
        self.assertEqual(seedream_payload["model"], "seedream-target")
        self.assertEqual(seedream_payload["watermark"], True)
        self.assertEqual(seedream_payload["size"], "2K")
        self.assertNotIn("n", seedream_payload)
        self.assertNotIn("quality", seedream_payload)
        self.assertNotIn("moderation", seedream_payload)

    def test_usage_endpoint_details_preserves_inherited_source_route(self):
        plugin = self.make_plugin([],
            gemini_model_list=[],
            chat_completions_model_list=[],
            images_generations_model_list=["source"],
            images_edits_model_list=[],
        )
        request_context = plugin._get_request_context("source", "target", False)

        api_route, endpoint_type = plugin._get_usage_endpoint_details(
            "target",
            False,
            source_model="source",
            request_context=request_context,
        )

        self.assertEqual((api_route, endpoint_type), ("generic", "images_generations"))

    def test_failover_uses_each_candidates_reference_image_limit(self):
        plugin = self.make_plugin([
            {"model": "first", "reference_image_limit": 1},
            {"model": "second", "reference_image_limit": 2},
        ], max_images_count=5)
        plugin._get_model_failover_candidates = lambda model: ["first", "second"]
        plugin._should_stop_model_failover = lambda status, result: False
        attempts = []

        async def call_once(images, prompt, override_model=None, request_context=None, **kwargs):
            attempts.append((override_model, len(images), request_context["actual_model"]))
            if override_model == "first":
                return {"error_type": "upstream_error"}, 503
            return b"image", 200

        plugin._call_api_once = call_once
        result, status, context = asyncio.run(plugin._call_api(
            [b"one", b"two", b"three"],
            "prompt",
            override_model="source",
            return_request_context=True,
        ))

        self.assertEqual((result, status), (b"image", 200))
        self.assertEqual(attempts, [("first", 1, "first"), ("second", 2, "second")])
        self.assertEqual(context["actual_model"], "second")
        self.assertEqual(len(context["image_bytes_list"]), 2)

    def test_dashboard_configuration_preserves_endpoint_and_failover_models(self):
        plugin = self.make_dashboard_plugin(
            model="entry",
            model_list=["entry"],
            gemini_model_list=["gemini-target"],
            chat_completions_model_list=[],
            images_generations_model_list=["generic-target"],
            model_mapping_list=[{
                "__template_key": "model_mapping",
                "model": "entry",
                "mapped_model": "generic-target",
                "priority": 10,
            }],
        )
        values = plugin._dashboard_configuration_values()
        self.assertEqual(values["model_list"], ["entry", "gemini-target", "generic-target"])
        revision = plugin._dashboard_configuration_revision(values)
        self.set_request_json({"revision": revision, "config": values})

        response = asyncio.run(plugin._web_dashboard_configuration_save())

        self.assertTrue(response["ok"])
        self.assertEqual(plugin.conf["model_list"], ["entry", "gemini-target", "generic-target"])
        self.assertEqual(plugin.conf["images_generations_model_list"], ["generic-target"])
        self.assertEqual(plugin.conf["model_mapping_list"][0]["mapped_model"], "generic-target")

    def test_dashboard_configuration_preserves_legacy_failover_shapes(self):
        plugin = self.make_dashboard_plugin(
            model="entry",
            model_list=["entry"],
            chat_completions_model_list=[],
            images_generations_model_list=["target-a", "target-b", "target-c"],
            model_mapping_list={
                "entry": ["target-a", "target-b"],
                "other": {"mapped_model": "target-c", "priority": 8},
            },
        )

        values = plugin._dashboard_configuration_values()

        self.assertEqual(values["model_mapping_list"], [
            {"model": "entry", "mapped_model": "target-a", "priority": 0},
            {"model": "entry", "mapped_model": "target-b", "priority": 0},
            {"model": "other", "mapped_model": "target-c", "priority": 8},
        ])
        revision = plugin._dashboard_configuration_revision(values)
        self.set_request_json({"revision": revision, "config": values})
        response = asyncio.run(plugin._web_dashboard_configuration_save())

        self.assertTrue(response["ok"])
        self.assertEqual(plugin.conf["model_mapping_list"], [
            {"__template_key": "model_mapping", "model": "entry", "mapped_model": "target-a", "priority": 0},
            {"__template_key": "model_mapping", "model": "entry", "mapped_model": "target-b", "priority": 0},
            {"__template_key": "model_mapping", "model": "other", "mapped_model": "target-c", "priority": 8},
        ])

    def test_dashboard_preserves_unknown_model_parameter_fields(self):
        plugin = self.make_dashboard_plugin(model_parameter_list=[{
            "__template_key": "model_parameters",
            "model": "m1",
            "parameter_mode": "gpt",
            "enable_gpt_parameters": True,
            "future_provider_option": {"keep": True},
        }])

        values = plugin._dashboard_configuration_values()
        self.assertEqual(values["model_parameter_list"][0]["future_provider_option"], {"keep": True})
        revision = plugin._dashboard_configuration_revision(values)
        self.set_request_json({"revision": revision, "config": values})

        response = asyncio.run(plugin._web_dashboard_configuration_save())

        self.assertTrue(response["ok"])
        self.assertEqual(plugin.conf["model_parameter_list"][0]["future_provider_option"], {"keep": True})

    def test_dashboard_configuration_rejects_conflicting_routes_and_invalid_binding(self):
        plugin = self.make_dashboard_plugin()
        values = plugin._dashboard_configuration_values()
        values["gemini_model_list"] = ["m1"]
        with self.assertRaisesRegex(ValueError, "不能同时"):
            plugin._validate_dashboard_configuration(values)

        values["gemini_model_list"] = []
        values["command_model_list"] = [{"command": "不存在", "model": "m1"}]
        with self.assertRaisesRegex(ValueError, "绑定指令未启用"):
            plugin._validate_dashboard_configuration(values)

        values["command_model_list"] = [{"command": "bnn", "model": "m1"}]
        values["model_parameter_list"] = [{
            "model": "m1",
            "auto_upgrade_1k_adaptive_resolution": True,
            "force_resolution_limit": True,
        }]
        validated = plugin._validate_dashboard_configuration(values)
        self.assertEqual(validated["command_model_list"][0]["command"], "bnn")
        self.assertFalse(validated["model_parameter_list"][0]["force_resolution_limit"])

    def test_gemini_uses_shared_generic_service_url_and_key_pool(self):
        plugin = self.make_dashboard_plugin(
            model="gemini-image",
            model_list=["gemini-image"],
            gemini_model_list=["gemini-image"],
            chat_completions_model_list=[],
            generic_api_keys=["shared-first", "shared-second"],
            gemini_api_keys=["legacy-only"],
        )
        plugin.key_lock = asyncio.Lock()
        plugin.generic_key_index = 0
        plugin.gemini_key_index = 0

        for url in (
            "https://gateway.example",
            "https://gateway.example/v1",
            "https://gateway.example/v1/chat/completions",
            "https://gateway.example/v1/images/generations",
            "https://gateway.example/v1/images/edits",
        ):
            self.assertEqual(
                plugin._resolve_gemini_endpoint_url(url, "gemini-image"),
                "https://gateway.example/v1beta/models/gemini-image:generateContent",
            )

        self.assertEqual(plugin._get_api_base_url(), "https://generic.example/v1")
        self.assertEqual(asyncio.run(plugin._get_pool_api_key("gemini")), "shared-first")
        self.assertEqual(asyncio.run(plugin._get_pool_api_key("gemini")), "shared-second")

        plugin.conf["generic_api_keys"] = []
        self.assertEqual(asyncio.run(plugin._get_pool_api_key("gemini")), "legacy-only")
        values = plugin._dashboard_configuration_values()
        validated = plugin._validate_dashboard_configuration(values)
        self.assertEqual(validated["model"], "gemini-image")

    def test_dashboard_rejects_or_redacts_credentialed_shared_service_url(self):
        secret_url = "https://user:secret@gateway.example/v1?token=private"
        plugin = self.make_dashboard_plugin(generic_api_url=secret_url)
        values = plugin._dashboard_configuration_values()

        self.assertEqual(values["generic_api_url"], "")
        self.assertNotIn(secret_url, str(values))
        sanitized_log_url = plugin._sanitize_request_log_url(secret_url)
        self.assertNotIn("user", sanitized_log_url)
        self.assertNotIn("secret", sanitized_log_url)
        self.assertNotIn("private", sanitized_log_url)
        sensitive = asyncio.run(plugin._web_dashboard_configuration_get())
        self.assertNotIn(secret_url, str(sensitive))
        self.assertTrue(sensitive["sensitive"]["generic_api_url"]["write_only"])

        replacement = copy.deepcopy(values)
        replacement["generic_api_url"] = "https://other.example/v1"
        with self.assertRaisesRegex(ValueError, "敏感配置"):
            plugin._validate_dashboard_configuration(replacement)

        revision = plugin._dashboard_current_revision()
        self.set_request_json({
            "revision": revision,
            "target": "generic_api_url",
            "action": "replace",
            "value": secret_url,
        })
        response = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(response["ok"])
        self.assertEqual(plugin.conf["generic_api_url"], secret_url)
        self.assertNotIn(secret_url, str(response))

        saved_values = plugin._dashboard_configuration_values()
        self.set_request_json({
            "revision": response["revision"],
            "config": saved_values,
        })
        ordinary_save = asyncio.run(plugin._web_dashboard_configuration_save())
        self.assertTrue(ordinary_save["ok"])
        self.assertEqual(plugin.conf["generic_api_url"], secret_url)

        self.set_request_json({
            "revision": ordinary_save["revision"],
            "target": "generic_api_url",
            "action": "clear",
        })
        cleared = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(cleared["ok"])
        self.assertEqual(plugin.conf["generic_api_url"], "")

    def test_dashboard_sensitive_configuration_is_write_only_and_versioned(self):
        secret_key = "shared-secret-key"
        secret_proxy = "socks5://user:token@proxy.example:1080"
        plugin = self.make_dashboard_plugin(
            generic_api_keys=[secret_key],
            proxy_url=secret_proxy,
        )
        values = plugin._dashboard_configuration_values()
        revision = plugin._dashboard_configuration_revision(values)
        public_snapshot = str(values)

        self.assertNotIn("generic_api_keys", values)
        self.assertNotIn(secret_key, public_snapshot)
        self.assertNotIn(secret_proxy, public_snapshot)
        self.assertNotIn("proxy_url", values["settings"])

        sensitive = asyncio.run(plugin._web_dashboard_sensitive_get())
        self.assertNotIn(secret_key, str(sensitive))
        self.assertNotIn(secret_proxy, str(sensitive))
        self.assertEqual(sensitive["sensitive"]["generic_api_keys"]["count"], 1)
        self.assertTrue(sensitive["sensitive"]["proxy_url"]["write_only"])

        self.set_request_json({
            "revision": revision,
            "target": "generic_api_keys",
            "action": "replace",
            "values": ["new-shared-key"],
        })
        response = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(response["ok"])
        self.assertEqual(plugin.conf["generic_api_keys"], ["new-shared-key"])
        self.assertNotIn("new-shared-key", str(response))
        self.assertNotEqual(response["revision"], revision)

        self.set_request_json({"revision": revision, "config": values})
        stale = asyncio.run(plugin._web_dashboard_configuration_save())
        self.assertEqual(stale["status"], 409)

    def test_dashboard_sensitive_key_pool_append_and_clear(self):
        plugin = self.make_dashboard_plugin(generic_api_keys=["first"])
        revision = plugin._dashboard_current_revision()
        self.set_request_json({
            "revision": revision,
            "target": "generic_api_keys",
            "action": "append",
            "values": ["first", "second"],
        })
        appended = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertEqual(plugin.conf["generic_api_keys"], ["first", "second"])
        self.assertEqual(appended["sensitive"]["generic_api_keys"]["count"], 2)

        self.set_request_json({
            "revision": appended["revision"],
            "target": "generic_api_keys",
            "action": "clear",
            "values": [],
        })
        cleared = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(cleared["ok"])
        self.assertEqual(plugin.conf["generic_api_keys"], [])

    def test_dashboard_sensitive_proxy_replace_clear_and_rollback(self):
        plugin = self.make_dashboard_plugin(proxy_url="http://proxy.example:8080")
        values = plugin._dashboard_configuration_values()
        self.assertEqual(values["settings"]["proxy_url"], "http://proxy.example:8080")
        revision = plugin._dashboard_current_revision()
        secret_proxy = "socks5://user:secret@proxy.example:1080"

        self.set_request_json({
            "revision": revision,
            "target": "proxy_url",
            "action": "replace",
            "value": secret_proxy,
        })
        replaced = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(replaced["ok"])
        self.assertEqual(plugin.conf["proxy_url"], secret_proxy)
        self.assertNotIn(secret_proxy, str(replaced))
        self.assertTrue(replaced["sensitive"]["proxy_url"]["write_only"])

        self.set_request_json({
            "revision": replaced["revision"],
            "target": "proxy_url",
            "action": "clear",
        })
        cleared = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(cleared["ok"])
        self.assertEqual(plugin.conf["proxy_url"], "")

        before = copy.deepcopy(dict(plugin.conf))
        plugin.conf.fail_save = True
        self.set_request_json({
            "revision": cleared["revision"],
            "target": "proxy_url",
            "action": "replace",
            "value": secret_proxy,
        })
        failed = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertEqual(failed["status"], 500)
        self.assertEqual(dict(plugin.conf), before)

    def test_dashboard_configuration_includes_only_sensitive_status(self):
        secret_key = "shared-secret-key"
        secret_proxy = "https://user:secret@proxy.example:8443"
        plugin = self.make_dashboard_plugin(
            generic_api_keys=[secret_key],
            proxy_url=secret_proxy,
        )

        response = asyncio.run(plugin._web_dashboard_configuration_get())

        self.assertTrue(response["ok"])
        self.assertEqual(response["sensitive"]["generic_api_keys"]["count"], 1)
        self.assertTrue(response["sensitive"]["proxy_url"]["write_only"])
        self.assertNotIn(secret_key, str(response))
        self.assertNotIn(secret_proxy, str(response))

    def test_dashboard_parameter_metadata_matches_route_capabilities(self):
        plugin = self.make_dashboard_plugin()
        fields = {field["name"]: field for field in plugin._dashboard_parameter_fields()}

        self.assertEqual(fields["max_output_tokens"]["endpoint_types"], [
            "chat_completions", "gemini_generate_content",
        ])
        self.assertEqual(fields["gemini_resolution"]["route"], "gemini")
        self.assertEqual(fields["seedream_resolution"]["endpoint_types"], ["images_generations"])
        self.assertEqual(fields["quality"]["depends_on"], {
            "field": "enable_gpt_parameters", "equals": True,
        })
        self.assertGreaterEqual(len(fields), 36)

    def test_dashboard_presets_are_saved_independently(self):
        plugin = self.make_dashboard_plugin(prompt_list=[{
            "__template_key": "preset", "command": "旧预设", "prompt": "old prompt",
        }])
        values = plugin._dashboard_configuration_values()
        self.assertNotIn("prompt_list", values)
        revision = plugin._dashboard_preset_revision()

        self.set_request_json({
            "revision": revision,
            "presets": [{"command": "新预设", "prompt": "new prompt"}],
        })
        response = asyncio.run(plugin._web_dashboard_presets_save())

        self.assertTrue(response["ok"])
        self.assertEqual(response["presets"], [{"command": "新预设", "prompt": "new prompt"}])
        self.assertEqual(plugin.prompt_map["新预设"], "new prompt")
        self.assertNotEqual(response["revision"], revision)

    def test_dashboard_presets_reject_stale_file_reload_and_preserve_alias(self):
        plugin = self.make_dashboard_plugin(prompt_list=[{
            "__template_key": "preset",
            "command": "旧名称",
            "prompt": "old prompt",
            "legacy_alias": "figurine_1",
        }])
        stale_revision = plugin._dashboard_preset_revision()

        plugin.conf["prompt_list"] = [{
            "__template_key": "preset",
            "command": "文件重载后的名称",
            "prompt": "reloaded prompt",
            "legacy_alias": "figurine_1",
        }]
        self.set_request_json({
            "revision": stale_revision,
            "presets": [{
                "command": "会覆盖文件的旧页面",
                "prompt": "stale prompt",
                "legacy_alias": "figurine_1",
            }],
        })

        stale = asyncio.run(plugin._web_dashboard_presets_save())

        self.assertEqual(stale["status"], 409)
        self.assertEqual(plugin.conf["prompt_list"][0]["command"], "文件重载后的名称")
        current_revision = plugin._dashboard_preset_revision()
        self.set_request_json({
            "revision": current_revision,
            "presets": [{
                "command": "WebUI改名后",
                "prompt": "saved prompt",
                "legacy_alias": "figurine_1",
            }],
        })
        saved = asyncio.run(plugin._web_dashboard_presets_save())

        self.assertTrue(saved["ok"])
        self.assertEqual(saved["presets"][0]["legacy_alias"], "figurine_1")
        self.assertEqual(plugin.prompt_map["WebUI改名后"], "saved prompt")

    def test_preset_command_advances_preset_revision_only(self):
        plugin = self.make_dashboard_plugin()
        plugin.is_global_admin = lambda event: True
        configuration_before = plugin._dashboard_current_revision()
        preset_before = plugin._dashboard_preset_revision()
        event = types.SimpleNamespace(
            message_str="手办化预设增加 新预设:new prompt",
            plain_result=lambda message: message,
        )

        async def collect_results():
            return [result async for result in plugin.add_preset_prompt(event)]

        results = asyncio.run(collect_results())

        self.assertEqual(len(results), 1)
        self.assertEqual(plugin.prompt_map["新预设"], "new prompt")
        self.assertEqual(plugin._dashboard_current_revision(), configuration_before)
        self.assertNotEqual(plugin._dashboard_preset_revision(), preset_before)

    def test_dashboard_configuration_rejects_stale_revision(self):
        plugin = self.make_dashboard_plugin()
        values = plugin._dashboard_configuration_values()
        revision = plugin._dashboard_configuration_revision(values)
        plugin.conf["model"] = "m2"
        plugin.conf["model_list"] = ["m1", "m2"]
        self.set_request_json({"revision": revision, "config": values})

        response = asyncio.run(plugin._web_dashboard_configuration_save())

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 409)
        self.assertEqual(plugin.conf["model"], "m2")

    def test_dashboard_configuration_restores_memory_when_persistence_fails(self):
        plugin = self.make_dashboard_plugin()
        before = copy.deepcopy(dict(plugin.conf))
        values = plugin._dashboard_configuration_values()
        revision = plugin._dashboard_configuration_revision(values)
        values["model_list"] = ["m1", "m2"]
        values["model"] = "m2"
        plugin.conf.fail_save = True
        self.set_request_json({"revision": revision, "config": values})

        response = asyncio.run(plugin._web_dashboard_configuration_save())

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 500)
        self.assertIn("simulated persistence failure", response["message"])
        self.assertEqual(dict(plugin.conf), before)
        self.assertEqual(plugin.conf.save_calls, 1)

    def test_dashboard_configuration_uses_astrbot_async_config_save(self):
        plugin = self.make_dashboard_plugin()
        values = plugin._dashboard_configuration_values()
        revision = plugin._dashboard_configuration_revision(values)
        values["model_list"] = ["m1", "m2"]
        values["model"] = "m2"
        plugin.conf.save = None
        save_calls = 0

        async def save_config_async():
            nonlocal save_calls
            save_calls += 1

        plugin.conf.save_config_async = save_config_async
        self.set_request_json({"revision": revision, "config": values})

        response = asyncio.run(plugin._web_dashboard_configuration_save())

        self.assertTrue(response["ok"])
        self.assertEqual(save_calls, 1)
        self.assertEqual(plugin.conf["model"], "m2")

    def test_dashboard_configuration_rolls_back_without_persistence_method(self):
        plugin = self.make_dashboard_plugin()
        before = copy.deepcopy(dict(plugin.conf))
        values = plugin._dashboard_configuration_values()
        revision = plugin._dashboard_configuration_revision(values)
        values["model_list"] = ["m1", "m2"]
        values["model"] = "m2"
        plugin.conf.save = None
        self.set_request_json({"revision": revision, "config": values})

        response = asyncio.run(plugin._web_dashboard_configuration_save())

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 500)
        self.assertIn("不支持持久化", response["message"])
        self.assertEqual(dict(plugin.conf), before)

    def test_snapshot_identity_prefers_standard_astrbot_message_objects(self):
        plugin = self.make_plugin([])
        plugin.usage_store = None
        event = types.SimpleNamespace(
            platform="aiocqhttp",
            message_obj=types.SimpleNamespace(
                sender=types.SimpleNamespace(nickname="显示昵称"),
                group=types.SimpleNamespace(group_name="显示群名"),
            ),
        )

        snapshot = asyncio.run(plugin._snapshot_event_identity(event, "10001", "20001"))

        self.assertEqual(snapshot["user_nickname_snapshot"], "显示昵称")
        self.assertEqual(snapshot["group_name_snapshot"], "显示群名")

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

    # ---- Seedream 边长超2000自动升2K ------------------------------------

    def _make_seedream_plugin(self, **overrides):
        entry = {
            "model": "m",
            "enable_seedream_parameters": True,
            "seedream_send_detailed_resolution": True,
        }
        entry.update(overrides)
        return self.make_plugin([entry])

    def test_seedream_side_upgrade_sends_2k_size(self):
        plugin = self._make_seedream_plugin()
        # 默认档位 1.5K 的 21:9 = 2352x1008 超过 2000 → 直接升 2K 档
        self.assertEqual(
            plugin._build_seedream_adaptive_size("m", [], "21:9"),
            "3136x1344",
        )

    def test_seedream_side_upgrade_keeps_fitting_size(self):
        plugin = self._make_seedream_plugin()
        # 1.5K 的 4:3 = 1792x1344 未超限 → 不升级
        self.assertEqual(
            plugin._build_seedream_adaptive_size("m", [], "4:3"),
            "1792x1344",
        )

    def test_seedream_side_upgrade_disabled_keeps_downgrade(self):
        plugin = self._make_seedream_plugin(seedream_side_over_2000_auto_2k=False)
        # 关闭开关 → 维持原降档逻辑（21:9 只剩 1K 合法）
        self.assertEqual(
            plugin._build_seedream_adaptive_size("m", [], "21:9"),
            "1568x672",
        )

    def test_seedream_side_upgrade_respects_pixel_limit(self):
        plugin = self._make_seedream_plugin(seedream_pixel_limit=1000)
        # 像素数上限 1000K 连 2K 档都容纳不下 → 保持降档（像素上限优先）
        self.assertEqual(
            plugin._build_seedream_adaptive_size("m", [], "21:9"),
            "1568x672",
        )

    def test_seedream_side_upgrade_request_parameters(self):
        plugin = self._make_seedream_plugin(seedream_send_aspect_ratio=True)
        request = plugin._get_seedream_request_parameters("m", [], "21:9", False)
        self.assertEqual(request["size"], "3136x1344")
        self.assertEqual(request["aspect_ratio"], "21:9")

    def test_seedream_side_upgrade_triggers_2k_tier(self):
        plugin = self._make_seedream_plugin(deduction_count_2k=5)
        # 参考图边长未超 2000，但比例来源 21:9 使详细分辨率升级为 2K → 按 2K 档扣次
        self.assertEqual(
            plugin._get_resolution_deduction_tier(
                "m",
                image_bytes_list=[b"small"],
                aspect_ratio="21:9",
            ),
            "2K",
        )
        self.assertEqual(
            plugin._get_required_invocation_cost(
                "m",
                image_bytes_list=[b"small"],
                aspect_ratio="21:9",
            ),
            5,
        )

    def test_seedream_without_side_upgrade_keeps_base_tier(self):
        plugin = self._make_seedream_plugin()
        # 4:3 在所选档位下未超限 → 不命中 2K 档
        self.assertIsNone(
            plugin._get_resolution_deduction_tier(
                "m",
                image_bytes_list=[b"small"],
                aspect_ratio="4:3",
            ),
        )

    def test_seedream_side_upgrade_off_no_tier(self):
        plugin = self._make_seedream_plugin(seedream_side_over_2000_auto_2k=False)
        self.assertIsNone(
            plugin._get_resolution_deduction_tier(
                "m",
                image_bytes_list=[b"small"],
                aspect_ratio="21:9",
            ),
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
