import asyncio
import copy
import sys
import types
import unittest
from pathlib import Path

# Use the test stubs and plugin loader from test_resolution_deduction
from tests.test_resolution_deduction import (
    PLUGIN_MODULE,
    FigurineProPlugin,
    _TestConfig,
)


class ApiKeyBindingsTests(unittest.TestCase):
    def make_dashboard_plugin(self, **extra_conf):
        config = {
            "model": "m1",
            "model_list": ["m1", "m2", "grok-model"],
            "generic_api_url": "https://generic.example/v1",
            "gemini_model_list": [],
            "chat_completions_model_list": ["m1", "m2", "grok-model"],
            "images_generations_model_list": [],
            "images_edits_model_list": [],
            "extra_prefix": [{"__template_key": "prefix", "prefix": "bnn"}],
            "prompt_list": [],
            "command_model_list": [],
            "model_mapping_list": [],
            "model_prompt_template_list": [],
            "model_parameter_list": [],
            "generic_api_keys": [],
            "api_key_list": [],
            **extra_conf,
        }
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig(config)
        plugin.prompt_map = {}
        plugin._dashboard_config_lock = asyncio.Lock()
        plugin.key_lock = asyncio.Lock()
        plugin.generic_key_index = 0
        plugin.gemini_key_index = 0
        plugin.tag_key_indices = {}
        plugin.request_timeout = 120
        plugin.download_timeout = 240
        plugin.iwf = types.SimpleNamespace(get_request_kwargs=lambda: {})
        return plugin

    @staticmethod
    def set_request_json(payload):
        async def read_json(default=None):
            return copy.deepcopy(payload)

        PLUGIN_MODULE.request.json = read_json

    def test_mask_api_key(self):
        plugin = self.make_dashboard_plugin()
        self.assertEqual(plugin._mask_api_key(""), "")
        self.assertEqual(plugin._mask_api_key("short"), "****")
        self.assertEqual(plugin._mask_api_key("1234567890"), "123****890")
        self.assertEqual(plugin._mask_api_key("sk-proj-12345678abcdef"), "sk-p****cdef")

    def test_migration_from_generic_api_keys(self):
        plugin = self.make_dashboard_plugin(
            generic_api_keys=["sk-first", "sk-second", "sk-third"],
            api_key_list=[],
        )
        asyncio.run(plugin._migrate_api_keys_config())
        keys = plugin.conf["api_key_list"]
        self.assertEqual(len(keys), 3)
        self.assertEqual(keys[0]["tag"], "默认")
        self.assertEqual(keys[0]["key"], "sk-first")
        self.assertTrue(keys[0]["is_default"])
        self.assertEqual(keys[1]["tag"], "Key-2")
        self.assertEqual(keys[1]["key"], "sk-second")
        self.assertFalse(keys[1]["is_default"])
        self.assertEqual(keys[2]["tag"], "Key-3")
        self.assertEqual(keys[2]["key"], "sk-third")

    def test_get_api_key_for_request_by_bound_tag(self):
        plugin = self.make_dashboard_plugin(
            api_key_list=[
                {"__template_key": "api_key", "tag": "默认", "key": "sk-default-key", "is_default": True},
                {"__template_key": "api_key", "tag": "Grok专线", "key": "sk-grok-key", "is_default": False},
                {"__template_key": "api_key", "tag": "备用Key", "key": "sk-backup-key", "is_default": False},
            ],
            model_parameter_list=[
                {
                    "__template_key": "model_parameters",
                    "model": "grok-model",
                    "api_key_tag": "Grok专线",
                },
                {
                    "__template_key": "model_parameters",
                    "model": "m2",
                    "api_key_tag": "备用Key",
                },
            ],
        )

        # 1. grok-model bound to "Grok专线"
        key_grok = asyncio.run(plugin._get_api_key_for_request("generic", model_name="grok-model"))
        self.assertEqual(key_grok, "sk-grok-key")

        # 2. m2 bound to "备用Key"
        key_m2 = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m2"))
        self.assertEqual(key_m2, "sk-backup-key")

        # 3. m1 has no binding -> defaults to "默认"
        key_m1 = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m1"))
        self.assertEqual(key_m1, "sk-default-key")

    def test_fallback_to_default_key_when_tag_not_found(self):
        plugin = self.make_dashboard_plugin(
            api_key_list=[
                {"__template_key": "api_key", "tag": "默认", "key": "sk-default-key", "is_default": True},
            ],
            model_parameter_list=[
                {
                    "__template_key": "model_parameters",
                    "model": "m1",
                    "api_key_tag": "DeletedTag",
                },
            ],
        )
        # DeletedTag does not exist -> gracefully falls back to default key
        key = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m1"))
        self.assertEqual(key, "sk-default-key")

    def test_round_robin_rotation_for_same_tag(self):
        plugin = self.make_dashboard_plugin(
            api_key_list=[
                {"__template_key": "api_key", "tag": "负载池", "key": "sk-pool-1", "is_default": False},
                {"__template_key": "api_key", "tag": "负载池", "key": "sk-pool-2", "is_default": False},
            ],
            model_parameter_list=[
                {
                    "__template_key": "model_parameters",
                    "model": "m1",
                    "api_key_tag": "负载池",
                },
            ],
        )
        first = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m1"))
        second = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m1"))
        third = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m1"))
        self.assertEqual(first, "sk-pool-1")
        self.assertEqual(second, "sk-pool-2")
        self.assertEqual(third, "sk-pool-1")

    def test_web_sensitive_api_key_management_flow(self):
        plugin = self.make_dashboard_plugin()
        revision = plugin._dashboard_current_revision()

        # 1. Add "默认" key
        self.set_request_json({
            "revision": revision,
            "target": "api_keys",
            "action": "add",
            "tag": "默认",
            "key": "sk-first-key-12345678",
            "is_default": True,
        })
        resp = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp["ok"])
        sensitive = resp["sensitive"]
        self.assertEqual(len(sensitive["api_keys"]), 1)
        self.assertEqual(sensitive["api_keys"][0]["tag"], "默认")
        self.assertTrue(sensitive["api_keys"][0]["is_default"])
        self.assertNotIn("sk-first-key-12345678", str(sensitive))

        # 2. Add second key "Grok"
        self.set_request_json({
            "revision": resp["revision"],
            "target": "api_keys",
            "action": "add",
            "tag": "Grok",
            "key": "sk-grok-secret-87654321",
            "is_default": False,
        })
        resp2 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp2["ok"])
        self.assertEqual(len(resp2["sensitive"]["api_keys"]), 2)

        # 3. Update "Grok" -> rename tag to "Grok专线" and test model parameter linkage
        plugin.conf["model_parameter_list"] = [
            {"__template_key": "model_parameters", "model": "m2", "api_key_tag": "Grok"}
        ]
        self.set_request_json({
            "revision": plugin._dashboard_current_revision(),
            "target": "api_keys",
            "action": "update",
            "old_tag": "Grok",
            "tag": "Grok专线",
        })
        resp3 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp3["ok"])
        # Check that model_parameter_list reference was updated
        self.assertEqual(plugin.conf["model_parameter_list"][0]["api_key_tag"], "Grok专线")

        # 4. Set "Grok专线" as default
        self.set_request_json({
            "revision": resp3["revision"],
            "target": "api_keys",
            "action": "set_default",
            "tag": "Grok专线",
        })
        resp4 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp4["ok"])
        keys = resp4["sensitive"]["api_keys"]
        grok_entry = next(k for k in keys if k["tag"] == "Grok专线")
        self.assertTrue(grok_entry["is_default"])

        # 5. Delete "Grok专线"
        self.set_request_json({
            "revision": resp4["revision"],
            "target": "api_keys",
            "action": "delete",
            "tag": "Grok专线",
        })
        resp5 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp5["ok"])
        self.assertEqual(len(resp5["sensitive"]["api_keys"]), 1)
        self.assertTrue(resp5["sensitive"]["api_keys"][0]["is_default"])

        # 6. Clear all
        self.set_request_json({
            "revision": resp5["revision"],
            "target": "api_keys",
            "action": "clear",
        })
        resp6 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp6["ok"])
        self.assertEqual(len(resp6["sensitive"]["api_keys"]), 0)

    def test_model_parameters_fields_and_normalization(self):
        plugin = self.make_dashboard_plugin(
            api_key_list=[
                {"__template_key": "api_key", "tag": "默认", "key": "sk-1", "is_default": True},
                {"__template_key": "api_key", "tag": "备用", "key": "sk-2", "is_default": False},
            ]
        )
        fields = plugin._dashboard_parameter_fields()
        api_key_field = next(f for f in fields if f["name"] == "api_key_tag")
        self.assertEqual(api_key_field["label"], "绑定 Key")
        self.assertEqual(api_key_field["group"], "基础与额度")
        self.assertEqual(api_key_field["default"], "默认")

        # Test normalization preserves api_key_tag
        normalized = plugin._dashboard_normalize_model_parameters(
            [{"model": "m1", "api_key_tag": "备用"}],
            {"m1"},
        )
        self.assertEqual(normalized[0]["api_key_tag"], "备用")

        # Test default normalization when missing
        normalized_def = plugin._dashboard_normalize_model_parameters(
            [{"model": "m1"}],
            {"m1"},
        )
        self.assertEqual(normalized_def[0]["api_key_tag"], "默认")

    def test_call_api_once_uses_bound_key(self):
        PLUGIN_MODULE.aiohttp.ClientTimeout = type("ClientTimeout", (), {"__init__": lambda self, *a, **kw: None})
        plugin = self.make_dashboard_plugin(
            api_key_list=[
                {"__template_key": "api_key", "tag": "默认", "key": "sk-default", "is_default": True},
                {"__template_key": "api_key", "tag": "专线", "key": "sk-dedicated", "is_default": False},
            ],
            model_parameter_list=[
                {
                    "__template_key": "model_parameters",
                    "model": "m2",
                    "api_key_tag": "专线",
                }
            ],
        )
        captured_headers = {}

        class _MockResponse:
            status = 200
            headers = {}
            async def json(self):
                return {"choices": [{"message": {"content": "https://img.example/1.png"}}]}
            async def text(self):
                return '{"choices": [{"message": {"content": "https://img.example/1.png"}}]}'
            async def read(self):
                return b"fake-png-bytes"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class _MockSession:
            def post(self, url, **kwargs):
                captured_headers.update(kwargs.get("headers", {}))
                return _MockResponse()
            def get(self, url, **kwargs):
                return _MockResponse()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        plugin._get_http_session = lambda *args, **kwargs: _MockSession()
        plugin.iwf.create_client_session = lambda **kw: _MockSession()
        plugin.iwf._download_image = lambda *args, **kwargs: asyncio.sleep(0, result=b"img-bytes")
        plugin.conf["use_stream"] = False
        plugin._download_image_with_retry = lambda *args, **kwargs: asyncio.sleep(0, result=b"img-bytes")

        # Call for m2 (bound to "专线" -> "sk-dedicated")
        ctx_m2 = plugin._get_request_context("m2", "m2", False)
        asyncio.run(plugin._call_api_once([], "prompt", override_model="m2", request_context=ctx_m2))
        self.assertEqual(captured_headers.get("Authorization"), "Bearer sk-dedicated")

        # Call for m1 (unbound -> defaults to "默认" -> "sk-default")
        captured_headers.clear()
        ctx_m1 = plugin._get_request_context("m1", "m1", False)
        asyncio.run(plugin._call_api_once([], "prompt", override_model="m1", request_context=ctx_m1))
        self.assertEqual(captured_headers.get("Authorization"), "Bearer sk-default")

    def test_request_model_name_dispatch_and_fallback(self):
        PLUGIN_MODULE.aiohttp.ClientTimeout = type("ClientTimeout", (), {"__init__": lambda self, *a, **kw: None})
        plugin = self.make_dashboard_plugin(
            api_key_list=[
                {"__template_key": "api_key", "tag": "默认", "key": "sk-test", "is_default": True},
            ],
            model_parameter_list=[
                {
                    "__template_key": "model_parameters",
                    "model": "m2",
                    "request_model_name": "upstream-gpt-4o",
                },
                {
                    "__template_key": "model_parameters",
                    "model": "m1",
                    "request_model_name": "",  # 默认未填
                },
            ],
        )
        captured_payloads = {}
        captured_urls = []

        class _MockResponse:
            status = 200
            headers = {}
            async def json(self):
                return {"choices": [{"message": {"content": "https://img.example/1.png"}}]}
            async def text(self):
                return '{"choices": [{"message": {"content": "https://img.example/1.png"}}]}'
            async def read(self):
                return b"fake-png-bytes"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class _MockSession:
            def post(self, url, **kwargs):
                captured_urls.append(url)
                captured_payloads.update(kwargs.get("json", {}))
                return _MockResponse()
            def get(self, url, **kwargs):
                return _MockResponse()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        plugin._get_http_session = lambda *args, **kwargs: _MockSession()
        plugin.iwf.create_client_session = lambda **kw: _MockSession()
        plugin.iwf._download_image = lambda *args, **kwargs: asyncio.sleep(0, result=b"img-bytes")
        plugin.conf["use_stream"] = False
        plugin._download_image_with_retry = lambda *args, **kwargs: asyncio.sleep(0, result=b"img-bytes")

        # 1. Calling m2: has request_model_name="upstream-gpt-4o"
        ctx_m2 = plugin._get_request_context("m2", "m2", False)
        self.assertEqual(ctx_m2["request_model_name"], "upstream-gpt-4o")
        asyncio.run(plugin._call_api_once([], "prompt", override_model="m2", request_context=ctx_m2))
        self.assertEqual(captured_payloads.get("model"), "upstream-gpt-4o")

        # 2. Calling m1: request_model_name is empty -> defaults to "m1"
        captured_payloads.clear()
        ctx_m1 = plugin._get_request_context("m1", "m1", False)
        self.assertEqual(ctx_m1["request_model_name"], "m1")
        asyncio.run(plugin._call_api_once([], "prompt", override_model="m1", request_context=ctx_m1))
        self.assertEqual(captured_payloads.get("model"), "m1")

        # 3. Gemini endpoint replacement test
        plugin.conf["gemini_model_list"] = ["gemini-custom"]
        plugin.conf["model_parameter_list"].append({
            "__template_key": "model_parameters",
            "model": "gemini-custom",
            "request_model_name": "gemini-2.5-flash-image",
        })
        captured_urls.clear()
        ctx_gemini = plugin._get_request_context("gemini-custom", "gemini-custom", False)
        self.assertEqual(ctx_gemini["api_route"], "gemini")
        self.assertEqual(ctx_gemini["request_model_name"], "gemini-2.5-flash-image")
        asyncio.run(plugin._call_api_once([], "prompt", override_model="gemini-custom", request_context=ctx_gemini))
        self.assertTrue(any("gemini-2.5-flash-image:generateContent" in u for u in captured_urls))

    def test_user_can_set_arbitrary_custom_key_tags(self):
        plugin = self.make_dashboard_plugin()
        revision = plugin._dashboard_current_revision()

        # 1. User adds key with arbitrary custom tag
        self.set_request_json({
            "revision": revision,
            "target": "api_keys",
            "action": "add",
            "tag": "超级VIP专线_01",
            "key": "sk-vip-custom-12345",
            "is_default": True,
        })
        resp1 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp1["ok"])
        tags = [k["tag"] for k in resp1["sensitive"]["api_keys"]]
        self.assertIn("超级VIP专线_01", tags)

        # 2. User adds another custom tag
        self.set_request_json({
            "revision": resp1["revision"],
            "target": "api_keys",
            "action": "add",
            "tag": "账号B-按量付费",
            "key": "sk-account-b-67890",
            "is_default": False,
        })
        resp2 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp2["ok"])

        # 3. Model parameter binds to this custom tag
        plugin.conf["model_parameter_list"] = [
            {"__template_key": "model_parameters", "model": "m2", "api_key_tag": "账号B-按量付费"}
        ]
        key = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m2"))
        self.assertEqual(key, "sk-account-b-67890")

        # 4. User can rename custom tag to "账号B_主力"
        self.set_request_json({
            "revision": plugin._dashboard_current_revision(),
            "target": "api_keys",
            "action": "update",
            "old_tag": "账号B-按量付费",
            "tag": "账号B_主力",
        })
        resp3 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp3["ok"])
        # Model parameter automatically updated to new tag
        self.assertEqual(plugin.conf["model_parameter_list"][0]["api_key_tag"], "账号B_主力")
        key_updated = asyncio.run(plugin._get_api_key_for_request("generic", model_name="m2"))
        self.assertEqual(key_updated, "sk-account-b-67890")

    def test_custom_api_url_per_key_and_fallback(self):
        PLUGIN_MODULE.aiohttp.ClientTimeout = type("ClientTimeout", (), {"__init__": lambda self, *a, **kw: None})
        plugin = self.make_dashboard_plugin(
            generic_api_url="https://global-default.example/v1/chat/completions",
            api_key_list=[
                {
                    "__template_key": "api_key",
                    "tag": "默认",
                    "key": "sk-default",
                    "api_url": "",  # 留空使用全局默认
                    "is_default": True,
                },
                {
                    "__template_key": "api_key",
                    "tag": "自定义中转",
                    "key": "sk-custom",
                    "api_url": "https://custom-gateway.example/v1",  # 单独配置
                    "is_default": False,
                },
            ],
            model_parameter_list=[
                {
                    "__template_key": "model_parameters",
                    "model": "m2",
                    "api_key_tag": "自定义中转",
                },
            ],
        )
        captured_urls = []
        captured_headers = []

        class _MockResponse:
            status = 200
            headers = {}
            async def json(self):
                return {"choices": [{"message": {"content": "https://img.example/1.png"}}]}
            async def text(self):
                return '{"choices": [{"message": {"content": "https://img.example/1.png"}}]}'
            async def read(self):
                return b"fake-png-bytes"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class _MockSession:
            def post(self, url, **kwargs):
                captured_urls.append(url)
                captured_headers.append(kwargs.get("headers", {}))
                return _MockResponse()
            def get(self, url, **kwargs):
                return _MockResponse()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        plugin._get_http_session = lambda *args, **kwargs: _MockSession()
        plugin.iwf.create_client_session = lambda **kw: _MockSession()
        plugin.iwf._download_image = lambda *args, **kwargs: asyncio.sleep(0, result=b"img-bytes")
        plugin.conf["use_stream"] = False
        plugin._download_image_with_retry = lambda *args, **kwargs: asyncio.sleep(0, result=b"img-bytes")

        # 1. m2 绑定了 "自定义中转"，应请求 custom-gateway
        ctx_m2 = plugin._get_request_context("m2", "m2", False)
        asyncio.run(plugin._call_api_once([], "prompt", override_model="m2", request_context=ctx_m2))
        self.assertEqual(len(captured_urls), 1)
        self.assertTrue(captured_urls[0].startswith("https://custom-gateway.example/v1/chat/completions"))
        self.assertEqual(captured_headers[0].get("Authorization"), "Bearer sk-custom")

        # 2. m1 未单独绑定，应回退默认 Key，且默认 Key 的 api_url 为空，应回退全局默认地址
        ctx_m1 = plugin._get_request_context("m1", "m1", False)
        asyncio.run(plugin._call_api_once([], "prompt", override_model="m1", request_context=ctx_m1))
        self.assertEqual(len(captured_urls), 2)
        self.assertTrue(captured_urls[1].startswith("https://global-default.example/v1/chat/completions"))
        self.assertEqual(captured_headers[1].get("Authorization"), "Bearer sk-default")

    def test_web_sensitive_key_custom_api_url_management(self):
        plugin = self.make_dashboard_plugin()
        revision = plugin._dashboard_current_revision()

        # 1. 添加带自定义 API 地址的 Key
        self.set_request_json({
            "revision": revision,
            "target": "api_keys",
            "action": "add",
            "tag": "专线A",
            "key": "sk-key-a-123456",
            "api_url": "https://api-a.example/v1",
            "is_default": True,
        })
        resp = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp["ok"])
        keys = resp["sensitive"]["api_keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["tag"], "专线A")
        self.assertEqual(keys[0]["api_url"], "https://api-a.example/v1")

        # 2. 更新 Key 的自定义 API 地址
        self.set_request_json({
            "revision": resp["revision"],
            "target": "api_keys",
            "action": "update",
            "old_tag": "专线A",
            "tag": "专线A-改",
            "api_url": "https://api-a-new.example/v1",
        })
        resp2 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp2["ok"])
        keys2 = resp2["sensitive"]["api_keys"]
        self.assertEqual(keys2[0]["tag"], "专线A-改")
        self.assertEqual(keys2[0]["api_url"], "https://api-a-new.example/v1")

        # 3. 批量导入支持 代号:Key:URL
        self.set_request_json({
            "revision": resp2["revision"],
            "target": "api_keys",
            "action": "batch_append",
            "values": [
                "备用1:sk-bak1-12345:https://bak1.example/v1",
                "备用2:sk-bak2-67890",
            ],
        })
        resp3 = asyncio.run(plugin._web_dashboard_sensitive_save())
        self.assertTrue(resp3["ok"])
        keys3 = resp3["sensitive"]["api_keys"]
        bak1 = next(k for k in keys3 if k["tag"] == "备用1")
        bak2 = next(k for k in keys3 if k["tag"] == "备用2")
        self.assertEqual(bak1["api_url"], "https://bak1.example/v1")
        self.assertEqual(bak2["api_url"], "")


if __name__ == "__main__":
    unittest.main()
