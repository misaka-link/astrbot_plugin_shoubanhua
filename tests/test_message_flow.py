import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path


class _CaptureLogger:
    def __init__(self):
        self.records = []

    def info(self, message, *args, **kwargs):
        self.records.append(("info", str(message)))

    def warning(self, message, *args, **kwargs):
        self.records.append(("warning", str(message)))

    def error(self, message, *args, **kwargs):
        self.records.append(("error", str(message)))


class _Component:
    def __init__(self, *args, **kwargs):
        self.args = args
        for key, value in kwargs.items():
            setattr(self, key, value)


class _At(_Component):
    def __init__(self, qq=None, **kwargs):
        super().__init__(qq=qq, **kwargs)


class _Image(_Component):
    def __init__(self, url=None, file=None, data=None, **kwargs):
        super().__init__(url=url, file=file, data=data, **kwargs)

    @classmethod
    def fromBytes(cls, data):
        return cls(data=data)


class _Reply(_Component):
    def __init__(self, id=None, chain=None, **kwargs):
        super().__init__(id=id, chain=chain, **kwargs)


class _Plain(_Component):
    def __init__(self, text):
        super().__init__(text=text)


class _FakeEvent:
    def __init__(
        self,
        *,
        message=None,
        message_id=None,
        raw_message=None,
        message_str="",
        send_error=None,
    ):
        self.message_obj = types.SimpleNamespace(
            message=message,
            message_id=message_id,
            raw_message=raw_message,
        )
        self.message_str = message_str
        self.platform = "test"
        self.is_at_or_wake_command = False
        self.send_error = send_error
        self.sent_results = []
        self.stopped = False

    def chain_result(self, chain):
        return list(chain)

    def plain_result(self, text):
        return self.chain_result([_Plain(text)])

    async def send(self, result):
        self.sent_results.append(result)
        if self.send_error:
            raise self.send_error

    def get_sender_id(self):
        return "sender"

    def get_group_id(self):
        return None

    def stop_event(self):
        self.stopped = True


class _TestConfig(dict):
    def save(self):
        pass


def _install_import_stubs():
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = type("ClientTimeout", (), {})
    aiohttp.ClientSession = type("ClientSession", (), {})
    aiohttp.TCPConnector = type("TCPConnector", (), {})
    aiohttp.FormData = type("FormData", (), {})
    sys.modules["aiohttp"] = aiohttp

    image_module = types.ModuleType("PIL.Image")
    image_module.open = lambda value: (_ for _ in ()).throw(ValueError("not used in these tests"))
    pil_module = types.ModuleType("PIL")
    pil_module.Image = image_module
    sys.modules["PIL"] = pil_module
    sys.modules["PIL.Image"] = image_module

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = _CaptureLogger()
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
    components_module.At = _At
    components_module.Image = _Image
    components_module.Reply = _Reply
    components_module.Plain = _Plain
    components_module.Node = _Component
    components_module.Nodes = _Component
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
        "message": message,
        "status": status_code,
        "data": data,
    }
    sys.modules["astrbot.api.web"] = web_module


def _load_plugin_module():
    _install_import_stubs()
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "message_flow_test_plugin"
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


