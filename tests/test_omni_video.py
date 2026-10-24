"""Omni 1.1 Flash 视频（flow2api 厂商参数模式）测试。

覆盖：参数模式判定与归一化、模型名模板拼装、首图横竖识别、时长分档计费、
流式阶段切换识别与两段耗时、视频地址解析、视频分支发送与结算。
"""
import asyncio
import importlib.util
import json
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
    if "omni_video_test_plugin" in sys.modules:
        return

    aiohttp = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    aiohttp.ClientTimeout = _ClientTimeout
    aiohttp.ClientSession = type("ClientSession", (), {})
    aiohttp.TCPConnector = type("TCPConnector", (), {})
    aiohttp.FormData = type("FormData", (), {})
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    existing_aiohttp = sys.modules.get("aiohttp")
    if existing_aiohttp is not None:
        # 其他测试模块可能已装过简化桩，这里补齐本用例需要的能力
        existing_aiohttp.ClientTimeout = _ClientTimeout
        if not hasattr(existing_aiohttp, "ClientError"):
            existing_aiohttp.ClientError = aiohttp.ClientError
        aiohttp = existing_aiohttp
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
    for name in ("At", "Node", "Nodes"):
        setattr(components_module, name, type(name, (), {}))

    class _Reply:
        def __init__(self, id=None, **kwargs):
            self.id = id

        def __repr__(self):
            return f"Reply({self.id})"

    components_module.Reply = _Reply

    class _Image:
        def __init__(self, file=None, **kwargs):
            self.file = file

        @classmethod
        def fromURL(cls, url, **kwargs):
            return cls(file=url)

        @classmethod
        def fromBytes(cls, data):
            return cls(file=f"bytes://{len(data)}")

        @classmethod
        def fromFileSystem(cls, path, **kwargs):
            return cls(file=path)

        @classmethod
        def fromBase64(cls, data, **kwargs):
            return cls(file="base64://")

        def __repr__(self):
            return f"Image({self.file})"

    components_module.Image = _Image

    class _Plain:
        def __init__(self, text=""):
            self.text = text

        def __repr__(self):
            return str(self.text)

    components_module.Plain = _Plain
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
    package_name = "omni_video_test_plugin"
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


# --------------------------------------------------------------------------
# 测试桩
# --------------------------------------------------------------------------
class _FakeContextManager:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


class _FakeResponse:
    def __init__(self, status=200, chunks=None, text=""):
        self.status = status
        self._chunks = list(chunks or [])
        self._text = text
        self.content = self

    async def text(self):
        return self._text

    async def read(self):
        return b"".join(self._chunks)

    def iter_chunked(self, size):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self._response

    def get(self, url, **kwargs):
        return self._response


class _FakeWorkflow:
    def __init__(self, response):
        self._response = response
        self.session = _FakeSession(response)
        self.max_videos = 2

    def get_request_kwargs(self):
        return {}

    def create_client_session(self, timeout=None):
        return _FakeContextManager(self.session)


class _FakeEvent:
    def __init__(self, sender_id="u1", group_id=None):
        self._sender_id = sender_id
        self._group_id = group_id
        self.message_obj = None
        self.sent = []

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def chain_result(self, chain):
        return chain

    def plain_result(self, text):
        return [text]

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        pass


def _sse(payload):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _delta(text):
    return _sse({"choices": [{"delta": {"content": text}}]})


def _progress(text):
    """flow2api 契约：过程文字走 delta.reasoning_content。"""
    return _sse({"choices": [{"delta": {"reasoning_content": text}}]})


def _final(text):
    """flow2api 契约：终态产物走 delta.content + finish_reason=stop。"""
    return _sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]})