class MessageFlowTests(unittest.TestCase):
    @staticmethod
    def make_plugin(**extra_conf):
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
            "max_batch_multiplier": 4,
            "max_batch_concurrency": 4,
        }
        config.update(extra_conf)
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig(config)
        plugin.prompt_map = {}
        plugin._dashboard_config_lock = asyncio.Lock()
        return plugin

    def test_build_reply_chain_uses_standard_and_raw_message_ids(self):
        plugin = self.make_plugin()
        raw_object = types.SimpleNamespace(id="object-id")
        cases = [
            ("standard-id", None, "standard-id"),
            (None, {"message_id": "raw-snake"}, "raw-snake"),
            (None, {"messageId": "raw-camel"}, "raw-camel"),
            (None, {"id": "raw-id"}, "raw-id"),
            (None, raw_object, "object-id"),
            (None, {}, None),
        ]

        for message_id, raw_message, expected_id in cases:
            with self.subTest(expected_id=expected_id):
                event = _FakeEvent(message_id=message_id, raw_message=raw_message)
                reply_chain = plugin._build_reply_chain(event)
                if expected_id is None:
                    self.assertEqual(reply_chain, [])
                else:
                    self.assertEqual(len(reply_chain), 1)
                    self.assertEqual(reply_chain[0].id, expected_id)

    def test_image_result_keeps_reply_image_and_caption_order(self):
        plugin = self.make_plugin()
        event = _FakeEvent(message_id="source-message")

        result = plugin._build_image_result(event, b"generated-image", "done")

        self.assertEqual([type(component) for component in result], [_Reply, _Image, _Plain])
        self.assertEqual(result[0].id, "source-message")
        self.assertEqual(result[1].data, b"generated-image")
        self.assertEqual(result[2].text, "done")
        self.assertEqual(event.sent_results, [])

    def test_reply_images_are_loaded_before_current_images(self):
        workflow = FigurineProPlugin.ImageWorkflow(max_retries=0)
        event = _FakeEvent(message=[
            _Reply(id="quoted", chain=[_Image(url="reply-url"), _Image(file="reply-file")]),
            _Image(url="current-url"),
            _At(qq="42"),
        ])
        images_by_source = {
            "reply-url": b"reply-url-bytes",
            "reply-file": b"reply-file-bytes",
            "current-url": b"current-url-bytes",
        }

        async def load_bytes(source):
            return images_by_source.get(source)

        async def get_avatar(user_id):
            self.assertEqual(user_id, "42")
            return b"avatar-bytes"

        workflow._load_bytes = load_bytes
        workflow._get_avatar = get_avatar

        images = asyncio.run(workflow.get_images(event))

        self.assertEqual(images, [
            b"reply-url-bytes",
            b"reply-file-bytes",
            b"current-url-bytes",
            b"avatar-bytes",
        ])

    def test_quote_without_expanded_chain_falls_back_safely(self):
        workflow = FigurineProPlugin.ImageWorkflow(max_retries=0)
        event = _FakeEvent(message=[_Reply(id="quote-only")])
        PLUGIN_MODULE.logger.records.clear()

        images = asyncio.run(workflow.get_images(event))

        self.assertEqual(images, [])
        self.assertTrue(any(
            "引用消息未携带可解析的内容链" in message
            for level, message in PLUGIN_MODULE.logger.records
            if level == "info"
        ))

    def test_missing_message_components_are_safe(self):
        workflow = FigurineProPlugin.ImageWorkflow(max_retries=0)
        event = types.SimpleNamespace(message_obj=None, message_str=None, platform="test")

        images = asyncio.run(workflow.get_images(event))

        self.assertEqual(images, [])

    def test_llm_tool_delivery_failure_sends_exactly_once(self):
        plugin = self.make_plugin()
        event = _FakeEvent(send_error=RuntimeError("ambiguous delivery failure"))
        settlements = []
        daily_usage = []

        plugin._get_maintenance_message = lambda: None
        plugin._get_llm_tool_model = lambda value: ("m1", None)
        plugin._get_llm_tool_aspect_ratio = lambda value: (None, None)
        plugin._get_llm_tool_batch_count = lambda value: (1, None)

        async def call_api(*args, **kwargs):
            return b"generated-image", 200, {
                "actual_model": "m1",
                "image_bytes_list": [],
            }

        async def settle(**kwargs):
            settlements.append(kwargs)

        async def record_daily(sender_id, group_id):
            daily_usage.append((sender_id, group_id))

        plugin._call_api = call_api
        plugin._settle_usage_generation = settle
        plugin._record_daily_usage = record_daily

        payload = json.loads(asyncio.run(plugin._run_llm_image_tool(
            event,
            prompt="a test image",
        )))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["generated"], 0)
        self.assertEqual(payload["failures"][0]["error_type"], "delivery_failed")
        self.assertEqual(len(event.sent_results), 1)
        self.assertEqual(len(settlements), 1)
        self.assertEqual(daily_usage, [("sender", None)])

    def test_reserved_command_does_not_enter_generic_handler(self):
        plugin = self.make_plugin(prefix=False)
        event = _FakeEvent(message_str="文生图 a test image")

        results = asyncio.run(self._collect(plugin.on_figurine_request(event)))

        self.assertEqual(results, [])
        self.assertFalse(event.stopped)

    def test_dashboard_rejects_reserved_prefix_and_preset(self):
        plugin = self.make_plugin()
        values = plugin._dashboard_configuration_values()
        values["extra_prefix"] = ["文生图"]

        with self.assertRaisesRegex(ValueError, "插件专用指令"):
            plugin._validate_dashboard_configuration(values)
        values = plugin._dashboard_configuration_values()
        values["settings"]["help_command"] = "文生图"
        with self.assertRaisesRegex(ValueError, "插件专用指令"):
            plugin._validate_dashboard_configuration(values)
        with self.assertRaisesRegex(
            ValueError,
            "预设指令“文生图”与插件专用指令“文生图”冲突",
        ):
            plugin._dashboard_validate_presets([
                {"command": "文生图", "prompt": "test prompt"},
            ], prefixes=["bnn"])

    def test_chat_preset_addition_rejects_reserved_command(self):
        plugin = self.make_plugin()
        plugin.is_global_admin = lambda event: True
        event = _FakeEvent(message_str="手办化预设增加 文生图:test prompt")

        results = asyncio.run(self._collect(plugin.add_preset_prompt(event)))

        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0][-1], _Plain)
        self.assertIn("预设指令“文生图”与插件专用指令“文生图”冲突", results[0][-1].text)
        self.assertEqual(plugin.conf["prompt_list"], [])

    def test_plugin_file_logger_is_reused_without_touching_foreign_handlers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            logger = PLUGIN_MODULE._plugin_logger(data_dir)
            foreign_handler = logging.NullHandler()
            logger.addHandler(foreign_handler)
            try:
                first = PLUGIN_MODULE._plugin_logger(data_dir)
                second = PLUGIN_MODULE._plugin_logger(data_dir)
                owned_handlers = [
                    handler for handler in logger.handlers
                    if getattr(handler, PLUGIN_MODULE.PLUGIN_LOG_HANDLER_MARKER, False)
                ]

                self.assertIs(first, second)
                self.assertEqual(len(owned_handlers), 1)
                self.assertIn(foreign_handler, logger.handlers)
                logger.info("plugin file log test")
                for handler in owned_handlers:
                    handler.flush()
                content = (data_dir / "logs" / "figurine_pro.log").read_text(encoding="utf-8")
                self.assertIn("plugin file log test", content)
            finally:
                logger.removeHandler(foreign_handler)
                for handler in list(logger.handlers):
                    if getattr(handler, PLUGIN_MODULE.PLUGIN_LOG_HANDLER_MARKER, False):
                        logger.removeHandler(handler)
                        handler.close()

    def test_schema_presets_drive_default_commands_without_python_mapping(self):
        plugin = self.make_plugin(prompt_list=[{
            "__template_key": "preset",
            "command": "我的手办",
            "prompt": "schema driven prompt",
            "legacy_alias": "figurine_1",
        }])

        asyncio.run(plugin._load_prompt_map())

        self.assertEqual(plugin.prompt_map, {"我的手办": "schema driven prompt"})
        self.assertEqual(plugin._get_default_preset_commands(), {"我的手办"})
        self.assertIsNone(plugin._get_custom_preset_prompt("我的手办"))

    def test_preset_conflicts_identify_source_and_allow_default_override(self):
        plugin = self.make_plugin(prompt_list=[{
            "__template_key": "preset",
            "command": "手办化5",
            "prompt": "customized default prompt",
            "legacy_alias": "figurine_5",
        }])

        saved = plugin._dashboard_validate_presets(plugin._dashboard_preset_items(), prefixes=["bnn"])
        self.assertEqual(saved[0]["command"], "手办化5")
        self.assertEqual(saved[0]["legacy_alias"], "figurine_5")

        with self.assertRaisesRegex(ValueError, "自定义触发词“bnn”冲突"):
            plugin._dashboard_validate_presets([
                {"command": "bnn", "prompt": "invalid"},
            ], prefixes=["bnn"])
        with self.assertRaisesRegex(ValueError, "另一条预设指令重复"):
            plugin._dashboard_validate_presets([
                {"command": "same", "prompt": "first"},
                {"command": "same", "prompt": "second"},
            ], prefixes=["bnn"])

    def test_legacy_builtin_prompt_migrates_to_schema_preset(self):
        plugin = self.make_plugin(
            prompt_list=[],
            prompts={"figurine_1": {"default": "legacy figurine prompt"}},
        )

        asyncio.run(plugin._migrate_prompt_list_config())

        presets = {item["command"]: item for item in plugin.conf["prompt_list"]}
        self.assertEqual(presets["手办化"]["prompt"], "legacy figurine prompt")
        self.assertEqual(presets["手办化"]["legacy_alias"], "figurine_1")
        self.assertNotIn("prompts", plugin.conf)
        self.assertEqual(plugin.conf["_preset_schema_version"], 1)

    @staticmethod
    async def _collect(generator):
        return [result async for result in generator]


if __name__ == "__main__":
    unittest.main()