class OmniVideoTests(unittest.TestCase):
    def setUp(self):
        _install_import_stubs()

    def tearDown(self):
        # 还原标准桩：PIL.Image.open 抛错、无 ImageOps，避免污染后续测试模块
        image_module = types.ModuleType("PIL.Image")
        image_module.open = lambda value: (_ for _ in ()).throw(ValueError("unused"))
        pil_module = types.ModuleType("PIL")
        pil_module.Image = image_module
        sys.modules["PIL"] = pil_module
        sys.modules["PIL.Image"] = image_module
        sys.modules.pop("PIL.ImageOps", None)
        PLUGIN_MODULE.PILImage = image_module

    # ---- 构造工具 ----------------------------------------------------
    @staticmethod
    def make_plugin(parameter_entries=None, **extra_conf):
        plugin = object.__new__(FigurineProPlugin)
        plugin.conf = _TestConfig({
            "model": "omni-1.1-flash",
            "model_list": ["omni-1.1-flash"],
            "generic_api_url": "https://api.example.com",
            "gemini_model_list": [],
            "chat_completions_model_list": ["omni-1.1-flash"],
            "images_generations_model_list": [],
            "images_edits_model_list": [],
            "extra_prefix": [],
            "prompt_list": [],
            "command_model_list": [],
            "model_mapping_list": [],
            "model_prompt_template_list": [],
            "model_parameter_list": list(parameter_entries or []),
            "prefix": False,
            **extra_conf,
        })
        plugin.prompt_map = {}
        plugin.preset_ratio_map = {}
        plugin.iwf = None
        plugin.request_timeout = 30
        plugin.download_timeout = 60
        plugin._dashboard_config_lock = asyncio.Lock()
        return plugin

    @staticmethod
    def omni_parameters(**overrides):
        parameters = {
            "parameter_mode": "omni",
            "enable_omni_parameters": True,
            "omni_model_template": "omni-1.1-flash-{duration}s-{orientation}",
            "omni_video_duration": "8",
            "omni_aspect_mode": "auto",
            "omni_default_aspect": "portrait",
            "omni_super_resolution": False,
            "omni_super_resolution_target": "1080p",
            "omni_upscale_start_message": "开始超分 {resolution}",
            "omni_upscale_start_pattern": r"视频生成完成.*?放大",
            "omni_result_caption": "完成 生成{generate_time} 超分{upscale_time}",
            "omni_show_timing": True,
            "omni_timeout": 900,
            "omni_max_reference_images": 3,
            "omni_charge_amount_4s": 0,
            "omni_charge_amount_6s": 0,
            "omni_charge_amount_8s": 0,
            "omni_charge_amount_10s": 0,
            "omni_upscale_extra_charge": 0,
            "charge_amount": 1000,
            "request_model_name": "",
        }
        parameters.update(overrides)
        return parameters

    # ---- 参数模式判定 ------------------------------------------------
    def test_is_video_parameters_by_mode(self):
        plugin = self.make_plugin()
        self.assertTrue(plugin._is_video_parameters({"parameter_mode": "omni"}))
        self.assertTrue(plugin._is_video_parameters({"enable_omni_parameters": True}))
        self.assertFalse(plugin._is_video_parameters({"parameter_mode": "seedream"}))
        self.assertFalse(plugin._is_video_parameters(None))

    def test_legacy_enable_field_infers_omni_mode(self):
        plugin = self.make_plugin()
        self.assertEqual(
            plugin._get_parameter_mode_from_entry({"enable_omni_parameters": True}),
            "omni",
        )
        self.assertEqual(
            plugin._get_parameter_mode_from_entry({"parameter_mode": "omni"}),
            "omni",
        )

    # ---- 归一化 ------------------------------------------------------
    def test_model_parameters_normalization(self):
        plugin = self.make_plugin([
            {
                "model": "omni-1.1-flash",
                "parameter_mode": "omni",
                "omni_video_duration": 6,
                "omni_aspect_mode": "竖屏",
                "omni_default_aspect": "landscape",
                "omni_super_resolution": True,
                "omni_super_resolution_target": "4K",
                "omni_timeout": 99999,
                "omni_max_reference_images": 99,
                "omni_charge_amount_8s": 0.5,
                "omni_upscale_extra_charge": 0.2,
            }
        ])
        parameters = plugin._get_model_parameter_map()["omni-1.1-flash"]
        self.assertEqual(parameters["parameter_mode"], "omni")
        self.assertTrue(parameters["enable_omni_parameters"])
        self.assertEqual(parameters["omni_video_duration"], "6")
        self.assertEqual(parameters["omni_aspect_mode"], "portrait")
        self.assertEqual(parameters["omni_default_aspect"], "landscape")
        self.assertTrue(parameters["omni_super_resolution"])
        self.assertEqual(parameters["omni_super_resolution_target"], "4k")
        self.assertEqual(parameters["omni_timeout"], 3600)
        self.assertEqual(parameters["omni_max_reference_images"], 3)
        self.assertEqual(parameters["omni_charge_amount_8s"], 500)
        self.assertEqual(parameters["omni_upscale_extra_charge"], 200)
        self.assertEqual(parameters["omni_video_duration"], "6")

    def test_model_parameters_defaults(self):
        plugin = self.make_plugin([{"model": "m1", "parameter_mode": "omni"}])
        parameters = plugin._get_model_parameter_map()["m1"]
        self.assertEqual(parameters["omni_video_duration"], "8")
        self.assertEqual(parameters["omni_aspect_mode"], "auto")
        self.assertEqual(parameters["omni_default_aspect"], "portrait")
        self.assertFalse(parameters["omni_super_resolution"])
        self.assertEqual(parameters["omni_super_resolution_target"], "1080p")
        self.assertTrue(parameters["omni_show_timing"])
        self.assertFalse(parameters["omni_send_preview_gif"])
        self.assertEqual(
            parameters["omni_preview_gif_caption"],
            "🔍 超分前预览 · {resolution}，超分完成后发送视频",
        )
        self.assertTrue(parameters["omni_send_orig_video"])
        self.assertEqual(parameters["omni_orig_video_pattern"], r"原视频[:：]\s*(https?://\S+)")
        self.assertIn("超分前原片", parameters["omni_orig_video_caption"])
        self.assertEqual(parameters["omni_timeout"], 2400)
        self.assertEqual(parameters["omni_model_template"], "omni-1.1-flash-{duration}s-{orientation}")

    def test_max_reference_images_clamped(self):
        plugin = self.make_plugin([
            {"model": "m1", "parameter_mode": "omni", "omni_max_reference_images": 99},
            {"model": "m2", "parameter_mode": "omni", "omni_max_reference_images": 0},
            {"model": "m3", "parameter_mode": "omni"},
        ])
        mapping = plugin._get_model_parameter_map()
        self.assertEqual(mapping["m1"]["omni_max_reference_images"], 3)
        self.assertEqual(mapping["m2"]["omni_max_reference_images"], 0)
        self.assertEqual(mapping["m3"]["omni_max_reference_images"], 3)

    # ---- 模型名拼装 --------------------------------------------------
    def test_render_model_name_default_template(self):
        plugin = self.make_plugin()
        parameters = self.omni_parameters()
        self.assertEqual(
            plugin._render_omni_model_name(parameters, "portrait"),
            "omni-1.1-flash-8s-portrait",
        )
        self.assertEqual(
            plugin._render_omni_model_name(parameters, "landscape"),
            "omni-1.1-flash-8s-landscape",
        )

    def test_render_model_name_appends_resolution(self):
        plugin = self.make_plugin()
        parameters = self.omni_parameters(omni_video_duration="4")
        self.assertEqual(
            plugin._render_omni_model_name(parameters, "portrait", "4k"),
            "omni-1.1-flash-4s-portrait-4k",
        )
        self.assertEqual(
            plugin._render_omni_model_name(parameters, "landscape", "1080p"),
            "omni-1.1-flash-4s-landscape-1080p",
        )

    def test_render_model_name_custom_template_with_placeholder(self):
        plugin = self.make_plugin()
        parameters = self.omni_parameters(
            omni_model_template="my-video-{duration}-{orientation}{resolution_suffix}",
            omni_video_duration="10",
        )
        self.assertEqual(
            plugin._render_omni_model_name(parameters, "portrait", "4k"),
            "my-video-10-portrait-4k",
        )
        self.assertEqual(
            plugin._render_omni_model_name(parameters, "portrait", ""),
            "my-video-10-portrait",
        )

    # ---- 首图横竖识别 ------------------------------------------------
    def _install_fake_pil(self, size):
        class _FakeImage:
            def __init__(self, image_size):
                self.size = image_size

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        image_module = types.ModuleType("PIL.Image")
        image_module.open = lambda _data: _FakeImage(size)
        ops_module = types.ModuleType("PIL.ImageOps")
        ops_module.exif_transpose = lambda image: image
        pil_module = types.ModuleType("PIL")
        pil_module.Image = image_module
        pil_module.ImageOps = ops_module
        sys.modules["PIL"] = pil_module
        sys.modules["PIL.Image"] = image_module
        sys.modules["PIL.ImageOps"] = ops_module
        PLUGIN_MODULE.PILImage = image_module

    def test_orientation_forced_modes(self):
        plugin = self.make_plugin()
        self.assertEqual(
            plugin._resolve_omni_orientation(self.omni_parameters(omni_aspect_mode="portrait"), [b"x"]),
            "portrait",
        )
        self.assertEqual(
            plugin._resolve_omni_orientation(self.omni_parameters(omni_aspect_mode="landscape"), []),
            "landscape",
        )

    def test_orientation_defaults_to_portrait_without_image(self):
        plugin = self.make_plugin()
        self.assertEqual(plugin._resolve_omni_orientation(self.omni_parameters(), []), "portrait")
        self.assertEqual(
            plugin._resolve_omni_orientation(
                self.omni_parameters(omni_default_aspect="landscape"), []
            ),
            "landscape",
        )

    def test_orientation_detects_from_first_image(self):
        plugin = self.make_plugin()
        self._install_fake_pil((900, 1600))
        self.assertEqual(
            plugin._resolve_omni_orientation(self.omni_parameters(), [b"portrait-bytes"]),
            "portrait",
        )
        self._install_fake_pil((1920, 1080))
        self.assertEqual(
            plugin._resolve_omni_orientation(self.omni_parameters(), [b"landscape-bytes"]),
            "landscape",
        )
        self._install_fake_pil((1024, 1024))
        self.assertEqual(
            plugin._resolve_omni_orientation(self.omni_parameters(), [b"square-bytes"]),
            "portrait",
        )

    def test_orientation_falls_back_when_decode_fails(self):
        plugin = self.make_plugin()
        # 默认桩：PIL.Image.open 抛错
        PLUGIN_MODULE.PILImage = sys.modules["PIL.Image"]
        self.assertEqual(
            plugin._resolve_omni_orientation(self.omni_parameters(), [b"broken"]),
            "portrait",
        )

    # ---- 计费 --------------------------------------------------------
    def test_charge_amount_by_duration(self):
        plugin = self.make_plugin()
        parameters = self.omni_parameters(
            omni_charge_amount_4s=500,
            omni_charge_amount_8s=2000,
            omni_charge_amount_10s=3000,
            omni_upscale_extra_charge=1000,
        )
        self.assertEqual(plugin._get_omni_charge_amount(parameters, "4"), 500)
        self.assertEqual(plugin._get_omni_charge_amount(parameters, "8"), 2000)
        self.assertEqual(plugin._get_omni_charge_amount(parameters, "10"), 3000)
        self.assertEqual(plugin._get_omni_charge_amount(parameters, "4", upscale=True), 1500)
        # 未配置 6s 档 → 继承 charge_amount (1 元)
        self.assertEqual(plugin._get_omni_charge_amount(parameters, "6"), 1000)

    def test_required_invocation_cost_uses_video_tier(self):
        plugin = self.make_plugin()
        parameters = self.omni_parameters(omni_charge_amount_8s=2500)
        self.assertEqual(
            plugin._get_required_invocation_cost("m", parameters=parameters),
            2500,
        )

    # ---- 文案渲染 ----------------------------------------------------
    def test_render_message_placeholders(self):
        plugin = self.make_plugin()
        text = plugin._render_omni_message(
            "生成{generate_time} 超分{upscale_time} {resolution} {orientation_cn} {duration}s",
            parameters=self.omni_parameters(),
            orientation="portrait",
            duration="8",
            resolution="4k",
            generate_seconds=12.34,
            upscale_seconds=65.0,
        )
        self.assertIn("生成12.3s", text)
        self.assertIn("超分1分5秒", text)
        self.assertIn("4k", text)
        self.assertIn("竖屏", text)
        self.assertIn("8s", text)

    # ---- 视频地址解析 ------------------------------------------------
    def test_extract_video_url(self):
        plugin = self.make_plugin()
        self.assertEqual(
            plugin._extract_video_url_from_text("<video src='https://x/a.mp4'></video>"),
            "https://x/a.mp4",
        )
        self.assertEqual(
            plugin._extract_video_url_from_text('<video controls src="https://x/b.webm">'),
            "https://x/b.webm",
        )
        self.assertEqual(
            plugin._extract_video_url_from_text("看这个 https://x/c.mp4?token=1 哦"),
            "https://x/c.mp4?token=1",
        )
        self.assertIsNone(plugin._extract_video_url_from_text("没有链接"))

    # ---- 请求体 ------------------------------------------------------
    def test_build_payload_text_only(self):
        plugin = self.make_plugin()
        payload = plugin._build_omni_video_payload(
            "omni-1.1-flash-8s-portrait", "一只猫", [], "portrait"
        )
        self.assertEqual(payload["model"], "omni-1.1-flash-8s-portrait")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["messages"][0]["content"], "一只猫")
        self.assertEqual(
            payload["generationConfig"]["imageConfig"]["aspectRatio"], "portrait"
        )
        self.assertNotIn("imageSize", payload["generationConfig"]["imageConfig"])

    def test_build_payload_with_images_and_upscale(self):
        plugin = self.make_plugin()
        payload = plugin._build_omni_video_payload(
            "omni-1.1-flash-8s-portrait-4k", "动起来", [b"\x89PNG\r\n\x1a\nabc"], "portrait", "4k"
        )
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/"))
        self.assertEqual(payload["generationConfig"]["imageConfig"]["imageSize"], "4k")

    # ---- 流式解析与阶段切换 ------------------------------------------
    def _video_plugin(self, response, parameters=None, **conf):
        plugin = self.make_plugin(parameter_entries=None, **conf)
        plugin.iwf = _FakeWorkflow(response)
        plugin._get_api_base_url = lambda: "https://api.example.com"

        async def _key_item(mode, request_context=None, model_name=None):
            return {"key": "sk-test", "api_url": ""}

        plugin._get_api_key_item_for_request = _key_item
        return plugin

    def test_stream_parses_video_and_detects_upscale_phase(self):
        response = _FakeResponse(chunks=[
            _progress("提交视频生成任务...\n"),
            _progress("视频生成中...\n"),
            _progress("\n视频生成完成，开始 4K 放大处理...（可能需要 30 分钟）\n"),
            _progress("预览 GIF: https://cdn.example.com/preview.gif\n"),
            _progress("放大任务已提交，继续轮询...\n"),
            _final("<video src='https://cdn.example.com/out.mp4' controls></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        phases = []

        async def _on_phase(timings):
            phases.append(dict(timings))

        result, status, timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-portrait-4k",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="4k",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
            on_upscale_start=_on_phase,
        ))

        self.assertEqual(status, 200)
        self.assertEqual(result["video_url"], "https://cdn.example.com/out.mp4")
        self.assertEqual(len(phases), 1)
        self.assertTrue(timings["upscale_started"])
        self.assertIsNotNone(timings["generate_seconds"])
        self.assertIsNotNone(timings["upscale_seconds"])
        self.assertGreaterEqual(timings["total_seconds"], 0.0)
        # 超分预览 GIF 取自 reasoning_content
        self.assertEqual(timings["preview_gif"], "https://cdn.example.com/preview.gif")
        self.assertEqual(result["preview_gif"], "https://cdn.example.com/preview.gif")

    def test_progress_text_is_not_treated_as_product(self):
        """过程文字只在 reasoning_content，不能被当成产物（否则会解析不到视频）。"""
        response = _FakeResponse(chunks=[
            _progress("视频生成中...\n"),
            _final("<video src='https://cdn.example.com/p.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        result, _status, timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-portrait",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
        ))
        self.assertEqual(result["video_url"], "https://cdn.example.com/p.mp4")
        self.assertFalse(timings["upscale_started"])

    def test_error_frame_uses_status_code(self):
        response = _FakeResponse(chunks=[
            _progress("视频生成中...\n"),
            _sse({"error": {
                "message": "生成失败: [upscale_unavailable] 超分档不可用",
                "type": "server_error",
                "code": "generation_failed",
                "status_code": 502,
            }}),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        result, status, _timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-portrait-4k",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="4k",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
        ))
        self.assertEqual(status, 502)
        self.assertEqual(result["error_type"], "api_error")
        self.assertEqual(result["provider_code"], "generation_failed")
        self.assertEqual(result["provider_type"], "server_error")
        self.assertIn("upscale_unavailable", result["message"])

    def test_stream_without_upscale_marker(self):
        response = _FakeResponse(chunks=[
            _progress("视频生成中...\n"),
            _final("<video src='https://cdn.example.com/plain.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)

        async def _never(_timings):
            raise AssertionError("不应触发超分阶段回调")

        result, status, timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-portrait",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
            on_upscale_start=_never,
        ))
        self.assertEqual(result["video_url"], "https://cdn.example.com/plain.mp4")
        self.assertFalse(timings["upscale_started"])
        self.assertIsNone(timings["upscale_seconds"])
        self.assertIsNotNone(timings["generate_seconds"])

    def test_stream_error_chunk(self):
        response = _FakeResponse(chunks=[
            _sse({"error": {"message": "上游爆炸了", "code": "boom"}}),
        ])
        plugin = self._video_plugin(response)
        result, status, _timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-portrait",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
        ))
        self.assertEqual(result["error_type"], "api_error")
        self.assertIn("上游爆炸了", result["message"])

    def test_http_error_status(self):
        response = _FakeResponse(status=500, text='{"error":{"message":"boom"}}')
        plugin = self._video_plugin(response)
        result, status, _timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-portrait",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
        ))
        self.assertEqual(status, 500)
        self.assertEqual(result["error_type"], "http_error")

    # ---- 仪表盘字段与保存校验 ----------------------------------------
    def test_dashboard_fields_expose_omni_group(self):
        plugin = self.make_plugin()
        fields = {field["name"]: field for field in plugin._dashboard_parameter_fields()}
        expected = {
            "enable_omni_parameters", "omni_model_template", "omni_video_duration",
            "omni_aspect_mode", "omni_default_aspect", "omni_super_resolution",
            "omni_super_resolution_target", "omni_upscale_start_message",
            "omni_upscale_start_pattern", "omni_result_caption", "omni_show_timing",
            "omni_timeout", "omni_max_reference_images", "omni_charge_amount_4s",
            "omni_charge_amount_6s", "omni_charge_amount_8s", "omni_charge_amount_10s",
            "omni_upscale_extra_charge",
        }
        self.assertTrue(expected.issubset(set(fields)))
        self.assertEqual(fields["omni_video_duration"]["parameter_mode"], "omni")
        self.assertEqual(fields["omni_super_resolution"]["route"], "generic")
        self.assertEqual(fields["omni_super_resolution"]["endpoint_types"], ["chat_completions"])
        self.assertEqual(
            fields["omni_super_resolution"]["depends_on"],
            {"field": "enable_omni_parameters", "equals": True},
        )
        self.assertIn(("omni", "Omni 视频"), FigurineProPlugin.PARAMETER_MODES)

    def test_dashboard_save_normalizes_omni_entry(self):
        plugin = self.make_plugin()
        normalized = plugin._dashboard_normalize_model_parameters(
            [{
                "model": "omni-1.1-flash",
                "parameter_mode": "omni",
                "omni_video_duration": "10",
                "omni_super_resolution": True,
                "omni_super_resolution_target": "4k",
                "omni_charge_amount_10s": 3.5,
            }],
            {"omni-1.1-flash"},
        )
        entry = normalized[0]
        self.assertTrue(entry["enable_omni_parameters"])
        self.assertEqual(entry["omni_video_duration"], "10")
        self.assertTrue(entry["omni_super_resolution"])
        self.assertEqual(entry["omni_super_resolution_target"], "4k")
        self.assertEqual(entry["omni_charge_amount_10s"], 3.5)
        # 其他厂商开关应被强制关闭
        self.assertFalse(entry["enable_seedream_parameters"])

    def test_preview_gif_callback_fires_once(self):
        response = _FakeResponse(chunks=[
            _progress("视频生成中...\n"),
            _progress("预览 GIF: https://cdn.example.com/p1.gif\n"),
            _progress("预览 GIF: https://cdn.example.com/p2.gif\n"),
            _progress("\n视频生成完成，开始 1080P 放大处理...\n"),
            _final("<video src='https://cdn.example.com/out.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        gifs = []

        async def _on_gif(url):
            gifs.append(url)

        result, _status, timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-portrait-1080p",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="1080p",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
            on_preview_gif=_on_gif,
        ))
        # 只在首次出现时回调一次
        self.assertEqual(gifs, ["https://cdn.example.com/p1.gif"])
        self.assertEqual(timings["preview_gif"], "https://cdn.example.com/p1.gif")
        self.assertEqual(result["video_url"], "https://cdn.example.com/out.mp4")

    def test_run_omni_video_sends_preview_gif_as_image(self):
        response = _FakeResponse(chunks=[
            _progress("视频生成中...\n"),
            _progress("预览 GIF: https://cdn.example.com/preview.gif\n"),
            _progress("\n视频生成完成，开始 1080P 放大处理...\n"),
            _final("<video src='https://cdn.example.com/final.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        parameters = self.omni_parameters(
            omni_super_resolution=True,
            omni_super_resolution_target="1080p",
            omni_send_preview_gif=True,
            omni_preview_gif_caption="预览 {resolution}",
        )
        plugin._get_model_parameters = lambda model, parameters=None, image_bytes_list=None: parameters
        plugin._build_final_prompt = lambda prompt, model, count: prompt
        plugin._resolve_generation_access = lambda *a, **k: (True, "user", None)

        async def _settle(**kwargs):
            pass

        plugin._settle_usage_generation = _settle
        event = _FakeEvent()

        async def _collect():
            async for _ in plugin._run_omni_video(
                event=event,
                user_prompt="一只猫在跑",
                assets=[],
                display_label="测试",
                model_in_use="omni-1.1-flash",
                candidate_contexts=[{"actual_model": "omni-1.1-flash", "parameters": parameters}],
                sender_id="u1",
                group_id=None,
            ):
                pass

        asyncio.run(_collect())
        dumped = json.dumps(event.sent, ensure_ascii=False, default=str)
        # 预览 GIF 以图片形式发出，且带自定义文案
        self.assertIn("Image(https://cdn.example.com/preview.gif)", dumped)
        self.assertIn("预览 1080p", dumped)
        # 阶段提示与视频也照常发送（helper 的阶段模板是「开始超分 {resolution}」）
        self.assertIn("开始超分 1080p", dumped)
        self.assertIn("https://cdn.example.com/final.mp4", dumped)
        self.assertEqual(len(event.sent), 3)

    def test_run_omni_video_preview_gif_can_be_disabled(self):
        response = _FakeResponse(chunks=[
            _progress("预览 GIF: https://cdn.example.com/preview.gif\n"),
            _final("<video src='https://cdn.example.com/final.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        parameters = self.omni_parameters(omni_send_preview_gif=False)
        plugin._get_model_parameters = lambda model, parameters=None, image_bytes_list=None: parameters
        plugin._build_final_prompt = lambda prompt, model, count: prompt
        plugin._resolve_generation_access = lambda *a, **k: (True, "user", None)

        async def _settle(**kwargs):
            pass

        plugin._settle_usage_generation = _settle
        event = _FakeEvent()

        async def _collect():
            async for _ in plugin._run_omni_video(
                event=event,
                user_prompt="一只猫在跑",
                assets=[],
                display_label="测试",
                model_in_use="omni-1.1-flash",
                candidate_contexts=[{"actual_model": "omni-1.1-flash", "parameters": parameters}],
                sender_id="u1",
                group_id=None,
            ):
                pass

        asyncio.run(_collect())
        dumped = json.dumps(event.sent, ensure_ascii=False, default=str)
        self.assertNotIn("preview.gif", dumped)

    def test_build_payload_agent_ui(self):
        plugin = self.make_plugin()
        # 默认不传
        p1 = plugin._build_omni_video_payload("m", "x", [], "portrait")
        self.assertNotIn("agent_ui", p1)
        p2 = plugin._build_omni_video_payload("m", "x", [], "portrait", "", "default")
        self.assertNotIn("agent_ui", p2)
        # 显式值才传
        for value in ("on", "off", "keep"):
            p = plugin._build_omni_video_payload("m", "x", [], "portrait", "", value)
            self.assertEqual(p["agent_ui"], value)

    def test_agent_ui_normalization(self):
        plugin = self.make_plugin([
            {"model": "m1", "parameter_mode": "omni", "omni_agent_ui": "开"},
            {"model": "m2", "parameter_mode": "omni", "omni_agent_ui": "OFF"},
            {"model": "m3", "parameter_mode": "omni"},
            {"model": "m4", "parameter_mode": "omni", "omni_agent_ui": "bogus"},
        ])
        mapping = plugin._get_model_parameter_map()
        self.assertEqual(mapping["m1"]["omni_agent_ui"], "on")
        self.assertEqual(mapping["m2"]["omni_agent_ui"], "off")
        self.assertEqual(mapping["m3"]["omni_agent_ui"], "default")
        self.assertEqual(mapping["m4"]["omni_agent_ui"], "default")

    def test_video_sent_as_sole_segment(self):
        """QQ 要求 video 段独占消息：文案单独一条，视频单独一条。"""
        plugin = self.make_plugin()

        class _FakeVideo:
            def __init__(self, file=None):
                self.file = file

            @classmethod
            def fromURL(cls, url, **kwargs):
                return cls(file=url)

            @classmethod
            def fromFileSystem(cls, path, **kwargs):
                return cls(file=path)

            def __repr__(self):
                return f"Video({self.file})"

        old_video = PLUGIN_MODULE.Video
        PLUGIN_MODULE.Video = _FakeVideo
        try:
            event = _FakeEvent()
            ok, saved = asyncio.run(
                plugin._send_video_with_retry(event, "https://cdn.example.com/a.mp4", "完成文案")
            )
            self.assertTrue(ok)
            self.assertIsNone(saved)
            self.assertEqual(len(event.sent), 2)
            # 第一条是文案
            self.assertIn("完成文案", json.dumps(event.sent[0], ensure_ascii=False, default=str))
            # 第二条只有视频段（没有 Plain）
            video_chain = event.sent[1]
            self.assertEqual(len(video_chain), 1)
            self.assertIsInstance(video_chain[0], _FakeVideo)
            self.assertEqual(video_chain[0].file, "https://cdn.example.com/a.mp4")
        finally:
            PLUGIN_MODULE.Video = old_video

    def test_video_falls_back_to_link_when_platform_rejects(self):
        plugin = self.make_plugin()

        class _RejectingVideo:
            def __init__(self, file=None):
                self.file = file

            @classmethod
            def fromURL(cls, url, **kwargs):
                return cls(file=url)

            @classmethod
            def fromFileSystem(cls, path, **kwargs):
                return cls(file=path)

        class _RejectingEvent(_FakeEvent):
            async def send(self, result):
                dumped = json.dumps(result, ensure_ascii=False, default=str)
                if "_RejectingVideo" in dumped or "a.mp4" in dumped and "链接" not in dumped:
                    raise RuntimeError('message element "video" must be the only segment in a message')
                self.sent.append(result)

        old_video = PLUGIN_MODULE.Video
        PLUGIN_MODULE.Video = _RejectingVideo
        try:
            event = _RejectingEvent()
            ok, _saved = asyncio.run(
                plugin._send_video_with_retry(event, "https://cdn.example.com/a.mp4", "完成文案")
            )
            self.assertTrue(ok)
            dumped = json.dumps(event.sent, ensure_ascii=False, default=str)
            self.assertIn("视频链接", dumped)
        finally:
            PLUGIN_MODULE.Video = old_video

    def test_orig_video_callback_fires_once(self):
        response = _FakeResponse(chunks=[
            _progress("视频生成中...\n"),
            _progress("\n视频生成完成，开始 1080P 放大处理...\n"),
            _progress("原视频: http://cdn.example.com/base720.mp4\n"),
            _progress("原视频: http://cdn.example.com/base720-dup.mp4\n"),
            _final("<video src='https://cdn.example.com/final1080.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        seen = []

        async def _on_orig(url):
            seen.append(url)

        result, _status, timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-8s-landscape-1080p",
            prompt="一只猫",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="landscape",
            resolution="1080p",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
            orig_video_pattern=r"原视频[:：]\s*(https?://\S+)",
            on_orig_video=_on_orig,
        ))
        self.assertEqual(seen, ["http://cdn.example.com/base720.mp4"])
        self.assertEqual(timings["orig_video_url"], "http://cdn.example.com/base720.mp4")
        # 终态仍是超分后的视频，不受影响
        self.assertEqual(result["video_url"], "https://cdn.example.com/final1080.mp4")

    def test_orig_video_custom_pattern(self):
        response = _FakeResponse(chunks=[
            _progress("BASE-FILE => https://cdn.example.com/x.mp4\n"),
            _final("<video src='https://cdn.example.com/f.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        seen = []

        async def _on_orig(url):
            seen.append(url)

        _r, _s, timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="m",
            prompt="x",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="portrait",
            resolution="1080p",
            timeout=900,
            upscale_pattern=r"视频生成完成.*?放大",
            orig_video_pattern=r"BASE-FILE\s*=>\s*(https?://\S+)",
            on_orig_video=_on_orig,
        ))
        self.assertEqual(seen, ["https://cdn.example.com/x.mp4"])
        self.assertEqual(timings["orig_video_url"], "https://cdn.example.com/x.mp4")

    def test_run_omni_video_sends_orig_video_as_video(self):
        response = _FakeResponse(chunks=[
            _progress("\n视频生成完成，开始 1080P 放大处理...\n"),
            _progress("原视频: http://cdn.example.com/base720.mp4\n"),
            _final("<video src='https://cdn.example.com/final1080.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        parameters = self.omni_parameters(
            omni_super_resolution=True,
            omni_super_resolution_target="1080p",
            omni_send_orig_video=True,
            omni_orig_video_caption="原片 {resolution}",
        )

        class _FakeVideo:
            def __init__(self, file=None):
                self.file = file

            @classmethod
            def fromURL(cls, url, **kwargs):
                return cls(file=url)

            @classmethod
            def fromFileSystem(cls, path, **kwargs):
                return cls(file=path)

            def __repr__(self):
                return f"Video({self.file})"

        old_video = PLUGIN_MODULE.Video
        PLUGIN_MODULE.Video = _FakeVideo
        try:
            plugin._build_final_prompt = lambda prompt, model, count: prompt
            plugin._resolve_generation_access = lambda *a, **k: (True, "user", None)

            async def _settle(**kwargs):
                pass

            plugin._settle_usage_generation = _settle
            event = _FakeEvent()

            async def _collect():
                async for _ in plugin._run_omni_video(
                    event=event,
                    user_prompt="一只猫在跑",
                    assets=[],
                    display_label="测试",
                    model_in_use="omni-1.1-flash",
                    candidate_contexts=[{"actual_model": "omni-1.1-flash", "parameters": parameters}],
                    sender_id="u1",
                    group_id=None,
                ):
                    pass

            asyncio.run(_collect())
        finally:
            PLUGIN_MODULE.Video = old_video

        dumped = json.dumps(event.sent, ensure_ascii=False, default=str)
        self.assertIn("http://cdn.example.com/base720.mp4", dumped)
        self.assertIn("原片 1080p", dumped)
        self.assertIn("https://cdn.example.com/final1080.mp4", dumped)

    def test_run_omni_video_orig_video_can_be_disabled(self):
        response = _FakeResponse(chunks=[
            _progress("原视频: http://cdn.example.com/base720.mp4\n"),
            _final("<video src='https://cdn.example.com/f.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        parameters = self.omni_parameters(omni_send_orig_video=False)
        plugin._build_final_prompt = lambda prompt, model, count: prompt
        plugin._resolve_generation_access = lambda *a, **k: (True, "user", None)

        async def _settle(**kwargs):
            pass

        plugin._settle_usage_generation = _settle
        event = _FakeEvent()

        async def _collect():
            async for _ in plugin._run_omni_video(
                event=event,
                user_prompt="一只猫在跑",
                assets=[],
                display_label="测试",
                model_in_use="omni-1.1-flash",
                candidate_contexts=[{"actual_model": "omni-1.1-flash", "parameters": parameters}],
                sender_id="u1",
                group_id=None,
            ):
                pass

        asyncio.run(_collect())
        dumped = json.dumps(event.sent, ensure_ascii=False, default=str)
        self.assertNotIn("base720.mp4", dumped)

    def test_orig_video_real_upstream_wording_20261009(self):
        """回归：2026-10-09 现网真实流措辞（阶段标记 + 原视频行）。"""
        response = _FakeResponse(chunks=[
            _progress("✨ 视频生成任务已启动\n"),
            _progress("提交视频生成任务...\n"),
            _progress("视频生成中...\n"),
            _progress("生成进度: 0%\n"),
            _progress("视频生成完成，正在提交 1080P 放大任务...\n"),
            _progress("原视频: http://10.10.10.99:38000/tmp/f47917a93143e4297d3e6a86d1af3511.mp4\n"),
            _progress("放大任务已提交，继续轮询...\n"),
            _progress("✅ 1080P 放大已完成，返回超分后的视频\n"),
            _final("<video src='http://10.10.10.99:38000/tmp/c1b6b4ae3c3a8126c8860359553fb706.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        seen = []
        phases = []

        async def _on_orig(url):
            seen.append(url)

        async def _on_phase(timings):
            phases.append(dict(timings))

        result, _s, timings = asyncio.run(plugin._call_omni_video_once(
            model_name="omni-1.1-flash",
            request_model_name="omni-1.1-flash-4s-landscape-1080p",
            prompt="一只橘猫在窗台上伸懒腰",
            assets=[],
            request_context={"api_route": "generic", "parameters": self.omni_parameters()},
            orientation="landscape",
            resolution="1080p",
            timeout=900,
            upscale_pattern=PLUGIN_MODULE.FigurineProPlugin.OMNI_UPSCALE_START_PATTERN_DEFAULT,
            on_upscale_start=_on_phase,
            on_orig_video=_on_orig,
        ))
        # 默认阶段正则仍能匹配新措辞
        self.assertEqual(len(phases), 1)
        # 原视频行被正确提取
        self.assertEqual(
            seen,
            ["http://10.10.10.99:38000/tmp/f47917a93143e4297d3e6a86d1af3511.mp4"],
        )
        self.assertEqual(timings["orig_video_url"], seen[0])
        # 终态不受影响
        self.assertEqual(
            result["video_url"],
            "http://10.10.10.99:38000/tmp/c1b6b4ae3c3a8126c8860359553fb706.mp4",
        )

    def test_video_message_must_not_contain_reply_segment(self):
        """回归：QQ 要求 video 段独占消息 —— 视频消息里**不能**有「引用回复」段。

        线上故障(2026-10-09): `_reply_chain_result` 会给每条消息前置 Reply，
        于是视频消息成了 `[Reply, Video]`，被 QQ 以
        `retcode=1400 message element "video" must be the only segment` 拒收。
        """
        plugin = self.make_plugin()

        class _FakeVideo:
            def __init__(self, file=None):
                self.file = file

            @classmethod
            def fromURL(cls, url, **kwargs):
                return cls(file=url)

            @classmethod
            def fromFileSystem(cls, path, **kwargs):
                return cls(file=path)

            def __repr__(self):
                return f"Video({self.file})"

        old_video = PLUGIN_MODULE.Video
        PLUGIN_MODULE.Video = _FakeVideo
        try:
            event = _FakeEvent()
            # 关键：让事件带上 message_id，_build_reply_chain 才会返回 Reply
            event.message_obj = types.SimpleNamespace(message_id="12345")
            ok, _saved = asyncio.run(
                plugin._send_video_with_retry(event, "https://cdn.example.com/a.mp4", "完成文案")
            )
            self.assertTrue(ok)
            self.assertEqual(len(event.sent), 2)

            text_chain, video_chain = event.sent[0], event.sent[1]
            # 文案那条照旧带引用回复
            self.assertTrue(any(isinstance(seg, PLUGIN_MODULE.Reply) for seg in text_chain))
            # 视频那条必须**只有** video 段
            self.assertEqual(len(video_chain), 1, f"视频消息不能有多余段: {video_chain}")
            self.assertIsInstance(video_chain[0], _FakeVideo)
            self.assertFalse(
                any(isinstance(seg, PLUGIN_MODULE.Reply) for seg in video_chain),
                "视频消息里出现了引用回复段，QQ 会拒收",
            )
            # 反证：走通用链确实会多出 Reply 段（这正是线上故障的成因）
            legacy = plugin._reply_chain_result(event, [_FakeVideo(file="x")])
            self.assertEqual(len(legacy), 2)
        finally:
            PLUGIN_MODULE.Video = old_video

    # ---- 视频素材收集（引用链优先） ----------------------------------

    def test_get_videos_reply_chain_first(self):
        """视频素材顺序：引用链排最前，其次当前消息（与图片语义一致）。"""
        import base64 as _b64

        workflow = PLUGIN_MODULE.FigurineProPlugin.ImageWorkflow()

        class _FakeVideo:
            def __init__(self, url=None, file=None, name=None):
                self.url = url
                self.file = file
                self.name = name

            def __repr__(self):
                return f"Video({self.url or self.file})"

        def _reply_with(chain):
            # 必须是插件真正 isinstance 检查的 Reply 类（否则引用链不会被遍历）
            seg = PLUGIN_MODULE.Reply(id="1")
            seg.chain = chain
            return seg

        def _data(payload: bytes) -> str:
            return "data:video/mp4;base64," + _b64.b64encode(payload).decode()

        reply_video = _FakeVideo(url=_data(b"REPLYVID"), name="r.mp4")
        current_video = _FakeVideo(url=_data(b"CURRENTVID"), name="c.mp4")

        event = _FakeEvent()
        event.message_obj = types.SimpleNamespace(
            message=[_reply_with([reply_video]), current_video]
        )

        old_video = PLUGIN_MODULE.Video
        PLUGIN_MODULE.Video = _FakeVideo
        try:
            videos = asyncio.run(workflow.get_videos(event))
        finally:
            PLUGIN_MODULE.Video = old_video

        self.assertEqual([v["source"] for v in videos], ["reply", "current"])
        self.assertEqual(videos[0]["bytes"], b"REPLYVID")
        self.assertEqual(videos[1]["bytes"], b"CURRENTVID")
        self.assertEqual(videos[0]["filename"], "r.mp4")
        self.assertEqual(videos[0]["size"], len(b"REPLYVID"))

    def test_get_videos_returns_empty_without_video_component(self):
        """旧版 AstrBot 没有 Video 组件时，安全返回空。"""
        workflow = PLUGIN_MODULE.FigurineProPlugin.ImageWorkflow()
        event = _FakeEvent()
        event.message_obj = types.SimpleNamespace(message=[object()])

        old_video = PLUGIN_MODULE.Video
        PLUGIN_MODULE.Video = None
        try:
            self.assertEqual(asyncio.run(workflow.get_videos(event)), [])
        finally:
            PLUGIN_MODULE.Video = old_video

    def test_get_videos_skips_unloadable_segment(self):
        """取不到地址 / 下载失败的视频段被跳过，不产生错位。"""
        import base64 as _b64

        workflow = PLUGIN_MODULE.FigurineProPlugin.ImageWorkflow()

        class _FakeVideo:
            def __init__(self, url=None, file=None, name=None):
                self.url = url
                self.file = file
                self.name = name

        def _reply_with(chain):
            # 必须是插件真正 isinstance 检查的 Reply 类（否则引用链不会被遍历）
            seg = PLUGIN_MODULE.Reply(id="1")
            seg.chain = chain
            return seg

        bad = _FakeVideo(url="", file="")
        good = _FakeVideo(
            url="data:video/mp4;base64," + _b64.b64encode(b"OK").decode(), name="g.mp4"
        )
        event = _FakeEvent()
        event.message_obj = types.SimpleNamespace(message=[_reply_with([bad]), good])

        old_video = PLUGIN_MODULE.Video
        PLUGIN_MODULE.Video = _FakeVideo
        try:
            videos = asyncio.run(workflow.get_videos(event))
        finally:
            PLUGIN_MODULE.Video = old_video

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["source"], "current")
        self.assertEqual(videos[0]["bytes"], b"OK")

    # ---- 有序素材（图+视频统一保序） --------------------------------

    def test_build_payload_ordered_assets(self):
        """图与视频统一按 assets 顺序挂载；视频走 video_url。"""
        plugin = self.make_plugin()
        assets = [
            {"kind": "image", "bytes": b"\x89PNG\r\n\x1a\n" + b"x" * 8},
            {"kind": "video", "bytes": b"\x00\x00\x00\x20ftypisom", "url": "upload://abc"},
            {"kind": "image", "bytes": b"\xff\xd8\xff\xe0" + b"y" * 8},
        ]
        payload = plugin._build_omni_video_payload("m", "提示词", assets, "portrait")
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "提示词"})
        self.assertEqual(
            [c["type"] for c in content[1:]], ["image_url", "video_url", "image_url"]
        )
        self.assertEqual(content[2]["video_url"]["url"], "upload://abc")
        payload2 = plugin._build_omni_video_payload(
            "m", "x", [{"kind": "video", "bytes": b"\x00\x00\x00\x20ftypisom"}], "portrait"
        )
        self.assertTrue(
            payload2["messages"][0]["content"][1]["video_url"]["url"].startswith(
                "data:video/mp4;base64,"
            )
        )
        self.assertEqual(
            plugin._build_omni_video_payload("m", "只有文字", [], "portrait")["messages"][0]["content"],
            "只有文字",
        )

    def test_video_bytes_to_data_url_mime(self):
        plugin = self.make_plugin()
        cases = [
            (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8, "data:video/mp4;base64,"),
            (b"\x1a\x45\xdf\xa3" + b"\x00" * 8, "data:video/webm;base64,"),
            (b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 4, "data:video/avi;base64,"),
        ]
        for data, prefix in cases:
            self.assertTrue(plugin._video_bytes_to_data_url(data).startswith(prefix))

    def test_resolve_video_assets_inline_and_upload_fallback(self):
        plugin = self.make_plugin()
        plugin.OMNI_VIDEO_INLINE_LIMIT = 8
        small = {"kind": "video", "bytes": b"\x00\x00\x00\x20ftyp", "filename": "a.mp4"}
        resolved = asyncio.run(plugin._resolve_video_assets([small], "http://x", "k"))
        self.assertTrue(resolved[0]["url"].startswith("data:video/mp4;base64,"))
        big = {"kind": "video", "bytes": b"\x00" * 9, "filename": "b.mp4"}
        plugin.iwf = None
        self.assertEqual(asyncio.run(plugin._resolve_video_assets([big], "http://x", "k")), [])

    def test_get_assets_ordered_interleaves_image_and_video(self):
        """跨类型保序：引用链 [图, 视频] → 当前消息 [图]。"""
        import base64 as _b64

        workflow = PLUGIN_MODULE.FigurineProPlugin.ImageWorkflow()

        class _FakeImage:
            def __init__(self, url):
                self.url = url
                self.file = None

        class _FakeVideo:
            def __init__(self, url):
                self.url = url
                self.file = None
                self.name = "v.mp4"

        def _reply_with(chain):
            seg = PLUGIN_MODULE.Reply(id="1")
            seg.chain = chain
            return seg

        img = _FakeImage("data:image/png;base64," + _b64.b64encode(b"IMG").decode())
        vid = _FakeVideo("data:video/mp4;base64," + _b64.b64encode(b"VID").decode())
        cur_img = _FakeImage("data:image/png;base64," + _b64.b64encode(b"CURIMG").decode())

        event = _FakeEvent()
        event.message_obj = types.SimpleNamespace(message=[_reply_with([img, vid]), cur_img])

        old_img, old_vid = PLUGIN_MODULE.Image, PLUGIN_MODULE.Video
        PLUGIN_MODULE.Image = _FakeImage
        PLUGIN_MODULE.Video = _FakeVideo
        try:
            assets = asyncio.run(workflow.get_assets_ordered(event))
        finally:
            PLUGIN_MODULE.Image = old_img
            PLUGIN_MODULE.Video = old_vid

        self.assertEqual([a["kind"] for a in assets], ["image", "video", "image"])
        self.assertEqual([a["source"] for a in assets], ["reply", "reply", "current"])
        self.assertEqual(assets[0]["bytes"], b"IMG")
        self.assertEqual(assets[1]["bytes"], b"VID")
        self.assertEqual(assets[2]["bytes"], b"CURIMG")

    def test_get_assets_ordered_picks_up_file_segment_video(self):
        """NapCat 把「引用的视频」下发成 File 段 —— 必须识别为视频。"""
        import os
        import tempfile

        workflow = PLUGIN_MODULE.FigurineProPlugin.ImageWorkflow()

        class _FakeFile:
            def __init__(self, name, path):
                self.name = name
                self.file_ = path
                self.url = ""

            async def get_file(self, allow_return_url=False):
                return self.file_

        def _reply_with(chain):
            seg = PLUGIN_MODULE.Reply(id="1")
            seg.chain = chain
            return seg

        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8)
        tmp.close()
        tmp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_pdf.write(b"%PDF-1.4 not a video")
        tmp_pdf.close()

        try:
            event = _FakeEvent()
            event.message_obj = types.SimpleNamespace(
                message=[_reply_with([_FakeFile("clip.mp4", tmp.name), _FakeFile("doc.pdf", tmp_pdf.name)])]
            )
            old_file = PLUGIN_MODULE.FileSegment
            PLUGIN_MODULE.FileSegment = _FakeFile
            try:
                assets = asyncio.run(workflow.get_assets_ordered(event))
            finally:
                PLUGIN_MODULE.FileSegment = old_file

            self.assertEqual(len(assets), 1, assets)
            self.assertEqual(assets[0]["kind"], "video")
            self.assertEqual(assets[0]["source"], "reply")
            self.assertEqual(assets[0]["filename"], "clip.mp4")
        finally:
            os.unlink(tmp.name)
            os.unlink(tmp_pdf.name)

    # ---- 视频分支端到端（发送与结算） --------------------------------
    def test_run_omni_video_sends_phase_and_video(self):
        response = _FakeResponse(chunks=[
            _progress("视频生成中...\n"),
            _progress("\n视频生成完成，开始 1080P 放大处理...\n"),
            _final("<video src='https://cdn.example.com/final.mp4'></video>"),
            b"data: [DONE]\n\n",
        ])
        plugin = self._video_plugin(response)
        parameters = self.omni_parameters(
            omni_super_resolution=True,
            omni_super_resolution_target="1080p",
            omni_upscale_start_message="开始超分 {resolution}（生成耗时 {generate_time}）",
            omni_charge_amount_8s=2000,
        )
        plugin._get_model_parameters = lambda model, parameters=None, image_bytes_list=None: parameters
        plugin._build_final_prompt = lambda prompt, model, count: prompt
        plugin._resolve_generation_access = lambda *a, **k: (True, "user", None)
        settled = []

        async def _settle(**kwargs):
            settled.append(kwargs)

        plugin._settle_usage_generation = _settle

        event = _FakeEvent()
        context = {"actual_model": "omni-1.1-flash", "parameters": parameters}

        async def _collect():
            items = []
            async for item in plugin._run_omni_video(
                event=event,
                user_prompt="一只猫在跑",
                assets=[],
                display_label="测试",
                model_in_use="omni-1.1-flash",
                candidate_contexts=[context],
                sender_id="u1",
                group_id=None,
            ):
                items.append(item)
            return items

        items = asyncio.run(_collect())

        # 1 条开始信息 + 1 条失败兜底/无（成功时只有开始信息被 yield）
        self.assertTrue(items)
        # 阶段提示通过 event.send 直接发出
        self.assertEqual(len(event.sent), 2)
        phase_payload = event.sent[0]
        phase_text = json.dumps(phase_payload, ensure_ascii=False, default=str)
        self.assertIn("开始超分 1080p", phase_text)
        video_payload = json.dumps(event.sent[1], ensure_ascii=False, default=str)
        self.assertIn("https://cdn.example.com/final.mp4", video_payload)
        # 结算
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["outcome"], "success")
        self.assertEqual(settled[0]["charged_amount"], 2000)
        self.assertIn("1080p", settled[0]["note"])

    def test_run_omni_video_denied_by_balance(self):
        plugin = self._video_plugin(_FakeResponse())
        plugin._resolve_generation_access = lambda *a, **k: (False, None, "❌ 余额不足")
        event = _FakeEvent()

        async def _collect():
            items = []
            async for item in plugin._run_omni_video(
                event=event,
                user_prompt="一只猫",
                assets=[],
                display_label="测试",
                model_in_use="omni-1.1-flash",
                candidate_contexts=[{"actual_model": "omni-1.1-flash", "parameters": self.omni_parameters()}],
                sender_id="u1",
                group_id=None,
            ):
                items.append(item)
            return items

        items = asyncio.run(_collect())
        self.assertEqual(len(items), 1)
        self.assertIn("余额不足", json.dumps(items[0], ensure_ascii=False, default=str))


if __name__ == "__main__":
    unittest.main()
