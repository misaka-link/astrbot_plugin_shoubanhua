import asyncio
import base64
import copy
import functools
import hashlib
import html
import io
import json
import logging
import math
import random
import re
import unicodedata
from dataclasses import field as dataclass_field
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Dict, Any, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import aiohttp
from PIL import Image as PILImage

from astrbot import logger as astrbot_host_logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Reply, Plain, Node, Nodes
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .usage_store import (
    LEGACY_COUNT_TO_YUAN,
    UsageStore,
    amount_to_yuan,
    format_amount,
    yuan_to_amount,
)

PLUGIN_LOGGER_NAME = "astrbot.plugin.astrbot_plugin_shoubanhua"
PLUGIN_LOG_HANDLER_MARKER = "astrbot_plugin_shoubanhua_file_handler"


class _PluginLogProxy:
    """Forward plugin records to AstrBot and the plugin-specific logger."""

    def __getattr__(self, name: str) -> Any:
        return getattr(astrbot_host_logger, name)

    def _emit(self, level: str, message: Any, *args: Any, **kwargs: Any) -> None:
        host_method = getattr(astrbot_host_logger, level)
        host_method(message, *args, **kwargs)
        plugin_logger = logging.getLogger(PLUGIN_LOGGER_NAME)
        log_method = getattr(plugin_logger, level)
        if args:
            log_method(message, *args, **kwargs)
        else:
            log_method(str(message), **kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._emit("info", message, *args, **kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._emit("warning", message, *args, **kwargs)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._emit("error", message, *args, **kwargs)


logger = _PluginLogProxy()


def _plugin_logger(data_dir: Path):
    """Return the plugin logger and install one reload-safe local file sink."""
    plugin_logger = logging.getLogger(PLUGIN_LOGGER_NAME)
    plugin_logger.setLevel(logging.INFO)
    plugin_logger.propagate = False
    log_path = data_dir / "logs" / "figurine_pro.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path = log_path.resolve()
        for handler in list(plugin_logger.handlers):
            if not getattr(handler, PLUGIN_LOG_HANDLER_MARKER, False):
                continue
            if Path(getattr(handler, "baseFilename", "")).resolve() == resolved_path:
                return plugin_logger
            plugin_logger.removeHandler(handler)
            handler.close()
        handler = TimedRotatingFileHandler(
            resolved_path,
            when="midnight",
            backupCount=30,
            encoding="utf-8",
            delay=True,
        )
        handler.suffix = "%Y-%m-%d"
        setattr(handler, PLUGIN_LOG_HANDLER_MARKER, True)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            "%Y-%m-%d %H:%M:%S",
        ))
        plugin_logger.addHandler(handler)
    except OSError as exc:
        logger.warning(f"初始化插件独立日志文件失败: {exc}")
    return plugin_logger


try:
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except ImportError:
    error_response = json_response = request = None
    WEB_API_AVAILABLE = False

try:
    from pydantic import Field
    from pydantic.dataclasses import dataclass as pydantic_dataclass
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext

    LLM_TOOL_API_AVAILABLE = True
except ImportError:
    Field = None
    FunctionTool = None
    ContextWrapper = Any
    AstrAgentContext = Any
    LLM_TOOL_API_AVAILABLE = False


if LLM_TOOL_API_AVAILABLE:
    @pydantic_dataclass
    class TextToImageTool(FunctionTool[AstrAgentContext]):
        plugin: Any = dataclass_field(default=None, repr=False, compare=False)
        name: str = "generate_text_to_image"
        description: str = (
            "Generate a new image from a text prompt. Use aspect_ratio when the user explicitly "
            "requests a composition such as a square, landscape, portrait, poster, or wallpaper."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed description of the image to generate.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model from the configured LLM image generation model list. "
                                   "Normally omit this parameter to use that list's default model.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Optional output aspect ratio, such as 1:1, 16:9, 9:16, 4:3, or 3:4. Use it when the user specifies the composition.",
                },
                "batch_count": {
                    "type": "number",
                    "description": "Optional number of images to generate. Leave empty for one image.",
                },
            },
            "required": ["prompt"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
            return await self.plugin._run_llm_image_tool(
                context.context.event,
                prompt=kwargs.get("prompt"),
                model=kwargs.get("model"),
                aspect_ratio=kwargs.get("aspect_ratio"),
                batch_count=kwargs.get("batch_count"),
            )


    @pydantic_dataclass
    class ImageToImageTool(FunctionTool[AstrAgentContext]):
        plugin: Any = dataclass_field(default=None, repr=False, compare=False)
        name: str = "generate_image_to_image"
        description: str = (
            "Transform one or more reference images according to a text prompt. Prefer reference_images "
            "when image URLs are available; otherwise the tool uses images from the current or replied message. "
            "Use aspect_ratio when the user explicitly requests a composition."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Instructions for transforming the reference image.",
                },
                "reference_images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional public image URLs, data URLs, or base64:// image strings. Do not pass local file paths.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model from the configured LLM image generation model list. "
                                   "Normally omit this parameter to use that list's default model.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Optional output aspect ratio, such as 1:1, 16:9, 9:16, 4:3, or 3:4. Use it when the user specifies the composition.",
                },
                "batch_count": {
                    "type": "number",
                    "description": "Optional number of images to generate. Leave empty for one image.",
                },
            },
            "required": ["prompt"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
            return await self.plugin._run_llm_image_tool(
                context.context.event,
                prompt=kwargs.get("prompt"),
                reference_images=kwargs.get("reference_images"),
                model=kwargs.get("model"),
                aspect_ratio=kwargs.get("aspect_ratio"),
                batch_count=kwargs.get("batch_count"),
                require_images=True,
            )


def _normalize_timeout(value: Any, default: int = 120, minimum: int = 5) -> int:
    """Normalize timeout values from config/user input."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = default
    return max(minimum, seconds)


def _normalize_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, number)


def _normalize_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(0, number)


def _normalize_charge_amount(value: Any, default_yuan: float) -> int:
    """金额配置（元）→ 厘；无效或为 0 时回退默认值。"""
    amount = yuan_to_amount(value)
    return amount if amount > 0 else yuan_to_amount(default_yuan)


def _build_client_timeout(connect_seconds: int, read_seconds: int | None = None) -> aiohttp.ClientTimeout:
    connect_seconds = max(1, connect_seconds)
    if read_seconds is None:
        read_seconds = connect_seconds
    read_seconds = max(connect_seconds, read_seconds)
    return aiohttp.ClientTimeout(
        total=None,
        connect=connect_seconds,
        sock_connect=connect_seconds,
        sock_read=read_seconds,
    )


@register(
    "astrbot_plugin_shoubanhua",
    "shskjw",
    "支持第三方 OpenAI 绘图格式、Gemini 路由和 Seedream 专属图片参数的文生图/图生图插件，按金额（元，精确到 0.001）计费",
    "2.0.0",
    "https://github.com/misaka-link/astrbot_plugin_shoubanhua",
)
class FigurineProPlugin(Star):
    RESERVED_COMMAND_NAMES = frozenset({
        "切换模型", "SwitchModel", "模型列表", "文生图",
        "手办化预设增加", "手办化预设查看",
        "预设图片清理", "预设图片统计", "手办化今日统计", "手办化签到",
        "手办化增加用户余额", "手办化增加群组余额", "手办化查询余额",
    })

    GENERIC_ENDPOINT_PATHS = {
        "chat_completions": "/v1/chat/completions",
        "images_generations": "/v1/images/generations",
        "images_edits": "/v1/images/edits",
    }
    DEFAULT_GENERIC_API_URL = "https://api.bltcy.ai/v1/chat/completions"
    DEFAULT_CONTENT_POLICY_WARNING_MESSAGE = (
        "您的请求包含违规内容，无法生成图片。\n"
        "请更换模型或提示词/参考图 尝试 ！！\n"
        "模型: {model}\n\n"
        "任务 {batch_index}/{batch_count}"
    )

    GENERIC_ENDPOINT_DISPLAY_NAMES = {
        "chat_completions": "chat",
        "images_generations": "generations",
        "images_edits": "edits",
    }

    GENERIC_ENDPOINT_MODEL_LIST_KEYS = {
        "chat_completions": "chat_completions_model_list",
        "images_generations": "images_generations_model_list",
        "images_edits": "images_edits_model_list",
    }

    PARAMETER_MODES = (
        ("none", "无厂商参数"),
        ("gpt", "GPT"),
        ("gemini", "Gemini"),
        ("grok", "Grok"),
        ("seedream", "Seedream"),
    )
    PARAMETER_MODE_ENABLE_FIELDS = {
        "gpt": "enable_gpt_parameters",
        "gemini": "enable_gemini_parameters",
        "grok": "enable_grok_parameters",
        "seedream": "enable_seedream_parameters",
    }

    IMAGE_QUALITY_OPTIONS = {"low", "medium", "high", "auto"}
    IMAGE_MODERATION_OPTIONS = {"auto", "low"}
    GROK_RESOLUTION_OPTIONS = {"1k", "2k"}
    GROK_ASPECT_RATIO_ORDER = (
        "1:2", "9:20", "9:19.5", "9:16", "2:3", "3:4", "1:1",
        "4:3", "3:2", "16:9", "19.5:9", "20:9", "2:1",
    )
    SEEDREAM_RESOLUTION_OPTIONS = {"1K", "1.5K", "2K"}
    SEEDREAM_RESOLUTION_PIXELS = {
        "1K": 1_048_576,
        "1.5K": 2_359_296,
        "2K": 4_194_304,
    }
    SEEDREAM_RESOLUTION_ORDER = ("1K", "1.5K", "2K")
    SEEDREAM_ADAPTIVE_MAX_SIDE = 2000
    SEEDREAM_SIZE_OPTIONS = {
        "1K": {
            "1:1": "1024x1024",
            "4:3": "1152x864",
            "3:4": "864x1152",
            "16:9": "1424x800",
            "9:16": "800x1424",
            "3:2": "1248x832",
            "2:3": "832x1248",
            "21:9": "1568x672",
        },
        "1.5K": {
            "1:1": "1536x1536",
            "4:3": "1792x1344",
            "3:4": "1344x1792",
            "16:9": "2048x1152",
            "9:16": "1152x2048",
            "3:2": "1872x1248",
            "2:3": "1248x1872",
            "21:9": "2352x1008",
        },
        "2K": {
            "1:1": "2048x2048",
            "4:3": "2368x1776",
            "3:4": "1776x2368",
            "16:9": "2816x1584",
            "9:16": "1584x2816",
            "3:2": "2496x1664",
            "2:3": "1664x2496",
            "21:9": "3136x1344",
        },
    }
    SEEDREAM_ASPECT_RATIO_ORDER = (
        "1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9",
    )
    ADAPTIVE_RESOLUTION_LONG_EDGES = {
        "1K": 1024,
        "2K": 2048,
        "4K": 3840,
    }
    GEMINI_ASPECT_RATIO_ORDER = (
        "1:8", "1:4", "9:16", "2:3", "3:4", "4:5", "1:1",
        "5:4", "4:3", "3:2", "16:9", "21:9", "4:1", "8:1",
    )
    GEMINI_ASPECT_RATIO_OPTIONS = {
        "1:1", "1:4", "4:1", "1:8", "8:1", "2:3", "3:2", "3:4",
        "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
    }
    ADAPTIVE_SIZE_ALIGNMENT = 16
    ADAPTIVE_MIN_PIXELS = 655_360
    ADAPTIVE_MAX_PIXELS = 8_294_400
    ADAPTIVE_MAX_ASPECT_RATIO = 3.0
    DEFAULT_REQUEST_USER_AGENT = (
        "Codex Desktop/0.145.0-alpha.30 (Ubuntu 22.4.0; x86_64) "
        "xterm-256color (Codex Desktop; 26.715.72359)"
    )
    DEFAULT_CHAT_COMPLETIONS_SYSTEM_PROMPT = (
        "You are an expert AI artist tool. Your ONLY job is to generate images based on user inputs. "
        "Do NOT describe the image. Do NOT ask questions. Do NOT start a conversation. "
        "Directly output the generated image url or data."
    )

    PRESET_LIST_RENDER_OPTIONS = {
        "width": 1280,
        "height": 900,
        "full_page": True,
        "type": "png",
    }

    PRESET_LIST_TEMPLATE_FILES = {
        "default": "preset_list.html",
    }

    class ImageWorkflow:
        def __init__(
            self,
            proxy_url: str | None = None,
            max_retries: int = 3,
            timeout: int = 60,
            download_timeout: int | None = None,
            max_download_bytes: int = 120 * 1024 * 1024,
        ):
            self.proxy: str | None = None  # HTTP/HTTPS 代理
            self._socks_proxy_url: str | None = None
            self._socks_proxy_cls = None

            if proxy_url:
                normalized_proxy, scheme = self._normalize_proxy_url(proxy_url)
                if scheme.startswith("socks"):
                    try:
                        from aiohttp_socks import ProxyConnector
                    except ModuleNotFoundError as exc:  # pragma: no cover - 依赖缺失时的友好提示
                        raise RuntimeError(
                            "检测到 SOCKS 代理地址，但未安装 aiohttp_socks，请先执行 `pip install aiohttp_socks` 再重启插件。"
                        ) from exc
                    self._socks_proxy_cls = ProxyConnector
                    self._socks_proxy_url = normalized_proxy
                    logger.info("ImageWorkflow 使用 SOCKS 代理")
                else:
                    self.proxy = normalized_proxy
                    logger.info("ImageWorkflow 使用 HTTP 代理")

            self.max_retries = max_retries
            self.timeout = timeout
            self.download_timeout = download_timeout or timeout
            self.max_download_bytes = max(1, max_download_bytes)
            self._chunk_size = 512 * 1024  # 512KB

        async def terminate(self):
            """清理资源"""
            pass

        @staticmethod
        def _normalize_proxy_url(proxy_url: str) -> Tuple[str, str]:
            parsed = urlparse(proxy_url)
            scheme = (parsed.scheme or "").lower()
            if not scheme:
                return f"http://{proxy_url}", "http"
            return proxy_url, scheme

        def _build_request_kwargs(self) -> Dict[str, Any]:
            return {"proxy": self.proxy} if self.proxy else {}

        def get_request_kwargs(self) -> Dict[str, Any]:
            """Expose proxy kwargs for外部 HTTP 请求."""
            return self._build_request_kwargs()

        def create_client_session(
            self,
            timeout_cfg: aiohttp.ClientTimeout | None = None,
            *,
            timeout: aiohttp.ClientTimeout | None = None,
        ) -> aiohttp.ClientSession:
            effective_timeout = timeout_cfg or timeout
            if effective_timeout is None:
                effective_timeout = _build_client_timeout(self.timeout, self.download_timeout)

            session_kwargs: Dict[str, Any] = {"timeout": effective_timeout}
            if self._socks_proxy_url and self._socks_proxy_cls:
                connector = self._socks_proxy_cls.from_url(self._socks_proxy_url)
                session_kwargs["connector"] = connector
            return aiohttp.ClientSession(**session_kwargs)

        async def _download_image(self, url: str) -> bytes | None:
            logger.info(f"正在下载图片: {url}")
            timeout_cfg = _build_client_timeout(self.timeout, self.download_timeout)

            for i in range(self.max_retries + 1):
                try:
                    async with self.create_client_session(timeout_cfg) as session:
                        async with session.get(url, **self._build_request_kwargs()) as resp:
                            resp.raise_for_status()
                            total = 0
                            data = bytearray()
                            async for chunk in resp.content.iter_chunked(self._chunk_size):
                                if not chunk:
                                    break
                                data.extend(chunk)
                                total += len(chunk)
                                if total > self.max_download_bytes:
                                    limit_mb = self.max_download_bytes / 1024 / 1024
                                    raise ValueError(f"图片体积超过限制(>{limit_mb:.0f} MB)，放弃下载")
                        logger.info(f"图片下载完成，大小约 {total / 1024 / 1024:.2f} MB")
                        return bytes(data)
                except asyncio.TimeoutError:
                    logger.warning(f"下载超时 ({i + 1}/{self.max_retries}), 1秒后重试...")
                    if i < self.max_retries:
                        await asyncio.sleep(1)
                    else:
                        logger.error(f"图片下载超时（已达最大重试）: {url}")
                        return None
                except ValueError as e:
                    logger.error(str(e))
                    return None
                except Exception as e:
                    if i < self.max_retries:
                        logger.warning(f"下载失败 ({i + 1}/{self.max_retries}): {e}, 1秒后重试...")
                        await asyncio.sleep(1)
                    else:
                        logger.error(f"下载最终失败: {url}, 错误: {e}")
                        return None
            return None

        async def _get_avatar(self, user_id: str) -> bytes | None:
            if not user_id.isdigit():
                return None

            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
            return await self._download_image(avatar_url)

        def _extract_first_frame_sync(self, raw: bytes) -> bytes:
            img_io = io.BytesIO(raw)
            try:
                with PILImage.open(img_io) as img:
                    if getattr(img, "is_animated", False):
                        img.seek(0)

                    img_converted = img.convert("RGBA")
                    out_io = io.BytesIO()
                    img_converted.save(out_io, format="PNG")
                    return out_io.getvalue()
            except Exception:
                pass
            return raw

        async def _load_bytes(self, src: str) -> bytes | None:
            raw: bytes | None = None
            loop = asyncio.get_running_loop()
            src = (src or "").strip()

            try:
                if src.startswith("data:image") and "," in src:
                    raw = await loop.run_in_executor(None, base64.b64decode, src.split(",", 1)[1])
                elif src.startswith("file://"):
                    file_path = unquote(urlparse(src).path)
                    if re.match(r"^/[a-zA-Z]:/", file_path):
                        file_path = file_path.lstrip("/")
                    if Path(file_path).is_file():
                        raw = await loop.run_in_executor(None, Path(file_path).read_bytes)
                elif Path(src).is_file():
                    raw = await loop.run_in_executor(None, Path(src).read_bytes)
                elif src.startswith("http"):
                    raw = await self._download_image(src)
                elif src.startswith("base64://"):
                    raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
            except Exception as e:
                logger.warning(f"图片资源解析失败: {e}")
                return None

            if not raw:
                return None

            return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

        async def get_images(self, event: AstrMessageEvent) -> List[bytes]:
            """增强的图片获取方法，支持多@用户和混合@与图片"""
            img_bytes_list: List[bytes] = []
            at_user_ids: List[str] = []
            message_obj = getattr(event, "message_obj", None)
            message_segments = getattr(message_obj, "message", None)
            if not isinstance(message_segments, (list, tuple)):
                if message_segments is not None:
                    logger.warning("消息组件格式无效，无法解析其中的参考图")
                message_segments = ()
            message_str = str(getattr(event, "message_str", "") or "")
            
            # 统计各种来源的图片数量
            reply_image_count = 0
            message_image_count = 0

            logger.info("=== 开始获取图片资源 ===")
            logger.info(f"消息平台: {getattr(event, 'platform', 'unknown')}")
            logger.info(f"消息内容: {message_str}")

            # 1. 处理回复链中的图片
            for seg in message_segments:
                if not isinstance(seg, Reply):
                    continue
                reply_chain = getattr(seg, "chain", None)
                if not isinstance(reply_chain, (list, tuple)):
                    if getattr(seg, "id", None) not in (None, ""):
                        logger.info("引用消息未携带可解析的内容链，无法读取其中的图片")
                    continue
                if reply_chain:
                    logger.info(f"发现回复链，长度: {len(reply_chain)}")
                    for s_chain in reply_chain:
                        if isinstance(s_chain, Image):
                            logger.info("在回复链中发现图片")
                            image_url = getattr(s_chain, "url", None)
                            image_file = getattr(s_chain, "file", None)
                            if image_url and (img := await self._load_bytes(image_url)):
                                img_bytes_list.append(img)
                                reply_image_count += 1
                                logger.info("成功从回复链URL加载图片")
                            elif image_file and (img := await self._load_bytes(image_file)):
                                img_bytes_list.append(img)
                                reply_image_count += 1
                                logger.info("成功从回复链文件加载图片")

            # 2. 处理当前消息中的图片
            for seg in message_segments:
                if isinstance(seg, Image):
                    logger.info("在当前消息中发现图片")
                    image_url = getattr(seg, "url", None)
                    image_file = getattr(seg, "file", None)
                    if image_url and (img := await self._load_bytes(image_url)):
                        img_bytes_list.append(img)
                        message_image_count += 1
                        logger.info("成功从当前消息URL加载图片")
                    elif image_file and (img := await self._load_bytes(image_file)):
                        img_bytes_list.append(img)
                        message_image_count += 1
                        logger.info("成功从当前消息文件加载图片")

            # 3. 处理@用户（支持多@）
            for seg in message_segments:
                if isinstance(seg, At):
                    user_id = getattr(seg, "qq", None)
                    if user_id in (None, ""):
                        continue
                    at_user_ids.append(str(user_id))
                    logger.info(f"发现@用户: {user_id}")

            # 4. 处理命令文本中的@用户（从文本提取QQ号）
            import re
            text_at_matches = re.findall(r'@(\d+)', message_str)
            for qq in text_at_matches:
                if qq not in at_user_ids:
                    at_user_ids.append(qq)
                    logger.info(f"从文本提取到@用户: {qq}")

            logger.info(f"总共发现 {len(at_user_ids)} 个@用户")
            if at_user_ids:
                logger.info(f"@用户详情: {at_user_ids}")

            # 5. 获取@用户的头像
            avatar_count = 0
            if at_user_ids:
                for user_id in at_user_ids:
                    logger.info(f"尝试获取用户 [{user_id}] 的头像...")
                    if avatar := await self._get_avatar(user_id):
                        img_bytes_list.append(avatar)
                        avatar_count += 1
                        logger.info(f"成功获取用户 [{user_id}] 的头像")
                    else:
                        logger.warning(f"无法获取用户 [{user_id}] 的头像")

            logger.info(f"成功获取 {avatar_count} 个@用户头像")

            # 汇总统计
            logger.info(f"=== 图片资源获取完成 ===")
            logger.info(f"📊 图片来源统计:")
            logger.info(f"  • 回复链图片: {reply_image_count} 张")
            logger.info(f"  • 当前消息图片: {message_image_count} 张")
            logger.info(f"  • @用户头像: {avatar_count} 张")
            logger.info(f"  • 总计携带图片: {len(img_bytes_list)} 张")
            
            return img_bytes_list

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = StarTools.get_data_dir()
        self.log = _plugin_logger(self.plugin_data_dir)

        # 余额以「厘」为单位（1 厘 = 0.001 元）；旧版按次的 user_counts.json/group_counts.json 首次启动时自动迁移
        self.user_balances_file = self.plugin_data_dir / "user_balances.json"
        self.group_balances_file = self.plugin_data_dir / "group_balances.json"
        self.legacy_user_counts_file = self.plugin_data_dir / "user_counts.json"
        self.legacy_group_counts_file = self.plugin_data_dir / "group_counts.json"
        self.user_checkin_file = self.plugin_data_dir / "user_checkin.json"
        self.daily_stats_file = self.plugin_data_dir / "daily_stats.json"
        self.usage_store_file = self.plugin_data_dir / "usage_history.json"
        self.legacy_usage_store_file = self.plugin_data_dir / "usage_history.sqlite3"
        self.preset_images_file = self.plugin_data_dir / "preset_images.json"
        self.preset_images_dir = self.plugin_data_dir / "preset_images"

        self.user_balances: Dict[str, int] = {}
        self.group_balances: Dict[str, int] = {}
        self.user_checkin_data: Dict[str, str] = {}
        self.daily_stats: Dict[str, Any] = {}
        self.usage_store: Optional[UsageStore] = None
        self.prompt_map: Dict[str, str] = {}
        self.preset_images: Dict[str, str] = {}  # 预设词 -> 图片文件名映射
        self.request_timeout = 120
        self.download_timeout = 240
        self.max_download_bytes = 120 * 1024 * 1024

        self.generic_key_index = 0
        self.gemini_key_index = 0
        self.key_lock = asyncio.Lock()
        self._dashboard_config_lock = asyncio.Lock()

        self.iwf: Optional[FigurineProPlugin.ImageWorkflow] = None
        self.llm_tools_registered = False

    async def initialize(self):
        use_proxy = self.conf.get("use_proxy", False)
        proxy_url = self.conf.get("proxy_url") if use_proxy else None

        retries = _normalize_positive_int(self.conf.get("download_retries", 3), 3)
        timeout = _normalize_timeout(self.conf.get("timeout", 120))
        download_timeout = _normalize_timeout(
            self.conf.get("download_timeout", timeout * 2),
            default=timeout * 2,
            minimum=timeout,
        )
        max_download_mb = _normalize_positive_int(self.conf.get("download_size_limit_mb", 120), 120)
        max_download_bytes = max_download_mb * 1024 * 1024

        self.request_timeout = timeout
        self.download_timeout = download_timeout
        self.max_download_bytes = max_download_bytes

        self.iwf = self.ImageWorkflow(
            proxy_url,
            max_retries=retries,
            timeout=self.request_timeout,
            download_timeout=self.download_timeout,
            max_download_bytes=self.max_download_bytes,
        )

        await self._load_user_balances()
        await self._load_group_balances()
        await self._load_user_checkin_data()
        await self._load_daily_stats()
        await self._initialize_usage_store()
        await self._migrate_failure_deduction_config()
        await self._migrate_command_model_list_config()
        await self._migrate_extra_prefix_config()
        await self._migrate_prompt_list_config()
        await self._load_prompt_map()
        await self._load_preset_images()
        self._register_llm_tools()

        # 创建预设图片目录
        if not self.preset_images_dir.exists():
            self.preset_images_dir.mkdir(parents=True, exist_ok=True)

        logger.info("FigurinePro 插件已加载")

        g_keys = self.conf.get("generic_api_keys", [])
        o_keys = self.conf.get("gemini_api_keys", [])

        if not g_keys and not o_keys:
            logger.warning("FigurinePro: 未配置任何 API Key")

        self._register_usage_web_apis()

    async def _initialize_usage_store(self):
        try:
            store = UsageStore(self.usage_store_file, self.legacy_usage_store_file)
            await store.initialize(self.user_balances, self.group_balances, self.daily_stats)
            balances = await store.merge_balance_sources(self.user_balances, self.group_balances)
            self.user_balances = balances["user"]
            self.group_balances = balances["group"]
            # 启动即落盘余额：旧 user_counts.json 按汇率迁移后立即物化为 user_balances.json
            await self._save_user_balances()
            await self._save_group_balances()
            self.usage_store = store
            self.log.info("JSON 用量账本已就绪: %s", self.usage_store_file)
        except Exception as exc:
            self.usage_store = None
            self.log.error(f"用量账本初始化失败，将继续使用 JSON 余额数据: {exc}")

    def _dashboard_error(self, message: str, status: int = 403):
        if error_response:
            return error_response(message, status_code=status)
        return {"ok": False, "error": message, "message": message, "status": status}

    @staticmethod
    def _dashboard_json(payload: Dict[str, Any]):
        return json_response(payload) if json_response else payload

    @staticmethod
    def _dashboard_query_value(name: str, default: Any = "") -> Any:
        query = getattr(request, "query", {}) if request else {}
        try:
            return query.get(name, default)
        except AttributeError:
            return default

    @staticmethod
    def _dashboard_page_value(value: Any, default: int = 1) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _dashboard_date_value(value: Any, end: bool = False) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError("日期必须是 YYYY-MM-DD 格式")
        parsed = datetime.strptime(text, "%Y-%m-%d")
        if end:
            return (parsed + timedelta(days=1)).isoformat(timespec="seconds")
        return parsed.isoformat(timespec="seconds")

    def _register_usage_web_apis(self):
        if not WEB_API_AVAILABLE or not hasattr(self.context, "register_web_api"):
            return
        routes = (
            ("usage/overview", self._web_usage_overview, ["GET"], "查询手办化用量概览"),
            ("usage/users", self._web_usage_users, ["GET"], "查询手办化用户用量"),
            ("usage/groups", self._web_usage_groups, ["GET"], "查询手办化群组用量"),
            ("usage/events", self._web_usage_events, ["GET"], "查询手办化用量账本"),
            ("usage/adjust", self._web_usage_adjust, ["POST"], "调整手办化余额"),
            ("configuration", self._web_dashboard_configuration_get, ["GET"], "查询手办化仪表盘配置"),
            ("configuration", self._web_dashboard_configuration_save, ["POST"], "保存手办化仪表盘配置"),
            ("configuration/sensitive", self._web_dashboard_sensitive_get, ["GET"], "查询手办化敏感配置状态"),
            ("configuration/sensitive", self._web_dashboard_sensitive_save, ["POST"], "保存手办化敏感配置"),
            ("presets", self._web_dashboard_presets_get, ["GET"], "查询手办化预设提示词"),
            ("presets", self._web_dashboard_presets_save, ["POST"], "保存手办化预设提示词"),
        )
        for suffix, handler, methods, description in routes:
            try:
                self.context.register_web_api(
                    f"/astrbot_plugin_shoubanhua/{suffix}", handler, methods, description
                )
            except Exception as exc:
                logger.warning(f"注册用量仪表盘接口失败 ({suffix}): {exc}")

    async def _web_usage_overview(self):
        if not self.usage_store:
            return self._dashboard_error("用量账本不可用", 503)
        try:
            start = self._dashboard_date_value(self._dashboard_query_value("start"))
            end = self._dashboard_date_value(self._dashboard_query_value("end"), end=True)
            granularity = str(self._dashboard_query_value("granularity", "day")).strip().lower()
            if granularity not in {"day", "hour"}:
                raise ValueError("granularity 仅支持 day / hour")
            data = await self.usage_store.get_overview(start, end, granularity=granularity)
            return self._dashboard_json({"ok": True, **data})
        except ValueError as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception as exc:
            logger.error(f"查询用量概览失败: {exc}")
            return self._dashboard_error("查询用量概览失败", 500)

    async def _web_usage_users(self):
        if not self.usage_store:
            return self._dashboard_error("用量账本不可用", 503)
        try:
            data = await self.usage_store.list_users(
                start=self._dashboard_date_value(self._dashboard_query_value("start")),
                end=self._dashboard_date_value(self._dashboard_query_value("end"), end=True),
                search=str(self._dashboard_query_value("search", "")),
                page=self._dashboard_page_value(self._dashboard_query_value("page", 1)),
                page_size=self._dashboard_page_value(self._dashboard_query_value("page_size", 30), 30),
            )
            return self._dashboard_json({"ok": True, **data})
        except ValueError as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception as exc:
            logger.error(f"查询用户用量失败: {exc}")
            return self._dashboard_error("查询用户用量失败", 500)

    async def _web_usage_groups(self):
        if not self.usage_store:
            return self._dashboard_error("用量账本不可用", 503)
        try:
            data = await self.usage_store.list_groups(
                start=self._dashboard_date_value(self._dashboard_query_value("start")),
                end=self._dashboard_date_value(self._dashboard_query_value("end"), end=True),
                search=str(self._dashboard_query_value("search", "")),
                page=self._dashboard_page_value(self._dashboard_query_value("page", 1)),
                page_size=self._dashboard_page_value(self._dashboard_query_value("page_size", 30), 30),
            )
            return self._dashboard_json({"ok": True, **data})
        except ValueError as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception as exc:
            logger.error(f"查询群组用量失败: {exc}")
            return self._dashboard_error("查询群组用量失败", 500)

    async def _web_usage_events(self):
        if not self.usage_store:
            return self._dashboard_error("用量账本不可用", 503)
        try:
            data = await self.usage_store.list_events(
                start=self._dashboard_date_value(self._dashboard_query_value("start")),
                end=self._dashboard_date_value(self._dashboard_query_value("end"), end=True),
                user_id=self._norm_id(self._dashboard_query_value("user_id", "")),
                group_id=self._norm_id(self._dashboard_query_value("group_id", "")),
                model=str(self._dashboard_query_value("model", "")).strip(),
                outcome=str(self._dashboard_query_value("outcome", "")).strip().lower(),
                page=self._dashboard_page_value(self._dashboard_query_value("page", 1)),
                page_size=self._dashboard_page_value(self._dashboard_query_value("page_size", 15), 15),
            )
            return self._dashboard_json({"ok": True, **data})
        except ValueError as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception as exc:
            logger.error(f"查询用量账本失败: {exc}")
            return self._dashboard_error("查询用量账本失败", 500)

    async def _web_usage_adjust(self):
        try:
            body = await request.json(default={})
            subject_type = str(body.get("subject_type") or "").strip()
            subject_id = self._norm_id(body.get("subject_id"))
            amount_yuan = float(body.get("amount"))
            amount = yuan_to_amount(amount_yuan)
            note = str(body.get("note") or "").strip()[:500]
            if subject_type not in {"user", "group"} or not subject_id:
                raise ValueError("目标类型或 ID 无效")
            if not -100000 <= amount_yuan <= 100000 or amount == 0:
                raise ValueError("调整金额必须在 -100000 到 100000 元之间且不能为 0（精确到 0.001）")
            actor = self._norm_id(getattr(request, "username", ""))
            balance = await self._adjust_usage_balance(
                event=None,
                subject_type=subject_type,
                subject_id=subject_id,
                amount=amount,
                source="web_admin",
                actor=actor,
                note=note or "网页管理员调整余额",
            )
            return self._dashboard_json({"ok": True, "balance": balance})
        except (TypeError, ValueError) as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception as exc:
            logger.error(f"网页调整余额失败: {exc}")
            return self._dashboard_error("调整余额失败", 500)

    @staticmethod
    def _dashboard_string_list(value: Any, field_name: str) -> List[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} 必须是列表")
        result: List[str] = []
        for item in value:
            name = str(item or "").strip()
            if not name:
                continue
            if len(name) > 200:
                raise ValueError(f"{field_name} 包含过长名称")
            if name not in result:
                result.append(name)
        return result

    @staticmethod
    def _dashboard_command_name(value: Any, field_name: str) -> str:
        command = str(value or "").strip().lstrip("#").strip()
        if not command or len(command) > 80 or any(character.isspace() for character in command):
            raise ValueError(f"{field_name} 必须是不含空格、最长 80 字符的指令")
        return command

    @staticmethod
    def _preset_alias(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _schema_default_preset_items(cls) -> List[Dict[str, str]]:
        prompt_list = cls._dashboard_schema().get("prompt_list", {})
        raw_presets = prompt_list.get("default", []) if isinstance(prompt_list, dict) else []
        if not isinstance(raw_presets, list):
            return []

        presets: List[Dict[str, str]] = []
        for item in raw_presets:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not command or not prompt:
                continue
            preset = {
                "__template_key": "preset",
                "command": command,
                "prompt": prompt,
            }
            if alias := cls._preset_alias(item.get("legacy_alias")):
                preset["legacy_alias"] = alias
            presets.append(preset)
        return presets

    @classmethod
    def _schema_default_preset_aliases(cls) -> Dict[str, str]:
        return {
            item["command"]: item["legacy_alias"]
            for item in cls._schema_default_preset_items()
            if item.get("legacy_alias")
        }

    def _normalized_preset_items(self, raw_presets: Any = None) -> List[Dict[str, str]]:
        if raw_presets is None:
            raw_presets = self.conf.get("prompt_list", [])
        if not isinstance(raw_presets, list):
            return []

        schema_aliases = self._schema_default_preset_aliases()
        valid_aliases = set(schema_aliases.values())
        presets: List[Dict[str, str]] = []
        seen = set()
        for item in raw_presets:
            command = ""
            prompt = ""
            alias = ""
            if isinstance(item, dict):
                command = str(item.get("command") or item.get("指令") or item.get("name") or "").strip()
                prompt = str(item.get("prompt") or item.get("提示词") or item.get("value") or "").strip()
                alias = self._preset_alias(item.get("legacy_alias"))
            elif isinstance(item, str) and ":" in item:
                command, prompt = (part.strip() for part in item.split(":", 1))
            if not command or not prompt or command in seen:
                continue
            seen.add(command)
            preset = {
                "__template_key": "preset",
                "command": command,
                "prompt": prompt,
            }
            alias = alias if alias in valid_aliases else schema_aliases.get(command, "")
            if alias:
                preset["legacy_alias"] = alias
            presets.append(preset)
        return presets

    def _get_default_preset_aliases(self) -> Dict[str, str]:
        schema_aliases = self._schema_default_preset_aliases()
        aliases: Dict[str, str] = {}
        for preset in self._normalized_preset_items():
            alias = self._preset_alias(preset.get("legacy_alias")) or schema_aliases.get(preset["command"], "")
            if alias:
                aliases[preset["command"]] = alias
        return aliases

    def _get_default_preset_commands(self) -> set[str]:
        return set(self._get_default_preset_aliases())

    def _get_reserved_command_names(self) -> set[str]:
        names = set(self.RESERVED_COMMAND_NAMES)
        names.add(self._get_help_command())
        names.update(self._get_preset_list_commands())
        return names

    def _preset_command_conflict_message(
            self,
            command: str,
            *,
            prefixes: Optional[List[str]] = None,
            reserved_commands: Optional[set[str]] = None,
            preset_commands: Optional[set[str]] = None,
            help_command: Optional[str] = None,
            preset_list_commands: Optional[List[str]] = None,
    ) -> Optional[str]:
        active_prefixes = prefixes if prefixes is not None else self._get_extra_prefixes()
        if command in active_prefixes:
            return f"预设指令“{command}”与自定义触发词“{command}”冲突"

        if preset_commands is not None and command in preset_commands:
            return f"预设指令“{command}”与同一列表中的另一条预设指令重复"

        active_reserved_commands = (
            set(reserved_commands)
            if reserved_commands is not None
            else self._get_reserved_command_names()
        )
        if command not in active_reserved_commands:
            return None

        effective_help_command = help_command if help_command is not None else self._get_help_command()
        effective_preset_list_commands = (
            preset_list_commands
            if preset_list_commands is not None
            else self._get_preset_list_commands()
        )
        if command == effective_help_command:
            category = "帮助菜单指令"
        elif command in effective_preset_list_commands:
            category = "提示词列表指令"
        else:
            category = "插件专用指令"
        return f"预设指令“{command}”与{category}“{command}”冲突"

    @staticmethod
    def _dashboard_url(value: Any, field_name: str, *, required: bool = False) -> str:
        url = str(value or "").strip()
        if not url:
            if required:
                raise ValueError(f"{field_name} 不能为空")
            return ""
        if len(url) > 1000:
            raise ValueError(f"{field_name} 过长")
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"{field_name} 必须是合法的 HTTP(S) URL")
        if parsed_url.username or parsed_url.password:
            raise ValueError(f"{field_name} 不能包含认证信息，请使用敏感配置入口")
        sensitive_query_names = {
            "key", "api_key", "apikey", "token", "access_token", "password", "passwd",
        }
        if any(name.lower() in sensitive_query_names for name, _ in parse_qsl(parsed_url.query, keep_blank_values=True)):
            raise ValueError(f"{field_name} 不能包含敏感查询参数，请使用敏感配置入口")
        return url

    @staticmethod
    def _dashboard_bool(value: Any, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if value in {0, 1}:
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"true", "1", "yes", "on", "开启"}:
            return True
        if normalized in {"false", "0", "no", "off", "", "关闭"}:
            return False
        raise ValueError(f"{field_name} 必须是布尔值")

    @staticmethod
    def _dashboard_int(value: Any, field_name: str, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} 必须是整数") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间")
        return number

    @staticmethod
    def _dashboard_float(value: Any, field_name: str, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} 必须是数字") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间")
        return round(number, 3)

    @staticmethod
    def _dashboard_schema_path() -> Path:
        return Path(__file__).with_name("_conf_schema.json")

    @classmethod
    def _dashboard_schema(cls) -> Dict[str, Any]:
        try:
            with cls._dashboard_schema_path().open("r", encoding="utf-8") as schema_file:
                schema = json.load(schema_file)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"读取仪表盘配置 schema 失败: {exc}")
            return {}
        return schema if isinstance(schema, dict) else {}

    @staticmethod
    def _dashboard_setting_group(description: Any) -> str:
        matched = re.match(r"\s*【([^】]+)】", str(description or ""))
        return matched.group(1) if matched else "其他设置"

    @staticmethod
    def _dashboard_setting_label(description: Any, fallback: str) -> str:
        label = re.sub(r"^\s*【[^】]+】", "", str(description or "")).strip()
        return label or fallback

    @staticmethod
    def _dashboard_url_is_sensitive(value: Any) -> bool:
        url = str(value or "").strip()
        if not url:
            return False
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            return True
        sensitive_query_names = {
            "key", "api_key", "apikey", "token", "access_token", "password", "passwd",
        }
        return any(
            name.lower() in sensitive_query_names
            for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )

    @classmethod
    def _dashboard_proxy_url_is_sensitive(cls, value: Any) -> bool:
        return cls._dashboard_url_is_sensitive(value)

    @staticmethod
    def _dashboard_sensitive_service_url(value: Any, field_name: str) -> str:
        url = str(value or "").strip()
        if not url or len(url) > 1000:
            raise ValueError(f"{field_name} 格式无效")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{field_name} 必须是合法的 HTTP(S) URL")
        return url

    def _dashboard_sensitive_state(self) -> Dict[str, Any]:
        keys = self.conf.get("generic_api_keys", [])
        key_count = len(keys) if isinstance(keys, list) else 0
        generic_api_url = str(self.conf.get("generic_api_url", "") or "").strip()
        proxy_url = str(self.conf.get("proxy_url", "") or "").strip()
        return {
            "generic_api_keys": {"configured": bool(key_count), "count": key_count},
            "generic_api_url": {
                "configured": bool(generic_api_url),
                "write_only": self._dashboard_url_is_sensitive(generic_api_url),
            },
            "proxy_url": {
                "configured": bool(proxy_url),
                "write_only": self._dashboard_proxy_url_is_sensitive(proxy_url),
            },
        }

    def _dashboard_special_setting_keys(self) -> set[str]:
        return {
            "generic_api_url",
            "generic_api_keys",
            "gemini_api_keys",
            "extra_prefix",
            "command_model_list",
            "model_prompt_template_list",
            "model_parameter_list",
            "model_mapping_list",
            "prompt_list",
            "model",
            "model_list",
            "gemini_model_list",
            "chat_completions_model_list",
            "images_generations_model_list",
            "images_edits_model_list",
        }

    def _dashboard_settings_metadata(self) -> List[Dict[str, Any]]:
        metadata: List[Dict[str, Any]] = []
        reload_required = {
            "use_proxy", "proxy_url", "timeout", "download_timeout",
            "download_size_limit_mb", "download_retries", "enable_llm_tools",
            "llm_image_generation_model_list",
        }
        for key, spec in self._dashboard_schema().items():
            if key in self._dashboard_special_setting_keys() or not isinstance(spec, dict):
                continue
            setting_type = str(spec.get("type") or "string")
            if setting_type not in {"bool", "int", "float", "string", "text", "list"}:
                continue
            value = self.conf.get(key, copy.deepcopy(spec.get("default")))
            is_sensitive_proxy = key == "proxy_url" and self._dashboard_proxy_url_is_sensitive(value)
            metadata.append({
                "key": key,
                "label": self._dashboard_setting_label(spec.get("description"), key),
                "group": self._dashboard_setting_group(spec.get("description")),
                "hint": str(spec.get("hint") or ""),
                "type": setting_type,
                "default": copy.deepcopy(spec.get("default")),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "step": spec.get("step"),
                "options": copy.deepcopy(spec.get("options", [])),
                "item_type": str((spec.get("items") or {}).get("type") or "string"),
                "reload_required": key in reload_required,
                "write_only": is_sensitive_proxy,
            })
        return metadata

    def _dashboard_public_setting_values(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        for setting in self._dashboard_settings_metadata():
            if setting["write_only"]:
                continue
            key = setting["key"]
            values[key] = copy.deepcopy(self.conf.get(key, setting["default"]))
        return values

    def _dashboard_normalize_setting_values(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for setting in self._dashboard_settings_metadata():
            key = setting["key"]
            if setting["write_only"]:
                continue
            value = raw.get(key, copy.deepcopy(self.conf.get(key, setting["default"])))
            field_type = setting["type"]
            label = setting["label"]
            if field_type == "bool":
                result[key] = self._dashboard_bool(value, label)
            elif field_type == "int":
                minimum = int(setting["min"]) if setting["min"] is not None else -1_000_000_000
                maximum = int(setting["max"]) if setting["max"] is not None else 1_000_000_000
                result[key] = self._dashboard_int(value, label, minimum, maximum)
            elif field_type == "float":
                minimum = float(setting["min"]) if setting["min"] is not None else -1_000_000_000
                maximum = float(setting["max"]) if setting["max"] is not None else 1_000_000_000
                result[key] = self._dashboard_float(value, label, minimum, maximum)
            elif field_type in {"string", "text"}:
                normalized = str(value or "").strip()
                if len(normalized) > (20_000 if field_type == "text" else 1_000):
                    raise ValueError(f"{label} 过长")
                if setting["options"] and normalized not in setting["options"]:
                    raise ValueError(f"{label} 取值无效")
                if key == "generic_api_url":
                    normalized = self._dashboard_url(normalized, label)
                if key == "proxy_url" and self._dashboard_proxy_url_is_sensitive(normalized):
                    raise ValueError("带认证信息的代理地址请在敏感配置中保存")
                result[key] = normalized
            else:
                if not isinstance(value, list):
                    raise ValueError(f"{label} 必须是列表")
                if len(value) > 1_000:
                    raise ValueError(f"{label} 项数过多")
                item_type = setting["item_type"]
                items: List[Any] = []
                for item in value:
                    if item_type == "int":
                        items.append(self._dashboard_int(item, label, -1_000_000_000, 1_000_000_000))
                    else:
                        normalized_item = str(item or "").strip()
                        if not normalized_item or len(normalized_item) > 1_000:
                            raise ValueError(f"{label} 包含无效项目")
                        if normalized_item not in items:
                            items.append(normalized_item)
                result[key] = items
        return result

    def _dashboard_preset_items(self) -> List[Dict[str, str]]:
        return [
            {
                "command": preset["command"],
                "prompt": preset["prompt"],
                **({"legacy_alias": preset["legacy_alias"]} if preset.get("legacy_alias") else {}),
            }
            for preset in self._normalized_preset_items()
        ]

    def _dashboard_mapping_items(self) -> List[Dict[str, Any]]:
        """Normalize every runtime-supported failover shape for dashboard editing."""
        raw_mappings = self.conf.get("model_mapping_list", [])
        items: List[Dict[str, Any]] = []

        def add_item(source: Any, mapped: Any, priority: Any = 0) -> None:
            source_name = str(source or "").strip()
            if not source_name:
                return
            try:
                normalized_priority = int(priority)
            except (TypeError, ValueError):
                normalized_priority = 0
            if isinstance(mapped, (list, tuple, set)):
                for target in mapped:
                    add_item(source_name, target, normalized_priority)
                return
            target_name = str(mapped or "").strip()
            if target_name and target_name != source_name:
                items.append({
                    "model": source_name,
                    "mapped_model": target_name,
                    "priority": max(-1, normalized_priority),
                })

        def priority_of(item: Dict[str, Any]) -> Any:
            return item.get("priority") if "priority" in item else item.get("优先权重", 0)

        if isinstance(raw_mappings, dict):
            for source, mapped in raw_mappings.items():
                if isinstance(mapped, dict):
                    add_item(
                        source,
                        mapped.get("mapped_model")
                        or mapped.get("target_model")
                        or mapped.get("mapping_model")
                        or mapped.get("映射模型"),
                        priority_of(mapped),
                    )
                else:
                    add_item(source, mapped)
        elif isinstance(raw_mappings, list):
            for item in raw_mappings:
                if isinstance(item, dict):
                    add_item(
                        item.get("model") or item.get("source_model") or item.get("源模型"),
                        item.get("mapped_model")
                        or item.get("target_model")
                        or item.get("mapping_model")
                        or item.get("映射模型"),
                        priority_of(item),
                    )
                elif isinstance(item, str) and ":" in item:
                    source, mapped = item.split(":", 1)
                    add_item(source, mapped)
        return items

    def _dashboard_parameter_fields(self) -> List[Dict[str, Any]]:
        fields = [
            {"name": "reference_image_limit", "label": "参考图数量限制", "group": "基础与额度", "type": "number", "default": 0, "min": 0, "max": 14},
            {"name": "extra_reference_image_quota", "label": "超限参考图阶梯额度", "group": "基础与额度", "type": "number", "default": 0, "min": 0, "max": 14},
            {"name": "extra_reference_image_charge", "label": "超限参考图每阶梯加费(元)", "group": "基础与额度", "type": "number", "default": 0, "min": 0, "max": 100000, "step": 0.001, "float": True},
            {"name": "charge_amount", "label": "该模型单次生成扣费(元)", "group": "基础与额度", "type": "number", "default": 1, "min": 0.001, "max": 100000, "step": 0.001, "float": True},
            {"name": "charge_amount_2k", "label": "2K单次扣费(元，0=继承全局)", "group": "基础与额度", "type": "number", "default": 0, "min": 0, "max": 100000, "step": 0.001, "float": True},
            {"name": "charge_amount_4k", "label": "4K单次扣费(元，0=继承全局)", "group": "基础与额度", "type": "number", "default": 0, "min": 0, "max": 100000, "step": 0.001, "float": True},
            {"name": "deduct_on_violation", "label": "违规是否扣费", "group": "基础与额度", "type": "boolean", "default": False},
            {"name": "max_output_tokens", "label": "最大输出/思考 Token", "group": "基础与额度", "type": "number", "default": 0, "min": 0, "max": 1000000},
            {"name": "default_resolution", "label": "默认分辨率", "group": "基础与额度", "type": "text", "default": "auto", "max_length": 64},
            {"name": "send_default_size", "label": "默认传递 size", "group": "基础与额度", "type": "boolean", "default": False},
            {"name": "enable_gpt_parameters", "label": "启用 GPT 参数", "group": "GPT", "type": "boolean", "default": False},
            {"name": "omit_n_parameter", "label": "不传递 n 参数", "group": "GPT", "type": "boolean", "default": False},
            {"name": "quality", "label": "质量", "group": "GPT", "type": "select", "default": "auto", "options": ["low", "medium", "high", "auto"]},
            {"name": "moderation", "label": "审核", "group": "GPT", "type": "select", "default": "auto", "options": ["auto", "low"]},
            {"name": "gpt_background", "label": "背景", "group": "GPT", "type": "select", "default": "auto", "options": [
                {"value": "auto", "label": "auto（自动：不发送参数，由模型按提示词决定）"},
                {"value": "transparent", "label": "transparent（透明背景，仅 png/webp 输出）"},
                {"value": "opaque", "label": "opaque（不透明背景）"},
            ]},
            {"name": "adaptive_aspect_ratio", "label": "自适应比例", "group": "GPT", "type": "boolean", "default": False},
            {"name": "adaptive_resolution", "label": "自适应比例分辨率", "group": "GPT", "type": "select", "default": "1K", "options": ["1K", "2K", "4K"]},
            {"name": "auto_upgrade_1k_adaptive_resolution", "label": "1K超限自动转2K", "group": "GPT", "type": "boolean", "default": False},
            {"name": "force_resolution_limit", "label": "强制限制分辨率", "group": "GPT", "type": "boolean", "default": False},
            {"name": "enable_gemini_parameters", "label": "启用 Gemini 参数", "group": "Gemini", "type": "boolean", "default": False},
            {"name": "gemini_resolution", "label": "Gemini分辨率", "group": "Gemini", "type": "select", "default": "auto", "options": ["auto", "1K", "2K", "4K"]},
            {"name": "gemini_adaptive_aspect_ratio", "label": "Gemini自适应比例", "group": "Gemini", "type": "boolean", "default": False},
            {"name": "gemini_aspect_ratio", "label": "Gemini图片比例", "group": "Gemini", "type": "select", "default": "auto", "options": ["auto", *sorted(self.GEMINI_ASPECT_RATIO_OPTIONS)]},
            {"name": "enable_grok_parameters", "label": "启用 Grok 参数", "group": "Grok", "type": "boolean", "default": False},
            {"name": "grok_resolution", "label": "Grok分辨率", "group": "Grok", "type": "select", "default": "2k", "options": ["1k", "2k"]},
            {"name": "grok_adaptive_aspect_ratio", "label": "Grok自适应比例", "group": "Grok", "type": "boolean", "default": False},
            {"name": "enable_seedream_parameters", "label": "启用 Seedream 参数", "group": "Seedream", "type": "boolean", "default": False},
            {"name": "seedream_web_search", "label": "Seedream联网搜索", "group": "Seedream", "type": "boolean", "default": False},
            {"name": "seedream_send_output_format", "label": "Seedream传递输出格式", "group": "Seedream", "type": "boolean", "default": False},
            {"name": "seedream_output_format", "label": "Seedream输出格式", "group": "Seedream", "type": "select", "default": "png", "options": ["png", "jpeg"]},
            {"name": "seedream_watermark", "label": "Seedream添加水印", "group": "Seedream", "type": "boolean", "default": False},
            {"name": "seedream_resolution", "label": "Seedream分辨率", "group": "Seedream", "type": "select", "default": "1.5K", "options": ["1K", "1.5K", "2K"]},
            {"name": "seedream_send_aspect_ratio", "label": "Seedream传递比例", "group": "Seedream", "type": "boolean", "default": False},
            {"name": "seedream_send_detailed_resolution", "label": "Seedream传递详细分辨率", "group": "Seedream", "type": "boolean", "default": False},
            {"name": "seedream_pixel_limit", "label": "Seedream像素数上限 (K)", "group": "Seedream", "type": "number", "default": 0, "min": 0, "max": 16384},
            {"name": "seedream_adaptive_aspect_ratio", "label": "Seedream自适应比例", "group": "Seedream", "type": "boolean", "default": False},
            {"name": "seedream_max_side_2000", "label": "Seedream宽高均不超过2000", "group": "Seedream", "type": "boolean", "default": True},
            {"name": "seedream_side_over_2000_auto_2k", "label": "边长超2000自动升2K", "group": "Seedream", "type": "boolean", "default": True},
            {"name": "seedream_optimize_prompt_mode", "label": "Seedream提示词优化模式", "group": "Seedream", "type": "select", "default": "standard", "options": ["standard", "fast"]},
        ]
        generic_image_fields = {
            "default_resolution", "send_default_size", "enable_gpt_parameters", "omit_n_parameter",
            "quality", "moderation", "gpt_background", "adaptive_aspect_ratio", "adaptive_resolution",
            "auto_upgrade_1k_adaptive_resolution", "force_resolution_limit",
        }
        gemini_fields = {
            "enable_gemini_parameters", "gemini_resolution", "gemini_adaptive_aspect_ratio",
            "gemini_aspect_ratio",
        }
        grok_fields = {"enable_grok_parameters", "grok_resolution", "grok_adaptive_aspect_ratio"}
        seedream_fields = {
            "enable_seedream_parameters", "seedream_web_search", "seedream_send_output_format",
            "seedream_output_format", "seedream_watermark", "seedream_resolution",
            "seedream_send_aspect_ratio", "seedream_send_detailed_resolution", "seedream_pixel_limit",
            "seedream_adaptive_aspect_ratio", "seedream_max_side_2000",
            "seedream_side_over_2000_auto_2k", "seedream_optimize_prompt_mode",
        }
        mode_by_field = {
            **{name: "gpt" for name in generic_image_fields},
            **{name: "gemini" for name in gemini_fields},
            **{name: "grok" for name in grok_fields},
            **{name: "seedream" for name in seedream_fields},
        }
        for field_name in {
            *self.PARAMETER_MODE_ENABLE_FIELDS.values(),
            "default_resolution",
            "send_default_size",
        }:
            mode_by_field.pop(field_name, None)
        dependencies = {
            "omit_n_parameter": "enable_gpt_parameters",
            "quality": "enable_gpt_parameters",
            "moderation": "enable_gpt_parameters",
            "gpt_background": "enable_gpt_parameters",
            "adaptive_aspect_ratio": "enable_gpt_parameters",
            "adaptive_resolution": "enable_gpt_parameters",
            "auto_upgrade_1k_adaptive_resolution": "enable_gpt_parameters",
            "force_resolution_limit": "enable_gpt_parameters",
            "gemini_resolution": "enable_gemini_parameters",
            "gemini_adaptive_aspect_ratio": "enable_gemini_parameters",
            "gemini_aspect_ratio": "enable_gemini_parameters",
            "grok_resolution": "enable_grok_parameters",
            "grok_adaptive_aspect_ratio": "enable_grok_parameters",
            "seedream_web_search": "enable_seedream_parameters",
            "seedream_send_output_format": "enable_seedream_parameters",
            "seedream_output_format": "enable_seedream_parameters",
            "seedream_watermark": "enable_seedream_parameters",
            "seedream_resolution": "enable_seedream_parameters",
            "seedream_send_aspect_ratio": "enable_seedream_parameters",
            "seedream_send_detailed_resolution": "enable_seedream_parameters",
            "seedream_pixel_limit": "enable_seedream_parameters",
            "seedream_adaptive_aspect_ratio": "enable_seedream_parameters",
            "seedream_max_side_2000": "enable_seedream_parameters",
            "seedream_side_over_2000_auto_2k": "enable_seedream_parameters",
            "seedream_optimize_prompt_mode": "enable_seedream_parameters",
        }
        schema_parameter_items = (
            self._dashboard_schema().get("model_parameter_list", {}).get("templates", {})
            .get("model_parameters", {}).get("items", {})
        )
        for field in fields:
            name = field["name"]
            field["route"] = "any"
            field["endpoint_types"] = []
            if name == "max_output_tokens":
                field["route"] = "any"
                field["endpoint_types"] = ["chat_completions", "gemini_generate_content"]
            elif name in generic_image_fields:
                field["route"] = "generic"
                field["endpoint_types"] = ["images_generations", "images_edits"]
            elif name in gemini_fields:
                field["route"] = "gemini"
            elif name in grok_fields:
                field["route"] = "generic"
                field["endpoint_types"] = ["images_generations", "images_edits"]
            elif name in seedream_fields:
                field["route"] = "generic"
                field["endpoint_types"] = ["images_generations"]
            if name in dependencies:
                field["depends_on"] = {"field": dependencies[name], "equals": True}
            field["parameter_mode"] = mode_by_field.get(name, "base")
            if name in self.PARAMETER_MODE_ENABLE_FIELDS.values():
                field["parameter_mode"] = "mode_switch"
            schema_item = schema_parameter_items.get(name, {})
            field["hint"] = str(schema_item.get("hint") or "")
        return fields

    def _dashboard_model_parameter_items(self) -> List[Dict[str, Any]]:
        raw_entries = self._get_raw_model_parameter_entry_map()
        fields = self._dashboard_parameter_fields()
        known_fields = {field["name"] for field in fields}
        # 金额字段在运行时归一化Map中是「厘」，回显给配置页时必须换算回「元」
        money_fields = {field["name"] for field in fields if field.get("float")}
        reserved_keys = {
            "__template_key", "model", "模型", "model_name", "模型名", "parameter_mode",
        }
        items: List[Dict[str, Any]] = []
        for model, parameters in self._get_model_parameter_map().items():
            raw_entry = raw_entries.get(model, {})
            extensions = {
                key: copy.deepcopy(value)
                for key, value in raw_entry.items()
                if key not in known_fields and key not in reserved_keys
            }
            display = dict(parameters)
            for key in money_fields:
                if display.get(key) is not None:
                    display[key] = amount_to_yuan(display[key])
            items.append({"model": model, **extensions, **display})
        return items

    def _dashboard_configuration_values(self) -> Dict[str, Any]:
        generic_api_url = str(self.conf.get("generic_api_url", "") or "").strip()
        if self._dashboard_url_is_sensitive(generic_api_url):
            generic_api_url = ""
        else:
            try:
                generic_api_url = self._dashboard_url(generic_api_url, "共享 API 地址")
            except ValueError:
                generic_api_url = ""
        model_names = list(self._get_all_models())
        model_names.append(str(self.conf.get("model", "") or "").strip())
        template_map = self._get_model_prompt_template_map()
        parameter_map = self._get_model_parameter_map()
        model_names.extend(name for name in template_map if name != "ALL")
        model_names.extend(parameter_map)
        model_names = self._dedupe_preserve_order([name for name in model_names if name])
        return {
            "model": str(self.conf.get("model", "") or "").strip(),
            "model_list": model_names,
            "generic_api_url": generic_api_url,
            "gemini_model_list": self._normalize_model_list(self.conf.get("gemini_model_list", [])),
            "chat_completions_model_list": self._normalize_model_list(self.conf.get("chat_completions_model_list", [])),
            "images_generations_model_list": self._normalize_model_list(self.conf.get("images_generations_model_list", [])),
            "images_edits_model_list": self._normalize_model_list(self.conf.get("images_edits_model_list", [])),
            "extra_prefix": self._get_extra_prefixes(),
            "command_model_list": [
                {"command": command, "model": model}
                for command, model in self._get_command_model_map().items()
            ],
            "model_mapping_list": self._dashboard_mapping_items(),
            "model_prompt_template_list": [
                {"model": model, "prompt_template": template}
                for model, template in template_map.items()
            ],
            "model_parameter_list": self._dashboard_model_parameter_items(),
            "settings": self._dashboard_public_setting_values(),
        }

    def _dashboard_configuration_generation(self) -> int:
        try:
            return max(0, int(self.conf.get("_dashboard_config_generation", 0)))
        except (TypeError, ValueError):
            return 0

    async def _persist_configuration(self) -> None:
        """Persist the injected AstrBot configuration across supported host versions."""
        save_async = getattr(self.conf, "save_config_async", None)
        if callable(save_async):
            await save_async()
            return

        save_sync = getattr(self.conf, "save_config", None)
        if callable(save_sync):
            save_sync()
            return

        save_legacy = getattr(self.conf, "save", None)
        if callable(save_legacy):
            save_legacy()
            return

        raise RuntimeError("当前 AstrBot 配置对象不支持持久化（缺少 save_config_async/save_config）")

    def _dashboard_configuration_revision(self, values: Optional[Dict[str, Any]] = None) -> str:
        raw = json.dumps(
            values or self._dashboard_configuration_values(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        versioned = f"{self._dashboard_configuration_generation()}:{raw}"
        return hashlib.sha256(versioned.encode("utf-8")).hexdigest()[:16]

    def _dashboard_preset_revision(self) -> str:
        raw = json.dumps(
            self._dashboard_preset_items(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _dashboard_advance_configuration_generation(self) -> None:
        self.conf["_dashboard_config_generation"] = (
            self._dashboard_configuration_generation() + 1
        )

    def _dashboard_normalize_model_parameters(self, value: Any, models: set[str]) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("model_parameter_list 必须是列表")
        fields = {field["name"]: field for field in self._dashboard_parameter_fields()}
        result: List[Dict[str, Any]] = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("模型参数条目必须是对象")
            model = str(item.get("model") or "").strip()
            if model not in models or model in seen:
                raise ValueError("模型参数包含重复或未配置模型")
            seen.add(model)
            parameter_mode = self._get_parameter_mode_from_entry(item)
            normalized: Dict[str, Any] = {
                key: copy.deepcopy(raw_value)
                for key, raw_value in item.items()
                if key not in {"__template_key", "model", "模型", "model_name", "模型名", "parameter_mode"}
                and key not in fields
            }
            normalized.update({
                "__template_key": "model_parameters",
                "model": model,
                "parameter_mode": parameter_mode,
            })
            for name, field in fields.items():
                raw_value = item.get(name, field["default"])
                field_type = field["type"]
                if field_type == "boolean":
                    normalized[name] = self._dashboard_bool(raw_value, field["label"])
                elif field_type == "number":
                    if field.get("float"):
                        normalized[name] = self._dashboard_float(raw_value, field["label"], field["min"], field["max"])
                    else:
                        normalized[name] = self._dashboard_int(raw_value, field["label"], field["min"], field["max"])
                elif field_type == "select":
                    normalized_value = str(raw_value or field["default"]).strip()
                    option_values = {
                        option["value"] if isinstance(option, dict) else option
                        for option in field["options"]
                    }
                    if normalized_value not in option_values:
                        raise ValueError(f"{field['label']} 取值无效")
                    normalized[name] = normalized_value
                else:
                    normalized_value = str(raw_value or field["default"]).strip()
                    if not normalized_value or len(normalized_value) > field["max_length"]:
                        raise ValueError(f"{field['label']} 长度无效")
                    normalized[name] = normalized_value
            if normalized["auto_upgrade_1k_adaptive_resolution"]:
                normalized["force_resolution_limit"] = False
            for mode, enable_field in self.PARAMETER_MODE_ENABLE_FIELDS.items():
                normalized[enable_field] = parameter_mode == mode
            result.append(normalized)
        return result

    def _validate_dashboard_configuration(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("仪表盘配置必须是对象")
        model_list = self._dashboard_string_list(raw.get("model_list", []), "model_list")
        if not model_list:
            raise ValueError("至少需要配置一个模型")
        model_set = set(model_list)
        default_model = str(raw.get("model") or "").strip()
        if default_model not in model_set:
            raise ValueError("默认模型必须存在于模型列表")
        gemini_models = self._dashboard_string_list(raw.get("gemini_model_list", []), "gemini_model_list")
        generic_lists: Dict[str, List[str]] = {}
        for field_name in self.GENERIC_ENDPOINT_MODEL_LIST_KEYS.values():
            generic_lists[field_name] = self._dashboard_string_list(raw.get(field_name, []), field_name)
        for field_name, models in {"gemini_model_list": gemini_models, **generic_lists}.items():
            unknown = [model for model in models if model not in model_set]
            if unknown:
                raise ValueError(f"{field_name} 包含未配置模型: {unknown[0]}")
        generic_members = {model for models in generic_lists.values() for model in models}
        conflict = next((model for model in gemini_models if model in generic_members), None)
        if conflict:
            raise ValueError(f"模型 {conflict} 不能同时使用 Gemini 和 Generic 路由")
        current_generic_api_url = str(self.conf.get("generic_api_url", "") or "").strip()
        if self._dashboard_url_is_sensitive(current_generic_api_url):
            if str(raw.get("generic_api_url") or "").strip():
                raise ValueError("带认证信息的共享 API 地址请在敏感配置中保存")
            generic_api_url = current_generic_api_url
        else:
            generic_api_url = self._dashboard_url(raw.get("generic_api_url"), "共享 API 地址")
        settings_payload = raw.get("settings", {})
        if not isinstance(settings_payload, dict):
            raise ValueError("settings 必须是对象")
        settings = self._dashboard_normalize_setting_values(settings_payload)
        help_command = self._dashboard_command_name(
            settings.get("help_command", "手办化帮助"), "帮助菜单命令"
        )
        preset_list_command = self._dashboard_command_name(
            settings.get("preset_list_command", "手办化列表"), "提示词列表触发指令"
        )
        static_reserved_commands = set(self.RESERVED_COMMAND_NAMES)
        if help_command in static_reserved_commands:
            raise ValueError("帮助菜单命令不能与插件专用指令冲突")
        if preset_list_command in static_reserved_commands:
            raise ValueError("提示词列表触发指令不能与插件专用指令冲突")
        if help_command == preset_list_command:
            raise ValueError("帮助菜单命令不能与提示词列表触发指令相同")
        settings["help_command"] = help_command
        settings["preset_list_command"] = preset_list_command
        configured_reserved_commands = static_reserved_commands | {
            help_command,
            preset_list_command,
        }
        mappings: List[Dict[str, Any]] = []
        mapping_pairs = set()
        raw_mappings = raw.get("model_mapping_list", [])
        if not isinstance(raw_mappings, list):
            raise ValueError("model_mapping_list 必须是列表")
        for item in raw_mappings:
            if not isinstance(item, dict):
                raise ValueError("热备映射条目必须是对象")
            source = str(item.get("model") or "").strip()
            target = str(item.get("mapped_model") or "").strip()
            priority = self._dashboard_int(item.get("priority", 0), "热备优先级", -1, 10000)
            if source not in model_set or target not in model_set or source == target or (source, target) in mapping_pairs:
                raise ValueError("热备映射包含重复、自映射或未配置模型")
            mapping_pairs.add((source, target))
            mappings.append({"__template_key": "model_mapping", "model": source, "mapped_model": target, "priority": priority})
        active_mapping_sources = {item["model"] for item in mappings if item["priority"] >= 0}
        active_mapping_targets = {item["mapped_model"] for item in mappings if item["priority"] >= 0}
        effective_models = (model_set - active_mapping_sources) | active_mapping_targets
        if effective_models and not generic_api_url:
            raise ValueError("存在启用模型时必须配置共享 API 地址")
        prefixes = []
        for raw_prefix in self._dashboard_string_list(raw.get("extra_prefix", []), "extra_prefix"):
            prefix = self._dashboard_command_name(raw_prefix, "自定义触发词")
            if prefix in prefixes:
                raise ValueError("自定义触发词不能重复")
            if prefix in configured_reserved_commands:
                raise ValueError(f"自定义触发词不能覆盖插件专用指令: {prefix}")
            prefixes.append(prefix)
        if not prefixes:
            raise ValueError("至少需要保留一个自定义触发词")
        preset_commands = {
            item["command"]
            for item in self._dashboard_validate_presets(
                self._dashboard_preset_items(),
                prefixes=prefixes,
                reserved_commands=configured_reserved_commands,
                help_command=help_command,
                preset_list_commands=[preset_list_command],
            )
        }
        available_commands = set(prefixes) | preset_commands

        bindings: List[Dict[str, str]] = []
        bound_commands = set()
        raw_bindings = raw.get("command_model_list", [])
        if not isinstance(raw_bindings, list):
            raise ValueError("command_model_list 必须是列表")
        for item in raw_bindings:
            if not isinstance(item, dict):
                raise ValueError("指令模型绑定条目必须是对象")
            command = self._dashboard_command_name(item.get("command"), "绑定指令")
            model = str(item.get("model") or "").strip()
            if command not in available_commands:
                raise ValueError(f"绑定指令未启用: {command}")
            if model not in model_set or command in bound_commands:
                raise ValueError("指令模型绑定包含重复指令或未配置模型")
            bound_commands.add(command)
            bindings.append({"__template_key": "binding", "command": command, "model": model})
        templates: List[Dict[str, str]] = []
        template_models = set()
        raw_templates = raw.get("model_prompt_template_list", [])
        if not isinstance(raw_templates, list):
            raise ValueError("model_prompt_template_list 必须是列表")
        for item in raw_templates:
            if not isinstance(item, dict):
                raise ValueError("模型提示词模板条目必须是对象")
            model = str(item.get("model") or "").strip()
            template = str(item.get("prompt_template") or "").strip()
            if (model != "ALL" and model not in model_set) or model in template_models or not template or len(template) > 20000:
                raise ValueError("模型提示词模板包含重复、未配置模型或无效内容")
            template_models.add(model)
            templates.append({"__template_key": "model_prompt_template", "model": model, "prompt_template": template})
        parameters = self._dashboard_normalize_model_parameters(raw.get("model_parameter_list", []), model_set)
        return {
            "model": default_model,
            "model_list": model_list,
            "generic_api_url": generic_api_url,
            "gemini_model_list": gemini_models,
            **settings,
            **generic_lists,
            "extra_prefix": [{"__template_key": "prefix", "prefix": prefix} for prefix in prefixes],
            "command_model_list": bindings,
            "model_mapping_list": mappings,
            "model_prompt_template_list": templates,
            "model_parameter_list": parameters,
        }

    def _dashboard_validate_presets(
            self,
            raw_presets: Any,
            *,
            prefixes: Optional[List[str]] = None,
            reserved_commands: Optional[set[str]] = None,
            help_command: Optional[str] = None,
            preset_list_commands: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        if not isinstance(raw_presets, list):
            raise ValueError("prompt_list 必须是列表")
        active_prefixes = prefixes if prefixes is not None else self._get_extra_prefixes()
        active_reserved_commands = (
            set(reserved_commands)
            if reserved_commands is not None
            else self._get_reserved_command_names()
        )
        schema_aliases = self._schema_default_preset_aliases()
        valid_aliases = set(schema_aliases.values())
        used_aliases = set()
        presets: List[Dict[str, str]] = []
        preset_commands = set()
        for item in raw_presets:
            if not isinstance(item, dict):
                raise ValueError("预设条目必须是对象")
            command = self._dashboard_command_name(item.get("command"), "预设指令")
            prompt = str(item.get("prompt") or "").strip()
            if not prompt or len(prompt) > 20000:
                raise ValueError("预设提示词不能为空且不能超过 20000 字符")
            if conflict_message := self._preset_command_conflict_message(
                command,
                prefixes=active_prefixes,
                reserved_commands=active_reserved_commands,
                preset_commands=preset_commands,
                help_command=help_command,
                preset_list_commands=preset_list_commands,
            ):
                raise ValueError(conflict_message)
            preset = {"__template_key": "preset", "command": command, "prompt": prompt}
            submitted_alias = self._preset_alias(item.get("legacy_alias"))
            alias = submitted_alias or schema_aliases.get(command, "")
            if alias:
                if alias not in valid_aliases:
                    raise ValueError("预设包含无效的历史兼容别名")
                if alias in used_aliases:
                    raise ValueError("预设包含重复的历史兼容别名")
                preset["legacy_alias"] = alias
                used_aliases.add(alias)
            preset_commands.add(command)
            presets.append(preset)
        return presets

    def _dashboard_current_revision(self) -> str:
        return self._dashboard_configuration_revision(self._dashboard_configuration_values())

    async def _web_dashboard_configuration_get(self):
        values = self._dashboard_configuration_values()
        return self._dashboard_json({
            "ok": True,
            "revision": self._dashboard_configuration_revision(values),
            "config": values,
            "sensitive": self._dashboard_sensitive_state(),
            "metadata": {
                "model_parameter_fields": self._dashboard_parameter_fields(),
                "parameter_modes": [
                    {"value": value, "label": label}
                    for value, label in self.PARAMETER_MODES
                ],
                "settings": self._dashboard_settings_metadata(),
            },
        })

    async def _web_dashboard_sensitive_get(self):
        values = self._dashboard_configuration_values()
        return self._dashboard_json({
            "ok": True,
            "revision": self._dashboard_configuration_revision(values),
            "sensitive": self._dashboard_sensitive_state(),
        })

    async def _web_dashboard_presets_get(self):
        return self._dashboard_json({
            "ok": True,
            "revision": self._dashboard_preset_revision(),
            "presets": self._dashboard_preset_items(),
        })

    async def _web_dashboard_configuration_save(self):
        try:
            body = await request.json(default={})
            expected_revision = str(body.get("revision") or "").strip()
            async with self._dashboard_config_lock:
                current_values = self._dashboard_configuration_values()
                current_revision = self._dashboard_configuration_revision(current_values)
                if expected_revision != current_revision:
                    return self._dashboard_error("配置已被其他页面修改，请重新加载后再保存", 409)
                validated = self._validate_dashboard_configuration(body.get("config", body))
                previous = {
                    key: (key in self.conf, copy.deepcopy(self.conf.get(key)))
                    for key in {*validated, "_dashboard_config_generation"}
                }
                try:
                    for key, value in validated.items():
                        self.conf[key] = value
                    self._dashboard_advance_configuration_generation()
                    await self._persist_configuration()
                except Exception:
                    for key, (existed, value) in previous.items():
                        if existed:
                            self.conf[key] = value
                        else:
                            try:
                                del self.conf[key]
                            except (KeyError, TypeError):
                                pass
                    raise
                await self._load_prompt_map()
                values = self._dashboard_configuration_values()
            return self._dashboard_json({
                "ok": True,
                "revision": self._dashboard_configuration_revision(values),
                "config": values,
                "message": "仪表盘配置已保存，后续生成请求将使用新配置。",
            })
        except ValueError as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception as exc:
            logger.error("保存仪表盘配置失败", exc_info=True)
            reason = str(exc).strip() or exc.__class__.__name__
            return self._dashboard_error(
                f"保存仪表盘配置失败，运行中的配置已恢复：{reason}", 500
            )

    async def _web_dashboard_presets_save(self):
        try:
            body = await request.json(default={})
            expected_revision = str(body.get("revision") or "").strip()
            async with self._dashboard_config_lock:
                if expected_revision != self._dashboard_preset_revision():
                    return self._dashboard_error("预设已被其他页面或配置文件修改，请重新加载后再保存", 409)
                presets = self._dashboard_validate_presets(body.get("presets", body.get("prompt_list", [])))
                previous = {
                    "prompt_list": ("prompt_list" in self.conf, copy.deepcopy(self.conf.get("prompt_list"))),
                }
                try:
                    self.conf["prompt_list"] = presets
                    await self._persist_configuration()
                except Exception:
                    for key, (existed, value) in previous.items():
                        if existed:
                            self.conf[key] = value
                        else:
                            self.conf.pop(key, None)
                    raise
                await self._load_prompt_map()
            return self._dashboard_json({
                "ok": True,
                "revision": self._dashboard_preset_revision(),
                "presets": self._dashboard_preset_items(),
                "message": "预设提示词已保存。",
            })
        except ValueError as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception as exc:
            logger.error(f"保存预设提示词失败: {exc}")
            return self._dashboard_error("保存预设提示词失败，运行中的配置已恢复", 500)

    async def _web_dashboard_sensitive_save(self):
        try:
            body = await request.json(default={})
            expected_revision = str(body.get("revision") or "").strip()
            action = str(body.get("action") or "").strip()
            target = str(body.get("target") or "").strip()
            if target not in {"generic_api_keys", "generic_api_url", "proxy_url"}:
                raise ValueError("敏感配置目标无效")
            if action not in {"append", "replace", "clear"}:
                raise ValueError("敏感配置操作无效")
            async with self._dashboard_config_lock:
                if expected_revision != self._dashboard_current_revision():
                    return self._dashboard_error("配置已被其他页面修改，请重新加载后再保存", 409)
                previous = {
                    target: (target in self.conf, copy.deepcopy(self.conf.get(target))),
                    "_dashboard_config_generation": (
                        "_dashboard_config_generation" in self.conf,
                        copy.deepcopy(self.conf.get("_dashboard_config_generation")),
                    ),
                }
                if target == "generic_api_keys":
                    raw_values = body.get("values", [])
                    if not isinstance(raw_values, list):
                        raise ValueError("Key 池必须是列表")
                    values = []
                    for raw_value in raw_values:
                        value = str(raw_value or "").strip()
                        if not value or len(value) > 1000:
                            raise ValueError("Key 格式无效")
                        if value not in values:
                            values.append(value)
                    if action == "append":
                        current = self.conf.get(target, [])
                        current_values = list(current) if isinstance(current, list) else []
                        next_value = current_values + [value for value in values if value not in current_values]
                    elif action == "replace":
                        next_value = values
                    else:
                        next_value = []
                else:
                    if action == "append":
                        raise ValueError("该敏感配置不支持追加")
                    if action == "clear":
                        next_value = ""
                    else:
                        next_value = str(body.get("value") or "").strip()
                        if not self._dashboard_url_is_sensitive(next_value):
                            raise ValueError("敏感地址必须包含认证信息或敏感查询参数")
                        if target == "generic_api_url":
                            next_value = self._dashboard_sensitive_service_url(next_value, "共享 API 地址")
                        else:
                            parsed = urlparse(next_value)
                            if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.netloc:
                                raise ValueError("代理地址格式无效")
                try:
                    self.conf[target] = next_value
                    self._dashboard_advance_configuration_generation()
                    await self._persist_configuration()
                except Exception:
                    for key, (existed, value) in previous.items():
                        if existed:
                            self.conf[key] = value
                        else:
                            self.conf.pop(key, None)
                    raise
                values_snapshot = self._dashboard_configuration_values()
            return self._dashboard_json({
                "ok": True,
                "revision": self._dashboard_configuration_revision(values_snapshot),
                "sensitive": self._dashboard_sensitive_state(),
                "message": "敏感配置已保存。",
            })
        except ValueError as exc:
            return self._dashboard_error(str(exc), 400)
        except Exception:
            logger.error("保存敏感配置失败")
            return self._dashboard_error("保存敏感配置失败，运行中的配置已恢复", 500)

    def _register_llm_tools(self):
        if not self.conf.get("enable_llm_tools", False):
            logger.info("FigurinePro LLM 工具未启用")
            return
        if not LLM_TOOL_API_AVAILABLE:
            logger.warning("FigurinePro LLM 工具未注册：当前 AstrBot 版本不支持 FunctionTool")
            return
        if self.llm_tools_registered:
            return
        if not self._get_llm_tool_models():
            logger.warning(
                "FigurinePro LLM 工具未注册：请在 llm_image_generation_model_list 配置至少一个模型"
            )
            return

        try:
            tools = (
                TextToImageTool(plugin=self),
                ImageToImageTool(plugin=self),
            )
            for tool in tools:
                self._configure_llm_tool_model_parameter(tool)
            self.context.add_llm_tools(*tools)
            self.llm_tools_registered = True
            logger.info("FigurinePro LLM 工具已注册：generate_text_to_image, generate_image_to_image")
        except Exception as exc:
            logger.error(f"FigurinePro LLM 工具注册失败: {exc}")

    def _get_llm_tool_models(self) -> List[str]:
        return self._dedupe_preserve_order(
            self._normalize_model_list(
                self.conf.get("llm_image_generation_model_list", [])
            )
        )

    def _get_llm_tool_default_model(self) -> Optional[str]:
        available_models = self._get_llm_tool_models()
        if not available_models:
            return None

        default_model = str(self.conf.get("model", "nano-banana") or "nano-banana").strip()
        if default_model in available_models:
            return default_model
        return available_models[0]

    def _configure_llm_tool_model_parameter(self, tool: Any):
        parameters = tool.parameters
        if not isinstance(parameters, dict):
            return
        model_parameter = parameters.get("properties", {}).get("model")
        if not isinstance(model_parameter, dict):
            return

        available_models = self._get_llm_tool_models()
        if available_models:
            model_parameter["enum"] = available_models
        default_model = self._get_llm_tool_default_model()
        if default_model:
            model_parameter["default"] = default_model

    def _get_llm_tool_model(self, requested_model: Any) -> Tuple[Optional[str], Optional[str]]:
        available_models = self._get_llm_tool_models()
        if not available_models:
            return None, "LLM 生图模型列表为空，请配置 llm_image_generation_model_list。"

        requested_name = str(requested_model or "").strip()
        if requested_name:
            if requested_name not in available_models:
                return None, f"模型 '{requested_name}' 未在 LLM 生图模型列表中启用。"
            return requested_name, None

        model_name = self._get_llm_tool_default_model()
        if not model_name:
            return None, "未配置可用模型。"
        return model_name, None

    def _get_llm_tool_aspect_ratio(self, value: Any) -> Tuple[Optional[str], Optional[str]]:
        aspect_ratio = str(value or "").strip()
        if not aspect_ratio:
            return None, None
        if not self._parse_aspect_ratio(aspect_ratio):
            return None, "图片比例必须是宽:高格式，例如 1:1、16:9 或 9:16。"
        return aspect_ratio.replace("：", ":"), None

    def _get_llm_tool_batch_count(self, value: Any) -> Tuple[Optional[int], Optional[str]]:
        if value in (None, ""):
            return 1, None
        if isinstance(value, bool):
            return None, "batch_count 必须是正整数。"
        try:
            batch_count = int(value)
        except (TypeError, ValueError):
            return None, "batch_count 必须是正整数。"
        if isinstance(value, float) and not value.is_integer():
            return None, "batch_count 必须是正整数。"
        if batch_count < 1:
            return None, "batch_count 必须大于 0。"
        max_batch = _normalize_positive_int(self.conf.get("max_batch_multiplier", 4), 4)
        return min(batch_count, max_batch), None

    @staticmethod
    def _is_llm_tool_image_reference(value: str) -> bool:
        if value.startswith(("data:image/", "base64://")):
            return True
        return urlparse(value).scheme.lower() in {"http", "https"}

    async def _load_llm_tool_reference_images(
            self,
            reference_images: Any,
    ) -> Tuple[List[bytes], Optional[str]]:
        if reference_images is None:
            return [], None
        if isinstance(reference_images, str):
            sources = [reference_images]
        elif isinstance(reference_images, list):
            sources = reference_images
        else:
            return [], "reference_images 必须是图片 URL、data URL 或 base64:// 图片字符串列表。"

        if not self.iwf:
            return [], "图片工作流未初始化。"

        max_images = _normalize_positive_int(self.conf.get("max_images_count", 10), 10)
        images: List[bytes] = []
        invalid_count = 0
        for source in sources:
            if len(images) >= max_images:
                break
            image_ref = str(source or "").strip()
            if not image_ref or not self._is_llm_tool_image_reference(image_ref):
                invalid_count += 1
                continue
            if len(image_ref) > self.max_download_bytes * 2:
                invalid_count += 1
                continue
            image_bytes = await self.iwf._load_bytes(image_ref)
            if not image_bytes or len(image_bytes) > self.max_download_bytes:
                invalid_count += 1
                continue
            images.append(image_bytes)

        if sources and not images:
            return [], (
                "未能读取任何参考图。请传入可访问的 http(s) 图片 URL、data URL 或 base64:// 图片，"
                "不要传入本地路径或 file:// URL。"
            )
        if invalid_count:
            logger.warning(f"LLM 图生图工具忽略了 {invalid_count} 个无效参考图")
        return images, None

    def _build_llm_tool_result(self, **payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    async def _run_llm_image_tool(
            self,
            event: AstrMessageEvent,
            *,
            prompt: Any,
            model: Any = None,
            aspect_ratio: Any = None,
            batch_count: Any = None,
            reference_images: Any = None,
            require_images: bool = False,
    ) -> str:
        if maintenance_message := self._get_maintenance_message():
            return self._build_llm_tool_result(
                ok=False,
                error_type="maintenance",
                message=maintenance_message,
            )

        user_prompt = str(prompt or "").strip()
        if not user_prompt:
            return self._build_llm_tool_result(
                ok=False,
                error_type="invalid_prompt",
                message="prompt 不能为空。",
            )

        model_name, model_error = self._get_llm_tool_model(model)
        if model_error:
            return self._build_llm_tool_result(
                ok=False,
                error_type="invalid_model",
                message=model_error,
            )

        normalized_aspect_ratio, aspect_ratio_error = self._get_llm_tool_aspect_ratio(aspect_ratio)
        if aspect_ratio_error:
            return self._build_llm_tool_result(
                ok=False,
                error_type="invalid_aspect_ratio",
                message=aspect_ratio_error,
            )

        normalized_batch_count, batch_error = self._get_llm_tool_batch_count(batch_count)
        if batch_error:
            return self._build_llm_tool_result(
                ok=False,
                error_type="invalid_batch_count",
                message=batch_error,
            )

        image_bytes_list: List[bytes] = []
        if require_images:
            image_bytes_list, image_error = await self._load_llm_tool_reference_images(reference_images)
            if image_error:
                return self._build_llm_tool_result(
                    ok=False,
                    error_type="invalid_reference_images",
                    message=image_error,
                )
            if not image_bytes_list and self.iwf:
                image_bytes_list = await self.iwf.get_images(event)
            max_images = _normalize_positive_int(self.conf.get("max_images_count", 10), 10)
            image_bytes_list = image_bytes_list[:max_images]
            if not image_bytes_list:
                return self._build_llm_tool_result(
                    ok=False,
                    error_type="missing_reference_image",
                    message="图生图需要参考图。请提供 reference_images，或在当前/回复消息中附带图片。",
                )

        max_concurrency = min(
            normalized_batch_count,
            _normalize_positive_int(self.conf.get("max_batch_concurrency", 4), 4),
            20,
        )
        initial_candidate_model = self._get_model_failover_candidates(model_name)[0]
        initial_request_context = self._get_request_context(
            model_name,
            initial_candidate_model,
            bool(image_bytes_list),
        )
        initial_request_context["image_bytes_list"] = self._limit_reference_images(
            initial_candidate_model,
            image_bytes_list,
            initial_request_context["parameters"],
        )
        batch_semaphore = asyncio.Semaphore(max_concurrency)

        async def _record_llm_failover_attempt(
            failed_model: str,
            failed_result: Any,
            failed_status: int,
            succeeded: bool,
        ):
            failed_context = self._get_request_context(
                model_name,
                failed_model,
                bool(image_bytes_list),
            )
            failed_context["image_bytes_list"] = self._limit_reference_images(
                failed_model,
                image_bytes_list,
                failed_context["parameters"],
            )
            sender_id = self._norm_id(event.get_sender_id())
            group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None
            await self._settle_usage_generation(
                event=event,
                source="llm_tool",
                sender_id=sender_id,
                group_id=group_id,
                logical_model=model_name,
                actual_model=failed_context["actual_model"],
                has_images=bool(failed_context["image_bytes_list"]),
                outcome="failed",
                http_status=failed_status,
                output_count=0,
                charged_amount=0,
                deduction_source=None,
                note="LLM 工具热备切换前的中间失败",
                request_context=failed_context,
            )

        async def call_generation(batch_index: int):
            async with batch_semaphore:
                try:
                    result = await self._call_api(
                        image_bytes_list,
                        user_prompt,
                        override_model=model_name,
                        aspect_ratio=normalized_aspect_ratio,
                        force_aspect_ratio=bool(normalized_aspect_ratio),
                        return_request_context=True,
                        on_attempt=_record_llm_failover_attempt,
                    )
                except Exception as exc:
                    return batch_index, exc
                return batch_index, result

        tasks = [
            asyncio.create_task(call_generation(index))
            for index in range(1, normalized_batch_count + 1)
        ]
        successful_models: List[str] = []
        failures: List[Dict[str, Any]] = []

        for completed_task in asyncio.as_completed(tasks):
            batch_index, result = await completed_task
            if isinstance(result, Exception):
                failures.append({
                    "batch_index": batch_index,
                    "error_type": "system_error",
                    "message": self._safe_error_text(str(result), 300),
                })
                sender_id = self._norm_id(event.get_sender_id())
                group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None
                await self._settle_usage_generation(
                    event=event,
                    source="llm_tool",
                    sender_id=sender_id,
                    group_id=group_id,
                    logical_model=model_name,
                    actual_model=initial_request_context["actual_model"],
                    has_images=bool(initial_request_context["image_bytes_list"]),
                    outcome="failed",
                    http_status=0,
                    output_count=0,
                    charged_amount=0,
                    deduction_source=None,
                    note="LLM 工具系统错误",
                    request_context=initial_request_context,
                )
                continue

            generated_image, http_status, request_context = result
            actual_model = request_context["actual_model"]
            candidate_images = request_context["image_bytes_list"]
            if not isinstance(generated_image, bytes):
                error_data = generated_image if isinstance(generated_image, dict) else {}
                failures.append({
                    "batch_index": batch_index,
                    "error_type": str(error_data.get("error_type") or "generation_failed"),
                    "http_status": http_status,
                    "message": self._safe_error_text(
                        error_data.get("message") or generated_image,
                        300,
                    ),
                    "model": actual_model,
                })
                sender_id = self._norm_id(event.get_sender_id())
                group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None
                await self._settle_usage_generation(
                    event=event,
                    source="llm_tool",
                    sender_id=sender_id,
                    group_id=group_id,
                    logical_model=model_name,
                    actual_model=actual_model,
                    has_images=bool(candidate_images),
                    outcome="failed",
                    http_status=http_status,
                    output_count=0,
                    charged_amount=0,
                    deduction_source=None,
                    request_context=request_context,
                )
                continue

            sender_id = self._norm_id(event.get_sender_id())
            group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None
            await self._settle_usage_generation(
                event=event,
                source="llm_tool",
                sender_id=sender_id,
                group_id=group_id,
                logical_model=model_name,
                actual_model=actual_model,
                has_images=bool(candidate_images),
                outcome="success",
                http_status=http_status,
                output_count=1,
                charged_amount=0,
                deduction_source=None,
                request_context=request_context,
            )
            await self._record_daily_usage(sender_id, group_id)
            sent = await self._send_llm_image_once(
                event,
                generated_image,
                f"LLM 工具生成完成 {batch_index}/{normalized_batch_count} | 模型: {actual_model}",
            )
            if not sent:
                failures.append({
                    "batch_index": batch_index,
                    "error_type": "delivery_failed",
                    "message": "图片已生成，但发送到聊天平台失败。",
                    "model": actual_model,
                })
            else:
                successful_models.append(actual_model)


        result_payload: Dict[str, Any] = {
            "ok": bool(successful_models),
            "mode": "image_to_image" if require_images else "text_to_image",
            "generated": len(successful_models),
            "requested": normalized_batch_count,
            "models": self._dedupe_preserve_order(successful_models),
            "aspect_ratio": normalized_aspect_ratio or "default",
        }
        if failures:
            result_payload["failures"] = failures
        return self._build_llm_tool_result(**result_payload)

    async def _migrate_failure_deduction_config(self):
        legacy_key = "deduct_on_content_policy_violation"
        current_key = "deduct_on_failure_status_codes"
        if legacy_key not in self.conf or current_key in self.conf:
            return

        self.conf[current_key] = bool(self.conf.get(legacy_key, True))
        try:
            await self._persist_configuration()
            logger.info("已迁移违规内容扣次配置为错误码扣次配置")
        except Exception as exc:
            logger.error(f"迁移错误码扣次配置失败: {exc}")

    def _extract_image_urls_from_text(self, text: str) -> List[str]:
        """从文本中提取图片链接和本地文件路径"""
        image_urls = []

        # 1. 匹配 data URL / file URL / 本地文件路径
        for match in re.findall(r"data:image/[a-zA-Z0-9.+-]+;base64,[a-zA-Z0-9+/=]+", text, re.IGNORECASE):
            if match and match not in image_urls:
                image_urls.append(match)

        for match in re.findall(r"file://[^\s,，。！？\n]+\.(?:jpg|jpeg|png|gif|bmp|webp)", text, re.IGNORECASE):
            if match and match not in image_urls:
                image_urls.append(match)

        # 匹配 C:\path\to\image.jpg 格式
        local_file_patterns = [
            r'[a-zA-Z]:\\[^\s,，。！？\n]+\.(?:jpg|jpeg|png|gif|bmp|webp)',  # Windows绝对路径
        ]

        for pattern in local_file_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if match and match not in image_urls:
                    # 检查文件是否存在
                    if Path(match).exists():
                        image_urls.append(match)

        # 2. 匹配常见的图片链接格式
        url_patterns = [
            r'https?://[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])',
            r'https?://[^\s<>"\'\)]+/(?:s\d+/|upload/|image/|img/|pic/)[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])',
            r'https?://youke\d+\.picui\.cn/[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])'
        ]

        for pattern in url_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if match and match not in image_urls:
                    image_urls.append(match)

        return image_urls

    async def _download_preset_image(self, image_url: str) -> bytes | None:
        """下载预设内容中的图片（支持本地文件和网络图片）"""
        import ssl
        from pathlib import Path

        # 清理URL，移除可能的尾随标点符号
        clean_url = image_url.strip().rstrip('.,;:!?')

        if clean_url.startswith(("data:image", "base64://", "file://")):
            try:
                return await self.iwf._load_bytes(clean_url)
            except Exception as e:
                logger.error(f"加载内联图片失败: {e}")
                return None

        # 检查是否是本地文件路径
        if Path(clean_url).is_file():
            logger.info(f"检测到本地文件路径: {clean_url}")
            try:
                # 使用现有的 _load_bytes 方法处理本地文件
                return await self.iwf._load_bytes(clean_url)
            except Exception as e:
                logger.error(f"加载本地文件失败: {clean_url}, 错误: {e}")
                return None

        # 网络图片处理（原有的下载逻辑）
        for attempt in range(3):  # 最多重试3次
            try:
                logger.info(f"正在下载预设内容中的网络图片: {clean_url} (尝试 {attempt + 1}/3)")

                # 创建SSL上下文，允许更多SSL配置
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                # 创建不使用代理的下载器，使用自定义SSL上下文
                timeout_cfg = _build_client_timeout(self.request_timeout, self.download_timeout)
                connector = aiohttp.TCPConnector(ssl=ssl_context, limit=10)

                async with aiohttp.ClientSession(connector=connector, timeout=timeout_cfg) as session:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    async with session.get(clean_url, headers=headers) as resp:
                        resp.raise_for_status()
                        chunk_size = getattr(self.iwf, "_chunk_size", 512 * 1024)
                        data = bytearray()
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            if not chunk:
                                break
                            data.extend(chunk)
                            if len(data) > self.max_download_bytes:
                                limit_mb = self.max_download_bytes / 1024 / 1024
                                raise ValueError(f"预设图片体积超过限制(>{limit_mb:.0f} MB)，放弃下载")
                        logger.info(f"预设图片下载完成，大小约 {len(data) / 1024 / 1024:.2f} MB")
                        return bytes(data)

            except asyncio.TimeoutError:
                logger.warning(f"下载预设图片超时 (尝试 {attempt + 1}/3): {clean_url}")
                if attempt < 2:  # 如果不是最后一次尝试，等待1秒
                    await asyncio.sleep(1)
                else:
                    logger.error(f"预设图片下载最终超时: {clean_url}")
                    return None
            except ValueError as e:
                logger.error(str(e))
                return None
            except Exception as e:
                logger.warning(f"下载预设图片失败 (尝试 {attempt + 1}/3): {clean_url}, 错误: {e}")
                if attempt < 2:  # 如果不是最后一次尝试，等待1秒
                    await asyncio.sleep(1)
                else:
                    logger.error(f"下载预设图片最终失败: {clean_url}, 错误: {e}")
                    return None
        return None

    async def _migrate_prompt_list_config(self):
        raw_prompt_list = self.conf.get("prompt_list", [])
        had_prompts = "prompts" in self.conf
        prompts_cfg = self.conf.get("prompts", {})
        if not isinstance(raw_prompt_list, list):
            raw_prompt_list = []
        try:
            preset_schema_version = int(self.conf.get("_preset_schema_version", 0))
        except (TypeError, ValueError):
            preset_schema_version = 0

        schema_defaults = self._schema_default_preset_items()
        schema_aliases = self._schema_default_preset_aliases()
        alias_commands = {alias: command for command, alias in schema_aliases.items()}
        existing_presets = self._normalized_preset_items(raw_prompt_list)
        existing_commands = {preset["command"] for preset in existing_presets}
        legacy_values: Dict[str, str] = {}
        legacy_custom_presets: List[Dict[str, str]] = []

        if isinstance(prompts_cfg, dict):
            for raw_key, raw_value in prompts_cfg.items():
                key = str(raw_key or "").strip()
                value = raw_value.get("default") if isinstance(raw_value, dict) else raw_value
                prompt = str(value or "").strip()
                if not key or not prompt:
                    continue
                command = alias_commands.get(key, key)
                if command in schema_aliases:
                    legacy_values[command] = prompt
                elif command not in existing_commands:
                    legacy_custom_presets.append({
                        "__template_key": "preset",
                        "command": command,
                        "prompt": prompt,
                    })
                    existing_commands.add(command)

        explicit_presets = {preset["command"]: preset for preset in existing_presets}
        if preset_schema_version < 1:
            migrated = []
            for default_preset in schema_defaults:
                migrated.append(copy.deepcopy(explicit_presets.pop(default_preset["command"], default_preset)))
            migrated.extend(explicit_presets.values())
        else:
            migrated = list(existing_presets)

        migrated_by_command = {preset["command"]: preset for preset in migrated}
        for command, prompt in legacy_values.items():
            if command in existing_commands:
                continue
            alias = schema_aliases[command]
            legacy_preset = {
                "__template_key": "preset",
                "command": command,
                "prompt": prompt,
                "legacy_alias": alias,
            }
            if command in migrated_by_command:
                index = migrated.index(migrated_by_command[command])
                migrated[index] = legacy_preset
            elif preset_schema_version < 1:
                migrated.append(legacy_preset)
            migrated_by_command[command] = legacy_preset
        migrated.extend(legacy_custom_presets)

        needs_migration = (
            migrated != raw_prompt_list
            or had_prompts
            or preset_schema_version < 1
        )
        if not needs_migration:
            return

        previous_prompt_list = copy.deepcopy(self.conf.get("prompt_list"))
        previous_schema_version = self.conf.get("_preset_schema_version")
        previous_prompts = copy.deepcopy(prompts_cfg)
        self.conf["prompt_list"] = migrated
        self.conf["_preset_schema_version"] = 1
        if had_prompts:
            try:
                del self.conf["prompts"]
            except (KeyError, TypeError):
                self.conf["prompts"] = {}
        try:
            await self._persist_configuration()
            logger.info(f"已自动迁移 prompt_list 到配置驱动预设，共 {len(migrated)} 条")
        except Exception as exc:
            self.conf["prompt_list"] = previous_prompt_list
            if previous_schema_version is None:
                self.conf.pop("_preset_schema_version", None)
            else:
                self.conf["_preset_schema_version"] = previous_schema_version
            if had_prompts:
                self.conf["prompts"] = previous_prompts
            logger.error(f"自动迁移 prompt_list 配置失败: {exc}")

    async def _load_prompt_map(self):
        self.prompt_map = {
            preset["command"]: preset["prompt"]
            for preset in self._normalized_preset_items()
        }

    def _get_custom_preset_prompt(self, preset_name: str) -> Optional[str]:
        command = (preset_name or "").strip()
        if command in self._get_default_preset_commands():
            return None
        return self.prompt_map.get(command)

    def _resolve_bnn_prompt(
            self,
            raw_prompt: str,
            allow_append: bool,
            separator: str,
    ) -> Tuple[str, str]:
        """展开 bnn 后的自定义预设，返回（最终提示词，命中的预设名）。"""
        raw_prompt = str(raw_prompt or "").strip()
        if not raw_prompt:
            return "", ""

        if allow_append and separator and separator in raw_prompt:
            candidate_name, candidate_append = raw_prompt.split(separator, 1)
            candidate_prompt = self._get_custom_preset_prompt(candidate_name)
            if candidate_prompt is not None:
                return candidate_prompt + candidate_append.strip(), candidate_name.strip()

        preset_prompt = self._get_custom_preset_prompt(raw_prompt)
        if preset_prompt is not None:
            return preset_prompt, raw_prompt
        return raw_prompt, ""

    @staticmethod
    def _normalize_model_list(raw_list: Any) -> List[str]:
        models = []
        # 兼容处理：确保返回的是字符串列表
        if not isinstance(raw_list, list):
            return models

        for item in raw_list:
            if isinstance(item, str):
                model = item.strip()
                if model:
                    models.append(model)
            elif isinstance(item, dict) and "id" in item:
                # 兼容旧配置
                model = str(item["id"]).strip()
                if model:
                    models.append(model)
        return models

    @staticmethod
    def _dedupe_preserve_order(items: List[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    def _get_endpoint_models(self, endpoint_type: str) -> List[str]:
        key = self.GENERIC_ENDPOINT_MODEL_LIST_KEYS.get(endpoint_type, "")
        if not key:
            return []
        return self._normalize_model_list(self.conf.get(key, []))

    def _has_generic_endpoint_model_routes(self) -> bool:
        return any(
            self._get_endpoint_models(endpoint_type)
            for endpoint_type in self.GENERIC_ENDPOINT_MODEL_LIST_KEYS
        )

    def _get_all_models(self) -> List[str]:
        """从通用模型列表和端点模型列表中获取所有 model ID。"""
        models = self._normalize_model_list(self.conf.get("model_list", []))
        models.extend(self._normalize_model_list(self.conf.get("gemini_model_list", [])))
        for endpoint_type in (
            "chat_completions",
            "images_generations",
            "images_edits",
        ):
            models.extend(self._get_endpoint_models(endpoint_type))
        models.extend(self._get_command_model_map().values())
        model_mapping = self._get_model_mapping_map()
        models.extend(model_mapping.keys())
        for mapped_models in model_mapping.values():
            models.extend(mapped_models)
        return self._dedupe_preserve_order(models)

    def _get_command_model_map(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        raw_list = self.conf.get("command_model_list", [])
        if not isinstance(raw_list, list):
            return mapping

        for item in raw_list:
            command = ""
            model = ""
            if isinstance(item, dict):
                command = str(item.get("command") or item.get("指令") or "").strip()
                model = str(item.get("model") or item.get("模型名") or item.get("model_name") or "").strip()
            elif isinstance(item, str) and ":" in item:
                command, model = (part.strip() for part in item.split(":", 1))
            if command and model:
                mapping[command] = model
        return mapping

    def _get_command_model(self, command: str) -> Optional[str]:
        return self._get_command_model_map().get((command or "").strip())

    @staticmethod
    def _get_text_display_width(text: str) -> int:
        """按东亚字符宽度计算文本列宽，用于纯文本帮助内容对齐。"""
        width = 0
        for character in text:
            if unicodedata.combining(character):
                continue
            width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
        return width

    def _get_binding_default_price_text(self, model: str) -> str:
        """返回该命令普通一次生成的默认价格文本（取热备候选中最高单次扣费，与余额预检口径一致）。"""
        candidates = self._get_model_failover_candidates(model) or [model]
        cost = max(self._get_required_invocation_cost(candidate) for candidate in candidates)
        return f"{format_amount(cost)}"

    def _get_custom_command_model_bindings_text(self, with_price: bool = False) -> str:
        """返回自定义提示词前缀与实际选择模型的帮助文本；with_price 时在模型后附默认价格。"""
        default_model = str(self.conf.get("model", "nano-banana") or "nano-banana").strip()
        default_model = default_model or "nano-banana"
        command_models = self._get_command_model_map()
        seen = set()
        bindings: List[Tuple[str, str]] = []

        for prefix in self._get_extra_prefixes():
            command = (prefix or "").strip().lstrip("#")
            if not command or command in seen:
                continue
            seen.add(command)
            bindings.append((command, command_models.get(command) or default_model))

        max_command_width = max(
            (self._get_text_display_width(command) for command, _ in bindings),
            default=0,
        )
        lines: List[str] = []
        for command, model in bindings:
            padded = f"{command}{' ' * (max_command_width - self._get_text_display_width(command))}"
            if with_price:
                lines.append(f"{padded} -> {model}（{self._get_binding_default_price_text(model)}）")
            else:
                lines.append(f"{padded} -> {model}")
        return "\n".join(lines)

    def _render_help_text(self) -> str:
        help_text = str(self.conf.get("help_text", "帮助文档未配置") or "")
        # 先替换带价格的变量名（更长），避免其前缀被子串替换破坏
        return help_text.replace(
            "{custom_command_model_bindings_with_price}",
            self._get_custom_command_model_bindings_text(with_price=True),
        ).replace(
            "{custom_command_model_bindings}",
            self._get_custom_command_model_bindings_text(),
        )

    def _get_model_mapping_map(self) -> Dict[str, List[str]]:
        """获取源模型到按优先权重排列的热备模型映射。"""
        mapping_entries: Dict[str, List[Tuple[int, str]]] = {}
        mapping: Dict[str, List[str]] = {}
        raw_list = self.conf.get("model_mapping_list", [])

        def add_item(source: Any, mapped: Any, priority: Any = 0):
            source_name = str(source or "").strip()
            if not source_name:
                return

            try:
                normalized_priority = int(priority)
            except (TypeError, ValueError):
                normalized_priority = 0
            if normalized_priority == -1:
                return
            normalized_priority = max(0, normalized_priority)
            if isinstance(mapped, (list, tuple, set)):
                for item in mapped:
                    add_item(source_name, item, normalized_priority)
                return

            mapped_name = str(mapped or "").strip()
            if not mapped_name or mapped_name == source_name:
                return

            mapping_entries.setdefault(source_name, []).append(
                (normalized_priority, mapped_name)
            )

        def get_priority(item: Dict[str, Any]) -> Any:
            return item.get("priority") if "priority" in item else item.get("优先权重")

        if isinstance(raw_list, dict):
            for source, mapped in raw_list.items():
                if isinstance(mapped, dict):
                    add_item(
                        source,
                        mapped.get("mapped_model")
                        or mapped.get("target_model")
                        or mapped.get("映射模型"),
                        get_priority(mapped),
                    )
                else:
                    add_item(source, mapped)
        elif isinstance(raw_list, list):
            for item in raw_list:
                if isinstance(item, dict):
                    add_item(
                        item.get("model")
                        or item.get("source_model")
                        or item.get("源模型"),
                        item.get("mapped_model")
                        or item.get("target_model")
                        or item.get("mapping_model")
                        or item.get("映射模型"),
                        get_priority(item),
                    )
                elif isinstance(item, str) and ":" in item:
                    source, mapped = item.split(":", 1)
                    add_item(source, mapped)

        for source, candidates in mapping_entries.items():
            # Python 的稳定排序保留同权重配置的原始顺序。
            ordered_candidates = sorted(
                candidates,
                key=lambda candidate: candidate[0],
                reverse=True,
            )
            seen = set()
            mapping[source] = []
            for _, mapped in ordered_candidates:
                if mapped in seen:
                    continue
                seen.add(mapped)
                mapping[source].append(mapped)

        return mapping

    def _get_model_failover_candidates(self, model_name: str) -> List[str]:
        """返回实际调用模型列表；配置映射时首项即为首选模型。"""
        source_name = (model_name or "").strip()
        if not source_name:
            return []
        mapped_models = self._get_model_mapping_map().get(source_name, [])
        return mapped_models or [source_name]

    def _get_model_prompt_template_map(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        raw_list = self.conf.get("model_prompt_template_list", [])

        def add_item(model: Any, template: Any):
            model_name = str(model or "").strip()
            prompt_template = str(template or "").strip()
            if model_name and prompt_template:
                mapping[model_name] = prompt_template

        if isinstance(raw_list, dict):
            for model_name, prompt_template in raw_list.items():
                add_item(model_name, prompt_template)
            return mapping

        if not isinstance(raw_list, list):
            return mapping

        def first_value(*values: Any) -> Any:
            for value in values:
                if value is not None:
                    return value
            return ""

        for item in raw_list:
            if isinstance(item, dict):
                model_name = first_value(
                    item.get("model"),
                    item.get("模型"),
                    item.get("model_name"),
                    item.get("模型名"),
                )
                prompt_template = first_value(
                    item.get("prompt_template"),
                    item.get("提示词模板"),
                    item.get("template"),
                    item.get("模板"),
                    item.get("prompt"),
                    item.get("提示词"),
                )
                add_item(model_name, prompt_template)
            elif isinstance(item, str) and ":" in item:
                model_name, prompt_template = item.split(":", 1)
                add_item(model_name, prompt_template)

        return mapping

    def _get_model_prompt_template(self, model_name: str) -> Optional[str]:
        mapping = self._get_model_prompt_template_map()
        normalized_model = (model_name or "").strip()
        # 精确模型配置优先；ALL（必须全大写）作为所有模型的兜底配置。
        return mapping.get(normalized_model) or mapping.get("ALL")

    def _get_raw_model_parameter_entry_map(self) -> Dict[str, Dict[str, Any]]:
        """Return configured entries without filling defaults so entry presence remains meaningful."""
        raw_list = self.conf.get("model_parameter_list", [])
        entries: Dict[str, Dict[str, Any]] = {}

        def add_entry(model: Any, entry: Any) -> None:
            model_name = str(model or "").strip()
            if model_name and isinstance(entry, dict):
                entries[model_name] = entry

        if isinstance(raw_list, dict):
            for model_name, entry in raw_list.items():
                add_entry(model_name, entry)
        elif isinstance(raw_list, list):
            for entry in raw_list:
                if isinstance(entry, dict):
                    add_entry(
                        entry.get("model") or entry.get("模型") or entry.get("model_name") or entry.get("模型名"),
                        entry,
                    )
        return entries

    def _get_parameter_mode_from_entry(self, entry: Optional[Dict[str, Any]]) -> str:
        """Resolve the persisted mode, or infer one deterministically for legacy entries."""
        if not isinstance(entry, dict):
            return "none"
        known_modes = {mode for mode, _ in self.PARAMETER_MODES}
        configured_mode = ""
        if "parameter_mode" in entry:
            configured_mode = str(entry.get("parameter_mode") or "").strip().lower()
            if configured_mode and configured_mode not in known_modes:
                return "none"
            if configured_mode and configured_mode != "none":
                return configured_mode

        def is_enabled(*keys: str) -> bool:
            for key in keys:
                if key not in entry or entry[key] is None:
                    continue
                value = entry[key]
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return value != 0
                return str(value).strip().lower() in {
                    "1", "true", "yes", "on", "enable", "enabled", "是", "开启",
                }
            return False

        legacy_enable_fields = {
            "gpt": ("enable_gpt_parameters", "gpt_parameters", "GPT参数设置"),
            "gemini": ("enable_gemini_parameters", "gemini_parameters", "Gemini参数设置"),
            "grok": ("enable_grok_parameters", "grok_parameters", "Grok参数设置"),
            "seedream": ("enable_seedream_parameters", "seedream_parameters", "Seedream参数设置"),
        }
        for mode in ("gpt", "gemini", "grok", "seedream"):
            if is_enabled(*legacy_enable_fields[mode]):
                return mode
        return configured_mode if configured_mode in known_modes else "none"

    def _get_effective_model_parameters(
            self,
            source_model: str,
            actual_model: str,
    ) -> Optional[Dict[str, Any]]:
        """Target entries override wholesale; inherit source only when target has no entry."""
        raw_entries = self._get_raw_model_parameter_entry_map()
        actual_name = str(actual_model or "").strip()
        source_name = str(source_model or "").strip()
        parameter_model = actual_name if actual_name in raw_entries else source_name
        if parameter_model not in raw_entries:
            return None
        return self._get_model_parameter_map().get(parameter_model)

    def _get_model_parameter_map(self) -> Dict[str, Dict[str, Any]]:
        mapping: Dict[str, Dict[str, Any]] = {}
        raw_entries = self._get_raw_model_parameter_entry_map()
        raw_list = self.conf.get("model_parameter_list", [])

        def normalize_option(value: Any, allowed: set[str], default: str) -> str:
            normalized = str(value or default).strip().lower()
            return normalized if normalized in allowed else default

        def normalize_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            return str(value or "").strip().lower() in {
                "1", "true", "yes", "on", "enable", "enabled", "是", "开启",
            }

        def normalize_gpt_background(value: Any) -> str:
            normalized = str(value or "auto").strip().lower()
            return normalized if normalized in {"auto", "transparent", "opaque"} else "auto"

        def normalize_resolution(value: Any) -> str:
            normalized = str(value or "1K").strip().upper()
            return normalized if normalized in self.ADAPTIVE_RESOLUTION_LONG_EDGES else "1K"

        def normalize_gemini_resolution(value: Any) -> str:
            normalized = str(value or "auto").strip().upper()
            if normalized in {"", "AUTO", "AUTOMATIC", "自动"}:
                return "auto"
            return normalized if normalized in self.ADAPTIVE_RESOLUTION_LONG_EDGES else "auto"

        def normalize_gemini_aspect_ratio(value: Any) -> str:
            normalized = str(value or "auto").strip()
            if normalized.upper() in {"", "AUTO", "AUTOMATIC", "自动"}:
                return "auto"
            return normalized if normalized in self.GEMINI_ASPECT_RATIO_OPTIONS else "auto"

        def normalize_grok_resolution(value: Any) -> str:
            normalized = str(value or "2k").strip().lower()
            return normalized if normalized in self.GROK_RESOLUTION_OPTIONS else "2k"

        def normalize_seedream_resolution(value: Any) -> str:
            normalized = str(value or "1.5K").strip().upper()
            return normalized if normalized in self.SEEDREAM_RESOLUTION_OPTIONS else "1.5K"

        def normalize_seedream_output_format(value: Any) -> str:
            normalized = str(value or "png").strip().lower()
            return normalized if normalized in {"png", "jpeg"} else "png"

        def normalize_seedream_prompt_optimization(value: Any) -> str:
            normalized = str(value or "standard").strip().lower()
            return normalized if normalized in {"standard", "fast"} else "standard"

        def normalize_default_resolution(value: Any) -> str:
            return str(value or "auto").strip() or "auto"

        def get_value(item: Dict[str, Any], *keys: str, default: Any = None) -> Any:
            for key in keys:
                if key in item and item[key] is not None:
                    return item[key]
            return default

        def get_charge_value(
                item: Dict[str, Any],
                *keys: str,
                legacy_keys: Tuple[str, ...] = (),
                default: Any = 0.0,
        ) -> Any:
            """读取金额（元）配置；仅命中 legacy_keys（旧「次数」键）时按迁移汇率换算成元。"""
            for key in keys:
                if key in item and item[key] is not None:
                    return item[key]
            for key in legacy_keys:
                if key in item and item[key] is not None:
                    try:
                        return float(item[key]) * LEGACY_COUNT_TO_YUAN
                    except (TypeError, ValueError):
                        return default
            return default

        def add_item(
                model: Any,
                quality: Any = "auto",
                moderation: Any = "auto",
                gpt_background: Any = "auto",
                adaptive_aspect_ratio: Any = False,
                adaptive_resolution: Any = "1K",
                auto_upgrade_1k_adaptive_resolution: Any = False,
                default_resolution: Any = "auto",
                send_default_size: Any = False,
                max_output_tokens: Any = 0,
                charge_amount: Any = 1,
                charge_amount_2k: Any = 0,
                charge_amount_4k: Any = 0,
                deduct_on_violation: Any = False,
                force_resolution_limit: Any = False,
                enable_gpt_parameters: Any = False,
                omit_n_parameter: Any = False,
                enable_gemini_parameters: Any = False,
                gemini_resolution: Any = "auto",
                gemini_adaptive_aspect_ratio: Any = False,
                gemini_aspect_ratio: Any = "auto",
                reference_image_limit: Any = 0,
                extra_reference_image_quota: Any = 0,
                extra_reference_image_charge: Any = 1.0,
                enable_grok_parameters: Any = False,
                grok_resolution: Any = "2k",
                grok_adaptive_aspect_ratio: Any = False,
                enable_seedream_parameters: Any = False,
                seedream_web_search: Any = False,
                seedream_send_output_format: Any = False,
                seedream_output_format: Any = "png",
                seedream_watermark: Any = False,
                seedream_resolution: Any = "1.5K",
                seedream_send_aspect_ratio: Any = False,
                seedream_send_detailed_resolution: Any = False,
                seedream_pixel_limit: Any = 0,
                seedream_adaptive_aspect_ratio: Any = False,
                seedream_max_side_2000: Any = True,
                seedream_side_over_2000_auto_2k: Any = True,
                seedream_optimize_prompt_mode: Any = "standard",
        ):
            model_name = str(model or "").strip()
            if not model_name:
                return
            auto_upgrade_1k = normalize_bool(auto_upgrade_1k_adaptive_resolution)
            parameter_mode = self._get_parameter_mode_from_entry(raw_entries.get(model_name))
            enabled_field = self.PARAMETER_MODE_ENABLE_FIELDS.get(parameter_mode, "")
            mapping[model_name] = {
                "parameter_mode": parameter_mode,
                "quality": normalize_option(quality, self.IMAGE_QUALITY_OPTIONS, "auto"),
                "moderation": normalize_option(moderation, self.IMAGE_MODERATION_OPTIONS, "auto"),
                "gpt_background": normalize_gpt_background(gpt_background),
                "adaptive_aspect_ratio": normalize_bool(adaptive_aspect_ratio),
                "adaptive_resolution": normalize_resolution(adaptive_resolution),
                "auto_upgrade_1k_adaptive_resolution": auto_upgrade_1k,
                "default_resolution": normalize_default_resolution(default_resolution),
                "send_default_size": normalize_bool(send_default_size),
                "max_output_tokens": _normalize_nonnegative_int(max_output_tokens),
                "charge_amount": _normalize_charge_amount(charge_amount, 1.0),
                "charge_amount_2k": max(0, yuan_to_amount(charge_amount_2k)),
                "charge_amount_4k": max(0, yuan_to_amount(charge_amount_4k)),
                "deduct_on_violation": normalize_bool(deduct_on_violation),
                "force_resolution_limit": (
                    normalize_bool(force_resolution_limit) and not auto_upgrade_1k
                ),
                "enable_gpt_parameters": enabled_field == "enable_gpt_parameters",
                "omit_n_parameter": normalize_bool(omit_n_parameter),
                "enable_gemini_parameters": enabled_field == "enable_gemini_parameters",
                "gemini_resolution": normalize_gemini_resolution(gemini_resolution),
                "gemini_adaptive_aspect_ratio": normalize_bool(gemini_adaptive_aspect_ratio),
                "gemini_aspect_ratio": normalize_gemini_aspect_ratio(gemini_aspect_ratio),
                "reference_image_limit": _normalize_nonnegative_int(reference_image_limit),
                "extra_reference_image_quota": _normalize_nonnegative_int(extra_reference_image_quota),
                # 阶梯加费允许显式 0（每阶梯免费）；仅在字段缺失时回退默认 1 元
                "extra_reference_image_charge": max(0, yuan_to_amount(extra_reference_image_charge)),
                "enable_grok_parameters": enabled_field == "enable_grok_parameters",
                "grok_resolution": normalize_grok_resolution(grok_resolution),
                "grok_adaptive_aspect_ratio": normalize_bool(grok_adaptive_aspect_ratio),
                "enable_seedream_parameters": enabled_field == "enable_seedream_parameters",
                "seedream_web_search": normalize_bool(seedream_web_search),
                "seedream_send_output_format": normalize_bool(seedream_send_output_format),
                "seedream_output_format": normalize_seedream_output_format(seedream_output_format),
                "seedream_watermark": normalize_bool(seedream_watermark),
                "seedream_resolution": normalize_seedream_resolution(seedream_resolution),
                "seedream_send_aspect_ratio": normalize_bool(seedream_send_aspect_ratio),
                "seedream_send_detailed_resolution": normalize_bool(seedream_send_detailed_resolution),
                "seedream_pixel_limit": _normalize_nonnegative_int(seedream_pixel_limit),
                "seedream_adaptive_aspect_ratio": normalize_bool(seedream_adaptive_aspect_ratio),
                "seedream_max_side_2000": normalize_bool(seedream_max_side_2000),
                "seedream_side_over_2000_auto_2k": normalize_bool(seedream_side_over_2000_auto_2k),
                "seedream_optimize_prompt_mode": normalize_seedream_prompt_optimization(seedream_optimize_prompt_mode),
            }

        if isinstance(raw_list, dict):
            for model_name, parameters in raw_list.items():
                if isinstance(parameters, dict):
                    add_item(
                        model_name,
                        get_value(parameters, "quality", "质量", default="auto"),
                        get_value(parameters, "moderation", "审核", default="auto"),
                        get_value(parameters, "gpt_background", "GPT背景", "背景", default="auto"),
                        get_value(parameters, "adaptive_aspect_ratio", "自适应比例", default=False),
                        get_value(
                            parameters,
                            "adaptive_resolution",
                            "自适应比例分辨率",
                            "自适应分辨率",
                            default="1K",
                        ),
                        get_value(
                            parameters,
                            "auto_upgrade_1k_adaptive_resolution",
                            "1K超限自动转2K",
                            default=False,
                        ),
                        get_value(parameters, "default_resolution", "默认分辨率", default="auto"),
                        get_value(parameters, "send_default_size", "默认传递 size", default=False),
                        get_value(
                            parameters,
                            "max_output_tokens",
                            "最大输出思考Token",
                            "最大输出Token",
                            "思考Token限制",
                            default=0,
                        ),
                        get_charge_value(
                            parameters,
                            "charge_amount", "该模型单次扣费", "单次扣费", "扣费金额",
                            legacy_keys=("deduction_count", "该模型扣除次数", "扣除次数"),
                            default=1.0,
                        ),
                        get_charge_value(
                            parameters,
                            "charge_amount_2k", "2K单次扣费", "2K扣费",
                            legacy_keys=("deduction_count_2k", "2K扣除次数", "2K扣除"),
                            default=0.0,
                        ),
                        get_charge_value(
                            parameters,
                            "charge_amount_4k", "4K单次扣费", "4K扣费",
                            legacy_keys=("deduction_count_4k", "4K扣除次数", "4K扣除"),
                            default=0.0,
                        ),
                        get_value(
                            parameters,
                            "deduct_on_violation",
                            "违规是否扣费",
                            "违规是否扣次数",
                            "违规扣次",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "force_resolution_limit",
                            "强制限制分辨率",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "enable_gpt_parameters",
                            "gpt_parameters",
                            "GPT参数设置",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "omit_n_parameter",
                            "不传递 n 参数",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "enable_gemini_parameters",
                            "gemini_parameters",
                            "Gemini参数设置",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "gemini_resolution",
                            "Gemini分辨率",
                            default="auto",
                        ),
                        get_value(
                            parameters,
                            "gemini_adaptive_aspect_ratio",
                            "Gemini自适应比例",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "gemini_aspect_ratio",
                            "Gemini图片比例",
                            default="auto",
                        ),
                        get_value(parameters, "reference_image_limit", "参考图数量限制", default=0),
                        get_value(parameters, "extra_reference_image_quota", "超限参考图阶梯额度", default=0),
                        get_value(parameters, "extra_reference_image_charge", "超限参考图每阶梯加费", "阶梯加费金额", default=0),
                        get_value(
                            parameters,
                            "enable_grok_parameters",
                            "grok_parameters",
                            "Grok参数设置",
                            default=False,
                        ),
                        get_value(parameters, "grok_resolution", "Grok分辨率", default="2k"),
                        get_value(
                            parameters,
                            "grok_adaptive_aspect_ratio",
                            "Grok自适应比例",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "enable_seedream_parameters",
                            "seedream_parameters",
                            "Seedream参数设置",
                            default=False,
                        ),
                        get_value(parameters, "seedream_web_search", "Seedream联网搜索", default=False),
                        get_value(
                            parameters,
                            "seedream_send_output_format",
                            "Seedream传递输出格式",
                            default=False,
                        ),
                        get_value(parameters, "seedream_output_format", "Seedream输出格式", default="png"),
                        get_value(parameters, "seedream_watermark", "Seedream添加水印", default=False),
                        get_value(parameters, "seedream_resolution", "Seedream分辨率", default="1.5K"),
                        get_value(
                            parameters,
                            "seedream_send_aspect_ratio",
                            "Seedream传递比例",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "seedream_send_detailed_resolution",
                            "Seedream传递详细分辨率",
                            default=False,
                        ),
                        get_value(parameters, "seedream_pixel_limit", "Seedream像素数上限", default=0),
                        get_value(
                            parameters,
                            "seedream_adaptive_aspect_ratio",
                            "Seedream自适应比例",
                            default=False,
                        ),
                        get_value(
                            parameters,
                            "seedream_max_side_2000",
                            "Seedream宽高均不超过2000",
                            "Seedream最大边长不超过2000",
                            default=True,
                        ),
                        get_value(
                            parameters,
                            "seedream_side_over_2000_auto_2k",
                            "边长超2000自动升2K",
                            "边长超过2000自动升级2K",
                            default=True,
                        ),
                        get_value(
                            parameters,
                            "seedream_optimize_prompt_mode",
                            "Seedream提示词优化模式",
                            default="standard",
                        ),
                    )
            return mapping

        if not isinstance(raw_list, list):
            return mapping

        for item in raw_list:
            if not isinstance(item, dict):
                continue
            add_item(
                item.get("model") or item.get("模型") or item.get("model_name") or item.get("模型名"),
                get_value(item, "quality", "质量", default="auto"),
                get_value(item, "moderation", "审核", default="auto"),
                get_value(item, "gpt_background", "GPT背景", "背景", default="auto"),
                get_value(item, "adaptive_aspect_ratio", "自适应比例", default=False),
                get_value(
                    item,
                    "adaptive_resolution",
                    "自适应比例分辨率",
                    "自适应分辨率",
                    default="1K",
                ),
                get_value(
                    item,
                    "auto_upgrade_1k_adaptive_resolution",
                    "1K超限自动转2K",
                    default=False,
                ),
                get_value(item, "default_resolution", "默认分辨率", default="auto"),
                get_value(item, "send_default_size", "默认传递 size", default=False),
                get_value(
                    item,
                    "max_output_tokens",
                    "最大输出思考Token",
                    "最大输出Token",
                    "思考Token限制",
                    default=0,
                ),
                get_charge_value(
                    item,
                    "charge_amount", "该模型单次扣费", "单次扣费", "扣费金额",
                    legacy_keys=("deduction_count", "该模型扣除次数", "扣除次数"),
                    default=1.0,
                ),
                get_charge_value(
                    item,
                    "charge_amount_2k", "2K单次扣费", "2K扣费",
                    legacy_keys=("deduction_count_2k", "2K扣除次数", "2K扣除"),
                    default=0.0,
                ),
                get_charge_value(
                    item,
                    "charge_amount_4k", "4K单次扣费", "4K扣费",
                    legacy_keys=("deduction_count_4k", "4K扣除次数", "4K扣除"),
                    default=0.0,
                ),
                get_value(
                    item,
                    "deduct_on_violation",
                    "违规是否扣费",
                    "违规是否扣次数",
                    "违规扣次",
                    default=False,
                ),
                get_value(
                    item,
                    "force_resolution_limit",
                    "强制限制分辨率",
                    default=False,
                ),
                get_value(
                    item,
                    "enable_gpt_parameters",
                    "gpt_parameters",
                    "GPT参数设置",
                    default=False,
                ),
                get_value(
                    item,
                    "omit_n_parameter",
                    "不传递 n 参数",
                    default=False,
                ),
                get_value(
                    item,
                    "enable_gemini_parameters",
                    "gemini_parameters",
                    "Gemini参数设置",
                    default=False,
                ),
                get_value(
                    item,
                    "gemini_resolution",
                    "Gemini分辨率",
                    default="auto",
                ),
                get_value(
                    item,
                    "gemini_adaptive_aspect_ratio",
                    "Gemini自适应比例",
                    default=False,
                ),
                    get_value(
                    item,
                    "gemini_aspect_ratio",
                    "Gemini图片比例",
                    default="auto",
                ),
                get_value(item, "reference_image_limit", "参考图数量限制", default=0),
                get_value(item, "extra_reference_image_quota", "超限参考图阶梯额度", default=0),
                get_value(item, "extra_reference_image_charge", "超限参考图每阶梯加费", "阶梯加费金额", default=0),
                get_value(
                    item,
                    "enable_grok_parameters",
                    "grok_parameters",
                    "Grok参数设置",
                    default=False,
                ),
                get_value(item, "grok_resolution", "Grok分辨率", default="2k"),
                get_value(
                    item,
                    "grok_adaptive_aspect_ratio",
                    "Grok自适应比例",
                    default=False,
                ),
                get_value(
                    item,
                    "enable_seedream_parameters",
                    "seedream_parameters",
                    "Seedream参数设置",
                    default=False,
                ),
                get_value(item, "seedream_web_search", "Seedream联网搜索", default=False),
                get_value(
                    item,
                    "seedream_send_output_format",
                    "Seedream传递输出格式",
                    default=False,
                ),
                get_value(item, "seedream_output_format", "Seedream输出格式", default="png"),
                get_value(item, "seedream_watermark", "Seedream添加水印", default=False),
                get_value(item, "seedream_resolution", "Seedream分辨率", default="1.5K"),
                get_value(
                    item,
                    "seedream_send_aspect_ratio",
                    "Seedream传递比例",
                    default=False,
                ),
                get_value(
                    item,
                    "seedream_send_detailed_resolution",
                    "Seedream传递详细分辨率",
                    default=False,
                ),
                get_value(item, "seedream_pixel_limit", "Seedream像素数上限", default=0),
                get_value(
                    item,
                    "seedream_adaptive_aspect_ratio",
                    "Seedream自适应比例",
                    default=False,
                ),
                get_value(
                    item,
                    "seedream_max_side_2000",
                    "Seedream宽高均不超过2000",
                    "Seedream最大边长不超过2000",
                    default=True,
                ),
                get_value(
                    item,
                    "seedream_side_over_2000_auto_2k",
                    "边长超2000自动升2K",
                    "边长超过2000自动升级2K",
                    default=True,
                ),
                get_value(
                    item,
                    "seedream_optimize_prompt_mode",
                    "Seedream提示词优化模式",
                    default="standard",
                ),
            )

        return mapping

    def _parameters_for_request(
            self,
            model_name: str,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if parameters is not None:
            return parameters
        return self._get_model_parameter_map().get((model_name or "").strip())

    def _get_model_parameters(
            self,
            model_name: str,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        parameters = self._parameters_for_request(model_name, parameters)
        if not parameters or not parameters.get("enable_gpt_parameters"):
            return {}
        result = {
            "quality": parameters["quality"],
            "moderation": parameters["moderation"],
        }
        background = parameters.get("gpt_background") or "auto"
        if background != "auto":
            result["background"] = background
            if background == "transparent":
                # 透明背景仅支持 png/webp 输出，显式锁定 png，避免网关默认 jpeg
                result["output_format"] = "png"
        return result

    def _get_max_output_tokens(
            self,
            model_name: str,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> int:
        parameters = self._parameters_for_request(model_name, parameters)
        model_limit = _normalize_nonnegative_int((parameters or {}).get("max_output_tokens", 0))
        if model_limit:
            return model_limit
        return _normalize_nonnegative_int(
            self.conf.get("max_output_tokens", self.conf.get("gemini_max_output_tokens", 0))
        )

    def _get_seedream_parameters(
            self,
            model_name: str,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        parameters = self._parameters_for_request(model_name, parameters)
        if not parameters or not parameters.get("enable_seedream_parameters"):
            return None
        return parameters

    def _get_reference_image_limit(
            self,
            model_name: str,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> int:
        global_limit = _normalize_positive_int(self.conf.get("max_images_count", 10), 10)
        parameters = self._parameters_for_request(model_name, parameters)
        model_limit = _normalize_nonnegative_int((parameters or {}).get("reference_image_limit", 0))
        return min(model_limit, global_limit) if model_limit else global_limit

    def _limit_reference_images(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            parameters: Optional[Dict[str, Any]] = None,
    ) -> List[bytes]:
        global_limit = _normalize_positive_int(self.conf.get("max_images_count", 10), 10)
        parameters = self._parameters_for_request(model_name, parameters)
        configured_limit = _normalize_nonnegative_int((parameters or {}).get("reference_image_limit", 0))
        quota = _normalize_nonnegative_int((parameters or {}).get("extra_reference_image_quota", 0))
        if configured_limit > 0 and quota > 0:
            return image_bytes_list[:global_limit]
        return image_bytes_list[:self._get_reference_image_limit(model_name, parameters)]

    def _get_extra_reference_image_charge(
            self,
            model_name: str,
            image_bytes_list: Optional[List[bytes]],
            parameters: Optional[Dict[str, Any]] = None,
    ) -> int:
        """超限参考图阶梯额外加费（厘，不含基础扣费）。

        每阶梯额外加收 extra_reference_image_charge（默认 0，即超出阶梯不额外加费），与模型
        charge_amount、分辨率升级均无关。仅当 reference_image_limit>0 且
        extra_reference_image_quota>0 时启用；二者任一为 0 则本功能完全不触发（返回 0）。
        """
        if not image_bytes_list:
            return 0
        parameters = self._parameters_for_request(model_name, parameters)
        if not parameters:
            return 0
        configured_limit = _normalize_nonnegative_int(parameters.get("reference_image_limit", 0))
        quota = _normalize_nonnegative_int(parameters.get("extra_reference_image_quota", 0))
        if configured_limit <= 0 or quota <= 0:
            return 0
        global_limit = _normalize_positive_int(self.conf.get("max_images_count", 10), 10)
        soft_limit = min(configured_limit, global_limit)
        sent = min(len(image_bytes_list), global_limit)  # 与实际发送数量保持一致
        excess = sent - soft_limit
        if excess <= 0:
            return 0
        steps = (excess + quota - 1) // quota  # 阶梯数（向上取整）
        raw_charge = parameters.get("extra_reference_image_charge")
        # 字段缺失（未归一化的旧参数）回退默认 1 元/阶梯；显式 0 表示阶梯免费
        charge_per_step = _normalize_nonnegative_int(parameters.get("extra_reference_image_charge"))
        return steps * charge_per_step

    def _get_max_reference_image_side(self, image_bytes_list: Optional[List[bytes]]) -> int:
        """遍历所有参考图，返回任一边长的最大值；读取失败跳过；无图返回 0。"""
        if not image_bytes_list:
            return 0
        max_side = 0
        for image_bytes in image_bytes_list:
            try:
                with PILImage.open(io.BytesIO(image_bytes)) as image:
                    width, height = image.size
            except Exception as exc:
                logger.warning(f"读取参考图尺寸失败，跳过边长判定: {exc}")
                continue
            if width > 0 and height > 0:
                max_side = max(max_side, width, height)
        return max_side

    def _get_resolution_charge_tier(
            self,
            model_name: str,
            resolution: Optional[str] = None,
            image_bytes_list: Optional[List[bytes]] = None,
            parameters: Optional[Dict[str, Any]] = None,
            aspect_ratio: Optional[str] = None,
    ) -> Optional[str]:
        """判定本次请求应使用的分辨率扣费档位。

        4K > 2K > 无。4K 档由模型分辨率设置或命令 x4 触发（暂不按边长检测）；
        2K 档由参考图任一边长 > 2000、模型分辨率设置 2K、命令 x2，
        或「边长超2000自动升2K」开启时 Seedream 详细分辨率的自适应结果触发。
        任一条件满足即命中对应档位，取最高档。
        """
        parameters = self._parameters_for_request(model_name, parameters)
        configured_resolution = str((parameters or {}).get("adaptive_resolution") or "1K").upper()
        requested_resolution = str(resolution or "").strip().upper()

        if configured_resolution == "4K" or requested_resolution == "4K":
            return "4K"
        if (
                configured_resolution == "2K"
                or requested_resolution == "2K"
                or self._get_max_reference_image_side(image_bytes_list) > 2000
                or self._seedream_side_upgrade_hits_2k(
                    model_name,
                    image_bytes_list,
                    aspect_ratio,
                    parameters,
                )
        ):
            return "2K"
        return None

    def _seedream_side_upgrade_hits_2k(
            self,
            model_name: str,
            image_bytes_list: Optional[List[bytes]],
            aspect_ratio: Optional[str],
            parameters: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """判断 Seedream 详细分辨率本次是否会因边长超限而升级为 2K 尺寸。

        与 _build_seedream_adaptive_size 的升级条件保持一致：
        「Seedream参数设置 + 传递详细分辨率 + 边长超2000自动升2K」均开启、
        有比例来源（命令 =宽:高 或首张参考图），且所选档位尺寸任一边超过
        宽高上限（像素数上限容纳不下 2K 档时不升级）。
        """
        seedream_parameters = self._get_seedream_parameters(model_name, parameters)
        if not seedream_parameters:
            return False
        if not seedream_parameters.get("seedream_send_detailed_resolution"):
            return False
        if not seedream_parameters.get("seedream_side_over_2000_auto_2k", True):
            return False
        selected_ratio = self._get_seedream_selected_aspect_ratio(image_bytes_list, aspect_ratio)
        if not selected_ratio:
            return False
        resolution = str(seedream_parameters.get("seedream_resolution") or "1.5K").upper()
        if resolution not in self.SEEDREAM_RESOLUTION_ORDER:
            resolution = "1.5K"
        width, height = self._parse_seedream_size(
            self.SEEDREAM_SIZE_OPTIONS[resolution][selected_ratio]
        )
        if max(width, height) <= self.SEEDREAM_ADAPTIVE_MAX_SIDE:
            return False
        pixel_limit = _normalize_nonnegative_int(seedream_parameters.get("seedream_pixel_limit", 0)) * 1000
        if pixel_limit:
            upgrade_width, upgrade_height = self._parse_seedream_size(
                self.SEEDREAM_SIZE_OPTIONS["2K"][selected_ratio]
            )
            if upgrade_width * upgrade_height > pixel_limit:
                return False
        return True

    def _global_charge_amount(self, key: str, default_yuan: float) -> int:
        """全局档位扣费配置（元）→ 厘；无效或 ≤0 时回退默认值。"""
        amount = yuan_to_amount(self.conf.get(key, default_yuan))
        return amount if amount > 0 else yuan_to_amount(default_yuan)

    def _get_tiered_charge_amount(
            self,
            model_name: str,
            tier: Optional[str],
            parameters: Optional[Dict[str, Any]] = None,
    ) -> int:
        """按档位返回单次生成扣费（厘，1厘=0.001元）：命中 2K/4K 时替换基础扣费。

        模型「2K/4K单次扣费」>0 时使用模型配置，否则回退全局
        resolution_2k_cost / resolution_4k_cost（默认 2 / 4 元）。
        """
        parameters = self._parameters_for_request(model_name, parameters)
        if tier == "4K":
            model_cost = _normalize_nonnegative_int((parameters or {}).get("charge_amount_4k", 0))
            if model_cost > 0:
                return model_cost
            return self._global_charge_amount("resolution_4k_cost", 4)
        if tier == "2K":
            model_cost = _normalize_nonnegative_int((parameters or {}).get("charge_amount_2k", 0))
            if model_cost > 0:
                return model_cost
            return self._global_charge_amount("resolution_2k_cost", 2)
        base_cost = _normalize_nonnegative_int((parameters or {}).get("charge_amount", 0))
        return base_cost if base_cost > 0 else yuan_to_amount(1.0)

    @classmethod
    def _get_nearest_gemini_aspect_ratio(
            cls,
            width: float,
            height: float,
    ) -> Optional[str]:
        if width <= 0 or height <= 0:
            return None

        source_ratio = width / height

        def distance(candidate: str) -> float:
            parsed = cls._parse_aspect_ratio(candidate)
            if not parsed:
                return float("inf")
            candidate_width, candidate_height = parsed
            return abs(math.log(source_ratio / (candidate_width / candidate_height)))

        return min(cls.GEMINI_ASPECT_RATIO_ORDER, key=distance)

    def _get_gemini_adaptive_aspect_ratio(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            aspect_ratio: Optional[str] = None,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        parameters = self._parameters_for_request(model_name, parameters)
        if (
                not parameters
                or not parameters.get("enable_gemini_parameters")
                or not parameters.get("gemini_adaptive_aspect_ratio")
                or not image_bytes_list
        ):
            return None

        parsed_ratio = self._parse_aspect_ratio(aspect_ratio)
        if parsed_ratio:
            source_width, source_height = parsed_ratio
            source_label = aspect_ratio
        else:
            try:
                with PILImage.open(io.BytesIO(image_bytes_list[0])) as image:
                    source_width, source_height = image.size
            except Exception as e:
                logger.warning(f"读取首图尺寸失败，跳过 Gemini 自适应比例: {e}")
                return None
            source_label = f"{source_width}:{source_height}"

        selected_ratio = self._get_nearest_gemini_aspect_ratio(source_width, source_height)
        if selected_ratio:
            logger.info(
                f"Gemini 自适应比例: model={model_name}, source={source_label}, "
                f"aspectRatio={selected_ratio}"
            )
        return selected_ratio

    def _get_gemini_image_config(
            self,
            model_name: str,
            image_bytes_list: Optional[List[bytes]] = None,
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        parameters = self._parameters_for_request(model_name, parameters)
        if not parameters or not parameters.get("enable_gemini_parameters"):
            return {}

        image_config: Dict[str, str] = {}
        if resolution := parameters.get("gemini_resolution"):
            if resolution in self.ADAPTIVE_RESOLUTION_LONG_EDGES:
                image_config["imageSize"] = resolution

        parsed_aspect_ratio = self._parse_aspect_ratio(aspect_ratio)
        if force_aspect_ratio and parsed_aspect_ratio:
            selected_aspect_ratio = self._get_nearest_gemini_aspect_ratio(*parsed_aspect_ratio)
            if selected_aspect_ratio:
                image_config["aspectRatio"] = selected_aspect_ratio
        else:
            adaptive_aspect_ratio = self._get_gemini_adaptive_aspect_ratio(
                model_name,
                image_bytes_list or [],
                aspect_ratio,
                parameters,
            )
            if adaptive_aspect_ratio:
                image_config["aspectRatio"] = adaptive_aspect_ratio
            elif configured_aspect_ratio := parameters.get("gemini_aspect_ratio"):
                if configured_aspect_ratio in self.GEMINI_ASPECT_RATIO_OPTIONS:
                    image_config["aspectRatio"] = configured_aspect_ratio

        return image_config

    @classmethod
    def _align_adaptive_dimension(cls, value: float, mode: str = "nearest") -> int:
        units = value / cls.ADAPTIVE_SIZE_ALIGNMENT
        if mode == "ceil":
            aligned_units = math.ceil(units)
        elif mode == "floor":
            aligned_units = math.floor(units)
        else:
            aligned_units = round(units)
        return max(cls.ADAPTIVE_SIZE_ALIGNMENT, aligned_units * cls.ADAPTIVE_SIZE_ALIGNMENT)

    @staticmethod
    def _parse_aspect_ratio(value: Any) -> Optional[Tuple[float, float]]:
        """解析宽:高比例，兼容半角和全角冒号。"""
        match = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)\s*",
            str(value or ""),
        )
        if not match:
            return None
        width, height = float(match.group(1)), float(match.group(2))
        if width <= 0 or height <= 0:
            return None
        return width, height

    def _calculate_adaptive_image_size(
            self,
            source_width: int,
            source_height: int,
            resolution: str,
            aspect_ratio: Optional[str] = None,
            force_resolution_limit: bool = False,
    ) -> str:
        parsed_ratio = self._parse_aspect_ratio(aspect_ratio)
        if parsed_ratio:
            ratio_width, ratio_height = parsed_ratio
            is_landscape = ratio_width >= ratio_height
            source_ratio = max(ratio_width / ratio_height, ratio_height / ratio_width)
        else:
            is_landscape = source_width >= source_height
            source_ratio = max(source_width, source_height) / min(source_width, source_height)
        target_ratio = min(source_ratio, self.ADAPTIVE_MAX_ASPECT_RATIO)
        target_long_edge = self.ADAPTIVE_RESOLUTION_LONG_EDGES[resolution]
        target_short_edge = target_long_edge / target_ratio
        target_pixels = target_long_edge * target_short_edge
        alignment_mode = "nearest"

        if target_pixels < self.ADAPTIVE_MIN_PIXELS:
            scale = math.sqrt(self.ADAPTIVE_MIN_PIXELS / target_pixels)
            target_long_edge *= scale
            target_short_edge *= scale
            alignment_mode = "ceil"
        elif target_pixels > self.ADAPTIVE_MAX_PIXELS:
            scale = math.sqrt(self.ADAPTIVE_MAX_PIXELS / target_pixels)
            target_long_edge *= scale
            target_short_edge *= scale
            alignment_mode = "floor"

        long_edge = self._align_adaptive_dimension(target_long_edge, alignment_mode)
        short_edge = self._align_adaptive_dimension(target_short_edge, alignment_mode)

        if long_edge * short_edge < self.ADAPTIVE_MIN_PIXELS:
            scale = math.sqrt(self.ADAPTIVE_MIN_PIXELS / (long_edge * short_edge))
            long_edge = self._align_adaptive_dimension(long_edge * scale, "ceil")
            short_edge = self._align_adaptive_dimension(short_edge * scale, "ceil")
        elif long_edge * short_edge > self.ADAPTIVE_MAX_PIXELS:
            scale = math.sqrt(self.ADAPTIVE_MAX_PIXELS / (long_edge * short_edge))
            long_edge = self._align_adaptive_dimension(long_edge * scale, "floor")
            short_edge = self._align_adaptive_dimension(short_edge * scale, "floor")

        if force_resolution_limit:
            max_long_edge = self.ADAPTIVE_RESOLUTION_LONG_EDGES[resolution]
            if long_edge > max_long_edge:
                long_edge = max_long_edge
                short_edge = max(
                    self._align_adaptive_dimension(long_edge / target_ratio),
                    self._align_adaptive_dimension(
                        self.ADAPTIVE_MIN_PIXELS / long_edge,
                        "ceil",
                    ),
                )

        if source_ratio > self.ADAPTIVE_MAX_ASPECT_RATIO:
            ratio_label = aspect_ratio or f"{source_width}:{source_height}"
            ratio_subject = "目标比例" if aspect_ratio else "首图比例"
            logger.warning(
                f"{ratio_subject} {ratio_label} 超过自适应 size 的 3:1 限制，"
                "已按最大 3:1 比例计算 size"
            )

        target_width, target_height = (
            (long_edge, short_edge) if is_landscape else (short_edge, long_edge)
        )
        return f"{target_width}x{target_height}"

    @staticmethod
    def _adaptive_size_exceeds_long_edge(size: str, long_edge_limit: int) -> bool:
        try:
            width, height = (int(dimension) for dimension in size.lower().split("x", 1))
        except (AttributeError, TypeError, ValueError):
            return False
        return max(width, height) > long_edge_limit

    def _get_adaptive_image_size_details(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            resolution: Optional[str],
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Optional[Tuple[str, str, bool]]:
        parameters = self._parameters_for_request(model_name, parameters)
        normalized_resolution = str(resolution or "").strip().upper()
        if (
                not parameters
                or not parameters.get("enable_gpt_parameters")
                or not parameters.get("adaptive_aspect_ratio")
                or (not image_bytes_list and not (force_aspect_ratio and self._parse_aspect_ratio(aspect_ratio)))
        ):
            return None

        if normalized_resolution not in self.ADAPTIVE_RESOLUTION_LONG_EDGES:
            normalized_resolution = str(parameters.get("adaptive_resolution") or "1K").upper()

        source_width = 1
        source_height = 1
        if not (force_aspect_ratio and self._parse_aspect_ratio(aspect_ratio)):
            try:
                with PILImage.open(io.BytesIO(image_bytes_list[0])) as image:
                    source_width, source_height = image.size
            except Exception as e:
                logger.warning(f"读取首图尺寸失败，跳过自适应比例参数: {e}")
                return None

            if source_width <= 0 or source_height <= 0:
                logger.warning("首图宽高无效，跳过自适应比例参数")
                return None

        size = self._calculate_adaptive_image_size(
            source_width,
            source_height,
            normalized_resolution,
            aspect_ratio=aspect_ratio,
            force_resolution_limit=bool(parameters.get("force_resolution_limit")),
        )
        auto_upgraded_to_2k = (
            normalized_resolution == "1K"
            and parameters.get("auto_upgrade_1k_adaptive_resolution")
            and self._adaptive_size_exceeds_long_edge(
                size,
                self.ADAPTIVE_RESOLUTION_LONG_EDGES["1K"],
            )
        )
        effective_resolution = "2K" if auto_upgraded_to_2k else normalized_resolution
        if auto_upgraded_to_2k:
            size = self._calculate_adaptive_image_size(
                source_width,
                source_height,
                effective_resolution,
                aspect_ratio=aspect_ratio,
                force_resolution_limit=bool(parameters.get("force_resolution_limit")),
            )

        source_label = aspect_ratio if force_aspect_ratio and aspect_ratio else f"{source_width}x{source_height}"
        logger.info(
            f"自适应比例参数: model={model_name}, source={source_label}, "
            f"requested_resolution={normalized_resolution}, resolution={effective_resolution}, "
            f"auto_upgraded_to_2k={auto_upgraded_to_2k}, "
            f"aspect_ratio={aspect_ratio or 'source'}, "
            f"force_resolution_limit={bool(parameters.get('force_resolution_limit'))}, size={size}"
        )
        return size, effective_resolution, auto_upgraded_to_2k

    def _get_adaptive_image_size(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            resolution: Optional[str],
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        details = self._get_adaptive_image_size_details(
            model_name,
            image_bytes_list,
            resolution,
            aspect_ratio,
            force_aspect_ratio,
            parameters,
        )
        return details[0] if details else None

    def _should_omit_n_parameter(
            self,
            model_name: str,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> bool:
        parameters = self._parameters_for_request(model_name, parameters)
        return bool(
            parameters
            and parameters.get("enable_gpt_parameters")
            and parameters.get("omit_n_parameter")
        )

    @classmethod
    def _get_nearest_grok_aspect_ratio(cls, width: float, height: float) -> Optional[str]:
        if width <= 0 or height <= 0:
            return None

        source_ratio = width / height

        def distance(candidate: str) -> float:
            candidate_ratio = cls._parse_aspect_ratio(candidate)
            if not candidate_ratio:
                return float("inf")
            candidate_width, candidate_height = candidate_ratio
            return abs(math.log(source_ratio / (candidate_width / candidate_height)))

        return min(cls.GROK_ASPECT_RATIO_ORDER, key=distance)

    def _get_grok_request_parameters(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        parameters = self._parameters_for_request(model_name, parameters)
        if not parameters or not parameters.get("enable_grok_parameters"):
            return {}

        request = {"resolution": parameters["grok_resolution"], "aspect_ratio": "auto"}
        if not parameters.get("grok_adaptive_aspect_ratio"):
            return request

        parsed_ratio = self._parse_aspect_ratio(aspect_ratio)
        if parsed_ratio and (force_aspect_ratio or image_bytes_list):
            source_width, source_height = parsed_ratio
            source_label = aspect_ratio
        elif image_bytes_list:
            try:
                with PILImage.open(io.BytesIO(image_bytes_list[0])) as image:
                    source_width, source_height = image.size
            except Exception as exc:
                logger.warning(f"读取 Grok 首图尺寸失败，使用 aspect_ratio=auto: {exc}")
                return request
            source_label = f"{source_width}:{source_height}"
        else:
            return request

        selected_ratio = self._get_nearest_grok_aspect_ratio(source_width, source_height)
        if selected_ratio:
            request["aspect_ratio"] = selected_ratio
            logger.info(
                f"Grok 自适应比例: model={model_name}, source={source_label}, "
                f"aspect_ratio={selected_ratio}"
            )
        return request

    def _get_seedream_source_aspect_ratio(
            self,
            image_bytes_list: List[bytes],
            aspect_ratio: Optional[str],
            fallback_to_square: bool = True,
    ) -> Optional[float]:
        parsed_ratio = self._parse_aspect_ratio(aspect_ratio)
        if parsed_ratio:
            return parsed_ratio[0] / parsed_ratio[1]
        if image_bytes_list:
            try:
                with PILImage.open(io.BytesIO(image_bytes_list[0])) as image:
                    return image.width / image.height
            except Exception as exc:
                fallback = "使用 1:1" if fallback_to_square else "不传递比例"
                logger.warning(f"读取 Seedream 首图尺寸失败，{fallback}: {exc}")
        return 1.0 if fallback_to_square else None

    @classmethod
    def _get_nearest_seedream_aspect_ratio(cls, source_ratio: float) -> str:
        def distance(candidate: str) -> float:
            width, height = cls._parse_aspect_ratio(candidate) or (1.0, 1.0)
            return abs(math.log(source_ratio / (width / height)))

        return min(cls.SEEDREAM_ASPECT_RATIO_ORDER, key=distance)

    def _get_seedream_selected_aspect_ratio(
            self,
            image_bytes_list: List[bytes],
            aspect_ratio: Optional[str],
    ) -> Optional[str]:
        source_ratio = self._get_seedream_source_aspect_ratio(
            image_bytes_list,
            aspect_ratio,
            fallback_to_square=False,
        )
        if source_ratio is None:
            return None
        return self._get_nearest_seedream_aspect_ratio(source_ratio)

    @classmethod
    def _parse_seedream_size(cls, value: str) -> Tuple[int, int]:
        """解析尺寸表中的 "宽x高" 字符串。"""
        width_text, height_text = str(value).split("x", 1)
        return int(width_text), int(height_text)

    def _build_seedream_adaptive_size(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            aspect_ratio: Optional[str],
            parameters: Optional[Dict[str, Any]] = None,
    ) -> str:
        parameters = self._get_seedream_parameters(model_name, parameters) or {}
        requested_resolution = str(parameters.get("seedream_resolution") or "1.5K").upper()
        if requested_resolution not in self.SEEDREAM_RESOLUTION_ORDER:
            logger.warning(
                f"Seedream 自适应尺寸表不支持 {requested_resolution}，已按 1.5K 生成"
            )
            requested_resolution = "1.5K"

        source_ratio = self._get_seedream_source_aspect_ratio(image_bytes_list, aspect_ratio)
        selected_ratio = self._get_nearest_seedream_aspect_ratio(source_ratio)
        # Seedream 价格表的 K px 使用十进制换算，例如 2360K = 2,360,000 px。
        pixel_limit = _normalize_nonnegative_int(parameters.get("seedream_pixel_limit", 0)) * 1000
        max_side_limit = (
            self.SEEDREAM_ADAPTIVE_MAX_SIDE
            if parameters.get("seedream_max_side_2000", True)
            else 0
        )
        available_resolutions = self.SEEDREAM_RESOLUTION_ORDER
        requested_index = available_resolutions.index(requested_resolution)

        side_auto_upgrade = (
            bool(parameters.get("seedream_side_over_2000_auto_2k", True))
            and bool(max_side_limit)
        )
        if side_auto_upgrade:
            width, height = self._parse_seedream_size(
                self.SEEDREAM_SIZE_OPTIONS[requested_resolution][selected_ratio]
            )
            if max(width, height) > max_side_limit:
                upgrade_width, upgrade_height = self._parse_seedream_size(
                    self.SEEDREAM_SIZE_OPTIONS["2K"][selected_ratio]
                )
                if not pixel_limit or upgrade_width * upgrade_height <= pixel_limit:
                    logger.info(
                        f"Seedream 尺寸边长超 {max_side_limit}px，自动升级 2K: "
                        f"model={model_name}, {width}x{height} -> "
                        f"{upgrade_width}x{upgrade_height}"
                    )
                    return self.SEEDREAM_SIZE_OPTIONS["2K"][selected_ratio]
                logger.info(
                    f"Seedream 边长超 {max_side_limit}px 但 2K 档超出像素数上限 "
                    f"{pixel_limit // 1000}K，维持降档逻辑: model={model_name}"
                )

        allowed_resolutions = available_resolutions[:requested_index + 1]
        if pixel_limit or max_side_limit:
            fitting_resolutions = []
            max_side_limited_resolutions = []
            for resolution in allowed_resolutions:
                width, height = (
                    int(dimension)
                    for dimension in self.SEEDREAM_SIZE_OPTIONS[resolution][selected_ratio].split("x", 1)
                )
                exceeds_max_side = bool(max_side_limit and max(width, height) > max_side_limit)
                if exceeds_max_side:
                    max_side_limited_resolutions.append(resolution)
                if (
                        (not pixel_limit or width * height <= pixel_limit)
                        and not exceeds_max_side
                ):
                    fitting_resolutions.append(resolution)
            if fitting_resolutions:
                requested_resolution = fitting_resolutions[-1]
            else:
                limits = []
                if pixel_limit:
                    limits.append(f"像素数上限 {pixel_limit // 1000}K")
                if max_side_limit:
                    limits.append(f"宽高上限 {max_side_limit} px")
                logger.warning(
                    f"Seedream {'、'.join(limits)} 下没有符合 {selected_ratio} 的候选尺寸，已使用 1K"
                )
                requested_resolution = "1K"
            if max_side_limited_resolutions:
                logger.info(
                    f"Seedream 宽高上限 {max_side_limit} px 排除了 {selected_ratio} 的 "
                    f"{', '.join(max_side_limited_resolutions)} 档位"
                )

        size = self.SEEDREAM_SIZE_OPTIONS[requested_resolution][selected_ratio]
        logger.info(
            f"Seedream 自适应尺寸: model={model_name}, source_ratio={source_ratio:.4f}, "
            f"resolution={requested_resolution}, aspect_ratio={selected_ratio}, size={size}"
        )
        return size

    def _get_seedream_request_parameters(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            aspect_ratio: Optional[str],
            force_aspect_ratio: bool,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        parameters = self._get_seedream_parameters(model_name, parameters)
        if not parameters:
            return {}

        request: Dict[str, Any] = {
            "watermark": parameters["seedream_watermark"],
        }
        if parameters["seedream_send_output_format"]:
            request["output_format"] = parameters["seedream_output_format"]
        request["optimize_prompt_options"] = {
            "mode": parameters["seedream_optimize_prompt_mode"],
        }
        if parameters["seedream_web_search"]:
            request["tools"] = [{"type": "web_search"}]

        selected_aspect_ratio = self._get_seedream_selected_aspect_ratio(
            image_bytes_list,
            aspect_ratio,
        )
        if parameters["seedream_send_aspect_ratio"] and selected_aspect_ratio:
            request["aspect_ratio"] = selected_aspect_ratio

        use_detailed_resolution = (
            parameters["seedream_send_detailed_resolution"]
            and selected_aspect_ratio
        )
        if use_detailed_resolution:
            request["size"] = self._build_seedream_adaptive_size(
                model_name,
                image_bytes_list,
                aspect_ratio,
                parameters,
            )
        else:
            request["size"] = parameters["seedream_resolution"]
        return request

    def _get_image_request_parameters(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            resolution: Optional[str] = None,
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        model_parameters = self._parameters_for_request(model_name, parameters)
        if not model_parameters:
            return {}

        parameters = self._get_model_parameters(model_name, model_parameters)
        adaptive_size = self._get_adaptive_image_size(
            model_name,
            image_bytes_list,
            resolution,
            aspect_ratio,
            force_aspect_ratio,
            model_parameters,
        )
        if adaptive_size:
            parameters["size"] = adaptive_size
        elif model_parameters.get("send_default_size"):
            parameters["size"] = model_parameters["default_resolution"]
        parameters.update(self._get_grok_request_parameters(
            model_name,
            image_bytes_list,
            aspect_ratio,
            force_aspect_ratio,
            model_parameters,
        ))
        return parameters

    @staticmethod
    def _render_prompt_template(template: str, variables: Dict[str, Any]) -> str:
        def replace_var(match: re.Match) -> str:
            key = match.group(1)
            if key in variables:
                return str(variables[key])
            return match.group(0)

        return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replace_var, template)

    def _build_final_prompt(self, prompt: str, model_name: str, image_count: int) -> str:
        user_prompt = str(prompt or "")
        has_images = image_count > 0
        # 未配置模板时直接发送用户/预设提示词，不再添加内置英文包装。
        default_prompt = user_prompt

        prompt_template = self._get_model_prompt_template(model_name)
        if not prompt_template:
            return default_prompt

        rendered_prompt = self._render_prompt_template(
            prompt_template,
            {
                "prompt": user_prompt,
                "model": (model_name or "").strip(),
                "mode": "图生图" if has_images else "文生图",
                "image_count": image_count,
                "default_prompt": default_prompt,
            },
        ).strip()
        return rendered_prompt or default_prompt

    def _get_chat_completions_system_prompt(self) -> str:
        if not self.conf.get("chat_completions_system_prompt_enabled", True):
            return ""
        return str(
            self.conf.get(
                "chat_completions_system_prompt",
                self.DEFAULT_CHAT_COMPLETIONS_SYSTEM_PROMPT,
            )
            or ""
        ).strip()

    async def _migrate_command_model_list_config(self):
        raw_list = self.conf.get("command_model_list", [])
        if not isinstance(raw_list, list):
            raw_list = []

        migrated: List[Dict[str, str]] = []
        changed = False
        seen = set()

        def add_item(command: Any, model: Any):
            key = str(command or "").strip()
            value = str(model or "").strip()
            if not key or not value or key in seen:
                return
            seen.add(key)
            migrated.append({"__template_key": "binding", "command": key, "model": value})

        for item in raw_list:
            if isinstance(item, dict):
                key = str(item.get("command") or item.get("指令") or "").strip()
                value = str(item.get("model") or item.get("模型名") or item.get("model_name") or "").strip()
                if key and value:
                    add_item(key, value)
                    if item.get("__template_key") != "binding" or set(item.keys()) != {"__template_key", "command", "model"}:
                        changed = True
                else:
                    changed = True
            elif isinstance(item, str) and ":" in item:
                key, value = item.split(":", 1)
                add_item(key, value)
                changed = True
            elif item:
                changed = True

        if changed:
            self.conf["command_model_list"] = migrated
            try:
                await self._persist_configuration()
                logger.info(f"已自动迁移 command_model_list 到模板列表格式，共 {len(migrated)} 条")
            except Exception as e:
                logger.error(f"自动迁移 command_model_list 配置失败: {e}")

    async def _migrate_extra_prefix_config(self):
        raw_prefixes = self.conf.get("extra_prefix", "bnn")
        if isinstance(raw_prefixes, list) and all(
            isinstance(item, dict) and item.get("__template_key") == "prefix" and "prefix" in item
            for item in raw_prefixes
        ):
            return

        prefixes: List[str] = []

        def add_prefix(value: Any):
            prefix = str(value or "").strip().lstrip("#")
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)

        if isinstance(raw_prefixes, str):
            for item in re.split(r"[,，\n]+", raw_prefixes):
                add_prefix(item)
        elif isinstance(raw_prefixes, list):
            for item in raw_prefixes:
                if isinstance(item, dict):
                    add_prefix(item.get("prefix") or item.get("前缀") or item.get("command") or item.get("value"))
                else:
                    add_prefix(item)

        if not prefixes:
            prefixes.append("bnn")

        self.conf["extra_prefix"] = [{"__template_key": "prefix", "prefix": prefix} for prefix in prefixes]
        try:
            await self._persist_configuration()
            logger.info(f"已自动迁移 extra_prefix 到模板列表格式，共 {len(prefixes)} 条")
        except Exception as e:
            logger.error(f"自动迁移 extra_prefix 配置失败: {e}")

    def _get_extra_prefixes(self) -> List[str]:
        raw_prefixes = self.conf.get("extra_prefix", "bnn")
        prefixes: List[str] = []

        def add_prefix(value: Any):
            prefix = str(value or "").strip().lstrip("#")
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)

        if isinstance(raw_prefixes, str):
            for item in re.split(r"[,，\n]+", raw_prefixes):
                add_prefix(item)
        elif isinstance(raw_prefixes, list):
            for item in raw_prefixes:
                if isinstance(item, dict):
                    add_prefix(item.get("prefix") or item.get("前缀") or item.get("command") or item.get("value"))
                else:
                    add_prefix(item)

        if not prefixes:
            prefixes.append("bnn")
        return prefixes

    def _get_preset_list_commands(self) -> List[str]:
        configured = str(self.conf.get("preset_list_command", "手办化列表") or "").strip()
        configured = configured.lstrip("#").strip()
        return [configured or "手办化列表"]

    def _model_has_explicit_endpoint_route(self, model_name: str) -> bool:
        normalized_model = (model_name or "").strip()
        if not normalized_model:
            return False
        if normalized_model in self._normalize_model_list(self.conf.get("gemini_model_list", [])):
            return True
        return any(
            normalized_model in self._get_endpoint_models(endpoint_type)
            for endpoint_type in self.GENERIC_ENDPOINT_MODEL_LIST_KEYS
        )

    def _get_request_context(
            self,
            source_model: str,
            actual_model: str,
            has_images: bool,
    ) -> Dict[str, Any]:
        """Resolve per-attempt route and parameters without changing the actual model ID."""
        source_name = (source_model or "").strip()
        actual_name = (actual_model or source_name).strip()
        route_model = (
            actual_name if self._model_has_explicit_endpoint_route(actual_name)
            else source_name if self._model_has_explicit_endpoint_route(source_name)
            else ""
        )
        gemini_models = self._normalize_model_list(self.conf.get("gemini_model_list", []))
        api_route = "gemini" if route_model in gemini_models else "generic"
        parameters = self._get_effective_model_parameters(source_name, actual_name)
        endpoint_type = "gemini_generate_content"
        if api_route == "generic":
            endpoint_type = self._get_generic_endpoint_type_for_model(
                route_model,
                has_images,
                parameters=parameters,
            )
        return {
            "source_model": source_name,
            "actual_model": actual_name,
            "route_model": route_model,
            "parameters": parameters,
            "api_route": api_route,
            "endpoint_type": endpoint_type,
        }

    def _get_api_route_for_model(self, model_name: str) -> str:
        return self._get_request_context(model_name, model_name, False)["api_route"]

    def _get_generic_endpoint_type_for_model(
            self,
            model_name: str,
            has_images: bool,
            *,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> str:
        """根据端点模型列表和输入图片决定 Generic 模式的请求端点。

        同一模型同时配置在 Images Edits 与 Images Generations 列表时，
        图生图优先走 Edits，文生图走 Generations。
        """
        if not self._has_generic_endpoint_model_routes():
            return "chat_completions"

        normalized_model = (model_name or "").strip()
        if not normalized_model:
            return "chat_completions"

        effective_parameters = self._parameters_for_request(normalized_model, parameters)
        if has_images:
            if (
                    effective_parameters
                    and effective_parameters.get("enable_seedream_parameters")
                    and normalized_model in self._get_endpoint_models("images_generations")
            ):
                endpoint_order = ("images_generations", "images_edits", "chat_completions")
            else:
                endpoint_order = ("images_edits", "images_generations", "chat_completions")
        else:
            endpoint_order = ("images_generations", "chat_completions")

        for endpoint_type in endpoint_order:
            if normalized_model in self._get_endpoint_models(endpoint_type):
                return endpoint_type

        return "chat_completions"

    def _get_endpoint_display_for_request(
            self,
            model_name: str,
            has_images: bool,
            source_model: Optional[str] = None,
    ) -> str:
        """返回开始消息中显示的当前请求端点。"""
        context = self._get_request_context(
            source_model or model_name,
            model_name,
            has_images,
        )
        if context["api_route"] == "gemini":
            return "generateContent"
        return self.GENERIC_ENDPOINT_DISPLAY_NAMES.get(
            context["endpoint_type"],
            self.GENERIC_ENDPOINT_DISPLAY_NAMES["chat_completions"],
        )

    def _parse_command_token(
            self,
            token: str,
    ) -> Tuple[str, int, Optional[int], Optional[int], Optional[str], Optional[str]]:
        """解析命令词末尾可任意排序的模型、批量、分辨率和比例修饰符。"""
        command = str(token or "").strip()
        default_batch = _normalize_positive_int(self.conf.get("default_batch_count", 1), 1)
        max_batch = _normalize_positive_int(self.conf.get("max_batch_multiplier", 4), 4)
        batch_count = min(default_batch, max_batch)
        requested_batch_count: Optional[int] = None
        model_index: Optional[int] = None
        resolution: Optional[str] = None
        aspect_ratio: Optional[str] = None
        batch_symbol = str(self.conf.get("batch_multiplier_symbol", "*") or "").strip()
        resolution_symbol = str(self.conf.get("resolution_symbol", "x") or "").strip()
        aspect_ratio_symbol = str(self.conf.get("aspect_ratio_symbol", "=") or "").strip()

        # 两种符号相同时无法区分含义，优先保留原有批量写法。
        if resolution_symbol == batch_symbol:
            resolution_symbol = ""
        if aspect_ratio_symbol in {batch_symbol, resolution_symbol}:
            aspect_ratio_symbol = ""

        while command:
            matched = False

            if requested_batch_count is None and batch_symbol:
                batch_match = re.search(rf"{re.escape(batch_symbol)}(\d+)$", command)
                if batch_match:
                    requested_batch_count = max(1, int(batch_match.group(1)))
                    batch_count = min(requested_batch_count, max_batch)
                    command = command[:batch_match.start()].strip()
                    matched = True

            if not matched and resolution is None and resolution_symbol:
                resolution_match = re.search(
                    rf"{re.escape(resolution_symbol)}([124])$",
                    command,
                    re.IGNORECASE,
                )
                if resolution_match:
                    resolution = f"{resolution_match.group(1)}K"
                    command = command[:resolution_match.start()].strip()
                    matched = True

            if not matched and aspect_ratio is None and aspect_ratio_symbol:
                ratio_match = re.search(
                    rf"{re.escape(aspect_ratio_symbol)}"
                    r"(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)$",
                    command,
                )
                if ratio_match:
                    ratio_value = f"{ratio_match.group(1)}:{ratio_match.group(2)}"
                    if self._parse_aspect_ratio(ratio_value):
                        aspect_ratio = ratio_value
                        command = command[:ratio_match.start()].strip()
                        matched = True

            if not matched and model_index is None:
                model_match = re.search(r"[\(（](\d+)[\)）]$", command)
                if model_match:
                    model_index = int(model_match.group(1))
                    command = command[:model_match.start()].strip()
                    matched = True

            if not matched:
                break

        return command, batch_count, requested_batch_count, model_index, resolution, aspect_ratio

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in self.context.get_config().get("admins_id", [])

    def _norm_id(self, raw_id: Any) -> str:
        if raw_id is None:
            return ""
        return str(raw_id).strip()

    @staticmethod
    def _qq_avatar_url(user_id: str) -> str:
        normalized = str(user_id or "").strip()
        if not normalized.isdigit():
            return ""
        return f"https://q1.qlogo.cn/g?b=qq&nk={normalized}&s=100"

    @staticmethod
    def _event_display_value(event: AstrMessageEvent, names: Tuple[str, ...]) -> str:
        for name in names:
            value = getattr(event, name, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if value not in (None, ""):
                return str(value).strip()
        message_obj = getattr(event, "message_obj", None)
        for name in names:
            value = getattr(message_obj, name, None)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    async def _snapshot_event_identity(
        self,
        event: AstrMessageEvent,
        user_id: str,
        group_id: Optional[str],
    ) -> Dict[str, str]:
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        group = getattr(message_obj, "group", None)
        nickname = str(getattr(sender, "nickname", "") or "").strip()
        group_name = str(getattr(group, "group_name", "") or "").strip()
        snapshot = {
            "identity_platform": str(getattr(event, "platform", "") or "").strip(),
            "user_nickname_snapshot": nickname or self._event_display_value(
                event,
                ("get_sender_name", "sender_name", "nickname", "user_name", "sender_nickname"),
            ),
            "user_avatar_url_snapshot": self._qq_avatar_url(user_id),
            "group_name_snapshot": group_name or self._event_display_value(
                event,
                ("get_group_name", "group_name", "group_nickname", "group_title"),
            ),
        }
        if not self.usage_store:
            return snapshot
        try:
            await self.usage_store.snapshot_identity(
                user_id=user_id,
                platform=snapshot["identity_platform"],
                nickname=snapshot["user_nickname_snapshot"],
                avatar_url=snapshot["user_avatar_url_snapshot"],
                group_id=group_id or "",
                group_name=snapshot["group_name_snapshot"],
            )
        except Exception as exc:
            logger.warning(f"记录用量身份快照失败: {exc}")
        return snapshot

    def _get_usage_endpoint_details(
            self,
            model_name: str,
            has_images: bool,
            source_model: Optional[str] = None,
            request_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        context = request_context or self._get_request_context(
            source_model or model_name,
            model_name,
            has_images,
        )
        return context["api_route"], context["endpoint_type"]

    async def _settle_usage_generation(
        self,
        *,
        event: AstrMessageEvent,
        source: str,
        sender_id: str,
        group_id: Optional[str],
        logical_model: str,
        actual_model: str,
        has_images: bool,
        outcome: str,
        http_status: int,
        output_count: int,
        charged_amount: int,
        deduction_source: Optional[str],
        note: str = "",
        request_context: Optional[Dict[str, Any]] = None,
    ):
        identity_snapshot = await self._snapshot_event_identity(event, sender_id, group_id)
        if not self.usage_store:
            if charged_amount:
                await self._deduct_generation_cost(
                    deduction_source, sender_id, group_id, charged_amount
                )
            return
        api_route, endpoint_type = self._get_usage_endpoint_details(
            actual_model,
            has_images,
            source_model=logical_model,
            request_context=request_context,
        )
        try:
            settlement = await self.usage_store.settle_generation(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                source=source,
                user_id=sender_id,
                group_id=group_id,
                logical_model=logical_model,
                actual_model=actual_model,
                api_route=api_route,
                endpoint_type=endpoint_type,
                mode="图生图" if has_images else "文生图",
                outcome=outcome,
                http_status=http_status,
                output_count=output_count,
                charged_amount=charged_amount,
                deduction_source=deduction_source,
                note=note,
                **identity_snapshot,
            )
            resulting_balance = settlement.get("resulting_balance")
            subject_type = settlement.get("balance_subject_type")
            subject_id = settlement.get("balance_subject_id")
            if resulting_balance is not None and subject_type == "user" and subject_id:
                self.user_balances[subject_id] = int(resulting_balance)
                await self._save_user_balances()
            elif resulting_balance is not None and subject_type == "group" and subject_id:
                self.group_balances[subject_id] = int(resulting_balance)
                await self._save_group_balances()
        except Exception as exc:
            logger.error(f"记录用量账本失败，回退 JSON 扣费: {exc}")
            if charged_amount:
                await self._deduct_generation_cost(
                    deduction_source, sender_id, group_id, charged_amount
                )

    async def _adjust_usage_balance(
        self,
        *,
        event: Optional[AstrMessageEvent],
        subject_type: str,
        subject_id: str,
        amount: int,
        source: str,
        actor: str = "",
        note: str = "",
        snapshot_identity: bool = True,
    ) -> int:
        subject_id = self._norm_id(subject_id)
        if not subject_id:
            raise ValueError("余额调整目标不能为空")
        group_id = subject_id if subject_type == "group" else ""
        user_id = subject_id if subject_type == "user" else ""
        identity_snapshot: Dict[str, str] = {}
        if event and snapshot_identity:
            identity_snapshot = await self._snapshot_event_identity(event, user_id, group_id or None)
        if self.usage_store:
            try:
                result = await self.usage_store.adjust_balance(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    amount=amount,
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    source=source,
                    actor=actor,
                    note=note,
                    user_id=user_id,
                    group_id=group_id,
                    **identity_snapshot,
                )
                balance = int(result["after"])
                if subject_type == "user":
                    self.user_balances[subject_id] = balance
                    await self._save_user_balances()
                else:
                    self.group_balances[subject_id] = balance
                    await self._save_group_balances()
                return balance
            except Exception as exc:
                logger.error(f"调整余额账本失败，回退 JSON: {exc}")
        if subject_type == "user":
            balance = max(0, self._get_user_balance(subject_id) + amount)
            self.user_balances[subject_id] = balance
            await self._save_user_balances()
        else:
            balance = max(0, self._get_group_balance(subject_id) + amount)
            self.group_balances[subject_id] = balance
            await self._save_group_balances()
        return balance

    def _get_maintenance_message(self) -> Optional[str]:
        if not self.conf.get("maintenance_mode", False):
            return None
        message = str(
            self.conf.get("maintenance_message", "插件当前正在维护中，请稍后再试。") or ""
        ).strip()
        return message or "插件当前正在维护中，请稍后再试。"

    def _get_help_command(self) -> str:
        command = str(self.conf.get("help_command", "手办化帮助") or "").strip()
        command = command.lstrip("#").strip()
        return command or "手办化帮助"

    @filter.command("切换模型", aliases={"SwitchModel", "模型列表"}, prefix_optional=True)
    async def on_switch_model(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        all_models = self._get_all_models()
        raw_msg = event.message_str.strip()
        parts = raw_msg.split()

        if len(parts) == 1:
            current_model = self.conf.get("model", "nano-banana")
            current_api_mode = self._get_api_route_for_model(current_model)

            msg = "📋 **可用模型列表**:\n"
            msg += "------------------\n"

            for idx, model_name in enumerate(all_models):
                seq_num = idx + 1
                status = "✅ (当前)" if model_name == current_model else ""
                msg += f"{seq_num}. {model_name} {status}\n"

            msg += "------------------\n"
            msg += f"📡 **当前模型路由**: {current_api_mode}\n"
            msg += "------------------\n"
            msg += "📝 **指令**:\n"
            msg += "1. `#切换模型 <序号>`\n"
            msg += "2. `#手办化(序号) [图片]`"

            yield event.plain_result(msg)
            return

        arg = parts[1]
        if not self.is_global_admin(event):
            yield event.plain_result("❌ 只有管理员可以更改全局默认模型。")
            return

        if not arg.isdigit():
            yield event.plain_result("❌ 格式错误。请输入数字序号。")
            return

        target_idx = int(arg) - 1

        if 0 <= target_idx < len(all_models):
            new_model = all_models[target_idx]
            previous_model = self.conf.get("model")
            self.conf["model"] = new_model
            try:
                await self._persist_configuration()
            except Exception as exc:
                self.conf["model"] = previous_model
                yield event.plain_result(f"❌ 默认模型保存失败: {exc}")
                return
            yield event.plain_result(f"✅ 切换成功！\n当前默认模型: **{new_model}**")
        else:
            yield event.plain_result(f"❌ 序号无效。")

    async def _get_pool_api_key(self, mode: str) -> str | None:
        """Return a shared Generic key, with legacy Gemini keys as read-only fallback."""
        async with self.key_lock:
            generic_keys = self.conf.get("generic_api_keys", [])
            if isinstance(generic_keys, list) and generic_keys:
                key = generic_keys[self.generic_key_index % len(generic_keys)]
                self.generic_key_index = (self.generic_key_index + 1) % len(generic_keys)
                return key

            if mode == "gemini":
                legacy_keys = self.conf.get("gemini_api_keys", [])
                if isinstance(legacy_keys, list) and legacy_keys:
                    key = legacy_keys[self.gemini_key_index % len(legacy_keys)]
                    self.gemini_key_index = (self.gemini_key_index + 1) % len(legacy_keys)
                    return key
            return None

    @staticmethod
    def _looks_like_image_mime(mime_type: Any) -> bool:
        return isinstance(mime_type, str) and mime_type.lower().startswith("image/")

    @staticmethod
    def _estimate_data_url_size(data_url: str) -> int:
        if not data_url.startswith("data:") or "," not in data_url:
            return 0
        b64 = data_url.split(",", 1)[1].strip()
        return max(0, len(b64) * 3 // 4)

    @staticmethod
    def _decode_data_url_image(data_url: str) -> bytes:
        """容错解码 data URL 中的 Base64：去除空白、兼容 URL-safe 字符集、补齐缺失的 = 填充。
        解码结果需为常见图片格式，损坏/截断的数据抛异常以便调用方回退到其他候选。"""
        b64 = re.sub(r"\s+", "", data_url.split(",", 1)[-1])
        b64 = b64.translate(str.maketrans("-_", "+/"))
        b64 += "=" * (-len(b64) % 4)
        raw = base64.b64decode(b64, validate=True)
        if raw.startswith(b"\x89PNG"):
            if b"IEND" not in raw[-16:]:
                raise ValueError("PNG 数据不完整(缺少 IEND 结尾块)")
        elif raw.startswith(b"\xff\xd8"):
            if not raw.endswith(b"\xff\xd9"):
                raise ValueError("JPEG 数据不完整(缺少结尾标记)")
        elif raw.startswith(b"GIF8"):
            if not raw.endswith(b"\x00\x3b"):
                raise ValueError("GIF 数据不完整(缺少结尾标记)")
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            pass
        else:
            raise ValueError("解码结果不是可识别的图片数据")
        return raw

    @staticmethod
    def _normalize_image_candidate(url: Any) -> str:
        if not isinstance(url, str):
            return ""
        return url.strip().rstrip(")>,'\"")

    def _extract_image_urls_from_text_blob(self, text: Any) -> List[str]:
        if not isinstance(text, str) or not text:
            return []

        candidates: List[str] = []
        data_matches = re.findall(
            r"data:image/[a-zA-Z0-9.+-]+;base64,[a-zA-Z0-9+/=]+",
            text,
            re.IGNORECASE,
        )
        candidates.extend(data_matches)

        markdown_matches = re.findall(r"!\[[^\]]*]\(([^)]+)\)", text)
        for match in markdown_matches:
            candidate = self._normalize_image_candidate(match)
            if candidate.startswith("data:image") or candidate.startswith("http"):
                candidates.append(candidate)

        url_matches = re.findall(r"https?://[^\s<>\"')\]]+", text)
        for match in url_matches:
            candidate = self._normalize_image_candidate(match)
            if candidate:
                candidates.append(candidate)

        return self._dedupe_preserve_order(candidates)

    def _extract_image_urls_from_response(self, data: Any) -> List[str]:
        candidates: List[str] = []

        def add(candidate: Any):
            normalized = self._normalize_image_candidate(candidate)
            if normalized:
                candidates.append(normalized)

        def add_data_url(mime_type: Any, b64_data: Any):
            if self._looks_like_image_mime(mime_type) and isinstance(b64_data, str) and b64_data.strip():
                add(f"data:{mime_type};base64,{b64_data.strip()}")

        def visit(obj: Any):
            if isinstance(obj, str):
                for candidate in self._extract_image_urls_from_text_blob(obj):
                    add(candidate)
                return

            if isinstance(obj, list):
                for item in obj:
                    visit(item)
                return

            if not isinstance(obj, dict):
                return

            inline_data = obj.get("inlineData") or obj.get("inline_data")
            if isinstance(inline_data, dict):
                add_data_url(
                    inline_data.get("mimeType") or inline_data.get("mime_type"),
                    inline_data.get("data"),
                )

            if isinstance(obj.get("b64_json"), str):
                add(f"data:image/png;base64,{obj['b64_json'].strip()}")

            for base64_key in ("image_base64", "image_b64", "base64"):
                if isinstance(obj.get(base64_key), str):
                    mime_type = obj.get("mime_type") or obj.get("mimeType") or "image/png"
                    add_data_url(mime_type, obj.get(base64_key))

            if (
                    isinstance(obj.get("data"), str)
                    and self._looks_like_image_mime(obj.get("mime_type") or obj.get("mimeType"))
            ):
                add_data_url(obj.get("mime_type") or obj.get("mimeType"), obj.get("data"))

            for url_key in ("url", "image_url", "imageUrl", "file_url", "fileUrl"):
                value = obj.get(url_key)
                if isinstance(value, str):
                    add(value)
                elif isinstance(value, dict):
                    nested_url = value.get("url") or value.get("image_url") or value.get("imageUrl")
                    if nested_url:
                        add(nested_url)

            for value in obj.values():
                visit(value)

        visit(data)
        deduped = self._dedupe_preserve_order(candidates)
        data_urls = [url for url in deduped if url.startswith("data:image")]
        other_urls = [url for url in deduped if not url.startswith("data:image")]
        data_urls.sort(key=self._estimate_data_url_size, reverse=True)
        return data_urls + other_urls

    def _extract_image_url_from_response(self, data: Dict[str, Any]) -> str | None:
        urls = self._extract_image_urls_from_response(data)
        return urls[0] if urls else None

    @staticmethod
    def _detect_generic_endpoint_type(url: str) -> str:
        path = urlparse(url).path.rstrip("/").lower()
        if path.endswith("/images/edits"):
            return "images_edits"
        if path.endswith("/images/generations"):
            return "images_generations"
        return "chat_completions"

    @staticmethod
    def _is_generic_base_url(url: str) -> bool:
        path = urlparse(url).path.rstrip("/").lower()
        return not path or path.endswith("/v1")

    @staticmethod
    def _is_generic_service_root(url: str) -> bool:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc and not parsed.path.rstrip("/"))

    def _get_api_base_url(self) -> str:
        """Return the shared service URL for Generic and Gemini-compatible requests."""
        return str(
            self.conf.get("generic_api_url", self.DEFAULT_GENERIC_API_URL) or ""
        ).strip()

    def _resolve_generic_endpoint_url(self, url: str, endpoint_type: str) -> str:
        target_path = self.GENERIC_ENDPOINT_PATHS.get(
            endpoint_type,
            self.GENERIC_ENDPOINT_PATHS["chat_completions"],
        )
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        lower_path = path.lower()

        base_path = path
        known_suffixes = sorted(
            self.GENERIC_ENDPOINT_PATHS.values(),
            key=len,
            reverse=True,
        )
        for suffix in known_suffixes:
            if lower_path.endswith(suffix):
                base_path = path[: -len(suffix)]
                break
        else:
            if lower_path.endswith("/v1"):
                base_path = path[: -len("/v1")]
            elif not path or path == "/":
                base_path = ""

        new_path = f"{base_path.rstrip('/')}{target_path}"
        return parsed._replace(path=new_path).geturl()

    @classmethod
    def _resolve_gemini_endpoint_url(cls, url: str, model_name: str) -> str:
        base_url = (url or "").strip()
        model = (model_name or "").strip()
        if "{model}" in base_url:
            return base_url.replace("{model}", model)

        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        lower_path = path.lower()

        for suffix in sorted(cls.GENERIC_ENDPOINT_PATHS.values(), key=len, reverse=True):
            if lower_path.endswith(suffix):
                path = path[: -len(suffix)]
                lower_path = path.rstrip("/").lower()
                break
        if lower_path.endswith("/v1"):
            path = path[: -len("/v1")]
            lower_path = path.rstrip("/").lower()

        if lower_path.endswith(":generatecontent"):
            return base_url

        if "/models/" in lower_path:
            if not path.endswith(model):
                path = path.rsplit("/models/", 1)[0] + f"/models/{model}"
            return parsed._replace(path=f"{path}:generateContent").geturl()

        if lower_path.endswith("/v1") or lower_path.endswith("/v1beta"):
            pass
        elif re.search(r"/v\d+(?:beta|alpha)?$", lower_path):
            pass
        else:
            path = f"{path}/v1beta" if path else "/v1beta"

        return parsed._replace(path=f"{path}/models/{model}:generateContent").geturl()

    @staticmethod
    def _guess_image_mime_type(image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG"):
            return "image/png"
        if image_bytes.startswith(b"\xff\xd8"):
            return "image/jpeg"
        if image_bytes.startswith(b"GIF8"):
            return "image/gif"
        if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:12]:
            return "image/webp"
        if image_bytes.startswith(b"BM"):
            return "image/bmp"
        return "image/png"

    def _image_bytes_to_data_url(self, image_bytes: bytes) -> str:
        mime_type = self._guess_image_mime_type(image_bytes)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{b64}"

    def _sanitize_request_log_value(self, value: Any, field_name: str = "") -> Any:
        normalized_field = field_name.strip().lower().replace("-", "_")
        if normalized_field in {
                "authorization",
                "api_key",
                "x_goog_api_key",
                "x_api_key",
        }:
            return "<redacted>"

        if isinstance(value, dict):
            return {
                str(key): self._sanitize_request_log_value(item, str(key))
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize_request_log_value(item, field_name) for item in value]
        if isinstance(value, bytes):
            return "<image omitted>"
        if isinstance(value, str):
            if value.startswith("data:") and ";base64," in value:
                return "<image omitted>"
            if normalized_field == "data" and value:
                return "<image omitted>"
        return value

    @staticmethod
    def _sanitize_request_log_url(url: str) -> str:
        parsed = urlparse(str(url or ""))
        sensitive_names = {
            "api_key", "apikey", "key", "token", "access_token", "password", "passwd",
        }
        sanitized_query = [
            (name, "<redacted>" if name.lower() in sensitive_names else value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            netloc = f"<redacted>@{host}"
        else:
            netloc = parsed.netloc
        return parsed._replace(netloc=netloc, query=urlencode(sanitized_query)).geturl()

    def _build_images_edits_log_parameters(
            self,
            model_name: str,
            final_prompt: str,
            image_bytes_list: List[bytes],
            resolution: Optional[str],
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        log_parameters: Dict[str, Any] = {
            "model": model_name,
            "prompt": final_prompt,
        }
        if not self._should_omit_n_parameter(model_name, parameters):
            log_parameters["n"] = "1"
        log_parameters.update(self._get_image_request_parameters(
            model_name,
            image_bytes_list,
            resolution,
            aspect_ratio,
            force_aspect_ratio,
            parameters,
        ))
        log_parameters["image"] = "<image omitted>"
        return log_parameters

    @staticmethod
    def _mime_type_to_extension(mime_type: str) -> str:
        return {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/bmp": "bmp",
        }.get(mime_type, "png")

    def _build_generic_images_payload(
            self,
            model_name: str,
            final_prompt: str,
            image_bytes_list: List[bytes],
            resolution: Optional[str] = None,
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model_name,
            "prompt": final_prompt,
        }
        seedream_parameters = self._get_seedream_parameters(model_name, parameters)
        if seedream_parameters:
            payload.update(self._get_seedream_request_parameters(
                model_name,
                image_bytes_list,
                aspect_ratio,
                force_aspect_ratio,
                parameters,
            ))
        else:
            if not self._should_omit_n_parameter(model_name, parameters):
                payload["n"] = 1
            payload.update(self._get_image_request_parameters(
                model_name,
                image_bytes_list,
                resolution,
                aspect_ratio,
                force_aspect_ratio,
                parameters,
            ))

        if image_bytes_list:
            image_inputs = [self._image_bytes_to_data_url(img) for img in image_bytes_list]
            if seedream_parameters:
                payload["image"] = image_inputs[0] if len(image_inputs) == 1 else image_inputs
            elif len(image_inputs) == 1:
                payload["image"] = image_inputs[0]
            else:
                payload["images"] = image_inputs

        return payload

    def _build_generic_images_edits_form(
            self,
            model_name: str,
            final_prompt: str,
            image_bytes_list: List[bytes],
            resolution: Optional[str] = None,
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("model", model_name)
        form.add_field("prompt", final_prompt)
        if not self._should_omit_n_parameter(model_name, parameters):
            form.add_field("n", "1")
        for field_name, value in self._get_image_request_parameters(
                model_name,
                image_bytes_list,
                resolution,
                aspect_ratio,
                force_aspect_ratio,
                parameters,
        ).items():
            form.add_field(field_name, value)

        for idx, image_bytes in enumerate(image_bytes_list):
            mime_type = self._guess_image_mime_type(image_bytes)
            ext = self._mime_type_to_extension(mime_type)
            form.add_field(
                "image",
                image_bytes,
                filename=f"image_{idx + 1}.{ext}",
                content_type=mime_type,
            )

        return form

    def _build_limit_exhausted_message(self, group_id: Optional[str]) -> str:
        if group_id and self.conf.get("enable_group_limit", False):
            msg = "❌ 本群或您的余额已不足 (优先扣除群余额)。"
        else:
            msg = "❌ 您的余额已不足。"

        if self.conf.get("enable_checkin", False) and self.conf.get("enable_user_limit", True):
            msg += "\n📅 可发送 \"#手办化签到\" 获取余额（触发前缀/唤醒请按实际配置调整）。"

        return msg

    def _get_required_invocation_cost(
            self,
            model_name: str = "",
            resolution: Optional[str] = None,
            has_images: bool = False,
            image_bytes_list: Optional[List[bytes]] = None,
            aspect_ratio: Optional[str] = None,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> int:
        tier = self._get_resolution_charge_tier(
            model_name,
            resolution,
            image_bytes_list,
            parameters,
            aspect_ratio,
        )
        return self._get_tiered_charge_amount(
            model_name,
            tier,
            parameters,
        ) + self._get_extra_reference_image_charge(
            model_name,
            image_bytes_list,
            parameters,
        )

    def _get_violation_deduction_cost(
            self,
            model_name: str,
            resolution: Optional[str] = None,
            image_bytes_list: Optional[List[bytes]] = None,
            parameters: Optional[Dict[str, Any]] = None,
            aspect_ratio: Optional[str] = None,
    ) -> int:
        """违规失败按实际调用模型的扣费档位结算（含超限参考图阶梯额外加费）。"""
        tier = self._get_resolution_charge_tier(
            model_name,
            resolution,
            image_bytes_list,
            parameters,
            aspect_ratio,
        )
        return self._get_tiered_charge_amount(
            model_name,
            tier,
            parameters,
        ) + self._get_extra_reference_image_charge(
            model_name,
            image_bytes_list,
            parameters,
        )

    def _get_failure_deduction_status_codes(self) -> set[int]:
        raw_codes = self.conf.get("failure_deduction_status_codes", [400])
        if isinstance(raw_codes, str):
            items: List[Any] = re.split(r"[,，\s]+", raw_codes)
        elif isinstance(raw_codes, list):
            items = raw_codes
        else:
            items = [raw_codes]

        status_codes = set()
        for item in items:
            if isinstance(item, dict):
                item = item.get("code") or item.get("status_code") or item.get("错误码")
            try:
                status_code = int(item)
            except (TypeError, ValueError):
                continue
            if 100 <= status_code <= 599:
                status_codes.add(status_code)
        return status_codes

    def _should_deduct_generation_result(
            self,
            result: Any,
            http_status: int,
            model_name: str,
            parameters: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if isinstance(result, bytes):
            return True
        if not self._should_send_content_policy_warning(http_status, result):
            return False

        parameters = self._parameters_for_request(model_name, parameters)
        if parameters is not None:
            return bool(parameters.get("deduct_on_violation"))

        configured_value = self.conf.get("deduct_on_failure_status_codes", None)
        if configured_value is None:
            configured_value = self.conf.get("deduct_on_content_policy_violation", True)
        return bool(configured_value)

    def _should_send_content_policy_warning(self, http_status: int, result: Any = None) -> bool:
        return (
            self._is_content_policy_violation(result)
            or http_status in self._get_failure_deduction_status_codes()
        )

    def _should_stop_model_failover(self, http_status: int, result: Any = None) -> bool:
        """违规或命中错误码时，直接返回警告而非继续热备请求。"""
        return self._should_send_content_policy_warning(http_status, result)

    async def _deduct_generation_cost(
            self,
            deduction_source: Optional[str],
            sender_id: str,
            group_id: Optional[str],
            amount: int,
    ):
        if deduction_source == "group" and group_id:
            await self._deduct_group_balance(group_id, amount)
        elif deduction_source == "user":
            await self._deduct_user_balance(sender_id, amount)

    def _get_remaining_balance_text(
            self,
            deduction_source: Optional[str],
            sender_id: str,
            group_id: Optional[str],
    ) -> str:
        """{remaining}：个人余额（仅数字，不带单位；免费时为 ∞）。"""
        if deduction_source == "free":
            return "∞"
        return format_amount(self._get_user_balance(sender_id))

    def _get_group_balance_text(
            self,
            deduction_source: Optional[str],
            group_id: Optional[str],
    ) -> str:
        """{group_balance}：群余额（仅数字；免费时为 ∞，无群时为 -）。"""
        if deduction_source == "free":
            return "∞"
        if group_id:
            return format_amount(self._get_group_balance(group_id))
        return "-"

    def _get_generation_cost_text(
            self,
            deduction_source: Optional[str],
            should_deduct: bool,
            charged_amount: int,
    ) -> str:
        """默认 caption 用：本次实际花费（带单位）。"""
        if deduction_source == "free":
            return "免费"
        if should_deduct and charged_amount > 0:
            return f"{format_amount(charged_amount)} 元"
        return "0 元"

    def _get_generation_cost_value(
            self,
            deduction_source: Optional[str],
            should_deduct: bool,
            charged_amount: int,
    ) -> str:
        """{cost} 变量用：本次实际花费（仅数字，不带单位；免费时为 免费）。"""
        if deduction_source == "free":
            return "免费"
        if should_deduct and charged_amount > 0:
            return format_amount(charged_amount)
        return "0"

    def _resolve_generation_access(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        group_id: Optional[str],
        required_cost: int,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        user_blacklist = [self._norm_id(x) for x in (self.conf.get("user_blacklist") or [])]
        if sender_id in user_blacklist:
            return False, None, None

        is_master = self.is_global_admin(event)

        if group_id:
            group_blacklist = [self._norm_id(x) for x in (self.conf.get("group_blacklist") or [])]
            if group_id in group_blacklist and not is_master:
                return False, None, None

        group_whitelist = [self._norm_id(x) for x in (self.conf.get("group_whitelist") or [])]
        user_whitelist = [self._norm_id(x) for x in (self.conf.get("user_whitelist") or [])]

        if is_master:
            return True, "free", None
        if group_id and group_id in group_whitelist:
            return True, "free", None
        if group_id and group_whitelist:
            return False, None, "❌ 本群未授权使用此功能。"
        if user_whitelist and sender_id not in user_whitelist:
            return False, None, None

        if group_id and self.conf.get("enable_group_limit", False):
            if self._get_group_balance(group_id) >= required_cost:
                return True, "group", None

        if self.conf.get("enable_user_limit", True):
            if self._get_user_balance(sender_id) >= required_cost:
                return True, "user", None

        if not self.conf.get("enable_group_limit", False) and not self.conf.get("enable_user_limit", True):
            return True, "free", None

        return False, None, self._build_limit_exhausted_message(group_id)

    def _build_reply_chain(self, event: AstrMessageEvent) -> List[Reply]:
        msg_obj = getattr(event, "message_obj", None)
        message_id = getattr(msg_obj, "message_id", None)
        if message_id in (None, ""):
            raw_message = getattr(msg_obj, "raw_message", None)
            if isinstance(raw_message, dict):
                message_id = (
                    raw_message.get("message_id")
                    or raw_message.get("messageId")
                    or raw_message.get("id")
                )
            elif raw_message is not None:
                message_id = (
                    getattr(raw_message, "message_id", None)
                    or getattr(raw_message, "messageId", None)
                    or getattr(raw_message, "id", None)
                )

        if message_id in (None, ""):
            return []
        return [Reply(id=message_id)]

    def _reply_plain_result(self, event: AstrMessageEvent, text: str):
        return event.chain_result([*self._build_reply_chain(event), Plain(text)])

    def _reply_chain_result(self, event: AstrMessageEvent, chain: List[Any]):
        return event.chain_result([*self._build_reply_chain(event), *chain])

    def _build_image_result(self, event: AstrMessageEvent, image_bytes: bytes, text: str):
        return self._reply_chain_result(event, [Image.fromBytes(image_bytes), Plain(text)])

    async def _send_llm_image_once(
            self,
            event: AstrMessageEvent,
            image_bytes: bytes,
            text: str,
    ) -> bool:
        try:
            await event.send(self._build_image_result(event, image_bytes, text))
            return True
        except Exception as exc:
            logger.error(f"LLM 工具图片发送失败，不会重试以避免重复发送: {exc}")
            return False

    @staticmethod
    def _mask_sensitive_text(text: str) -> str:
        text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", lambda m: f"{m.group(0)[:7]}***", text)
        text = re.sub(r"AIza[A-Za-z0-9_-]{12,}", lambda m: f"{m.group(0)[:8]}***", text)
        return text

    def _safe_error_text(self, value: Any, max_length: Optional[int] = None) -> str:
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except Exception:
                text = str(value)

        text = self._mask_sensitive_text(text).strip()
        if max_length and len(text) > max_length:
            text = text[:max_length].rstrip() + "..."
        return text

    @staticmethod
    def _contains_content_policy_violation_text(text: Any) -> bool:
        normalized = str(text or "").lower()
        markers = (
            "content_policy_violation",
            "content policy violation",
            "content policy",
            "content filter",
            "content_filter",
            "safety block",
            "safety_block",
            "safety filter",
            "safety system",
            "policy violation",
            "content moderation",
            "moderation block",
            "blocked content",
            "unsafe content",
            "违规内容",
            "内容违规",
            "安全拦截",
            "安全策略",
            "内容审核",
            "敏感内容",
            "不当内容",
        )
        return any(marker in normalized for marker in markers)

    def _is_content_policy_violation(self, result: Any) -> bool:
        """识别上游返回的内容安全拦截。"""
        if not isinstance(result, dict):
            return False

        error_type = str(result.get("error_type") or "").strip().lower()
        if error_type in {
            "safety_block",
            "content_policy_violation",
            "content_filter",
            "moderation_block",
            "policy_violation",
        }:
            return True

        return self._contains_content_policy_violation_text(
            " ".join(
                str(result.get(key) or "")
                for key in (
                    "provider_code",
                    "provider_type",
                    "provider_message",
                    "message",
                    "detail",
                )
            )
        )

    def _get_content_policy_warning_reason(self, result: Any) -> str:
        max_reason_length = _normalize_positive_int(
            self.conf.get("error_detail_max_length", 800),
            800,
            80,
        )
        if isinstance(result, dict):
            candidates = (
                result.get("provider_message"),
                result.get("message"),
                result.get("detail"),
            )
        else:
            candidates = (result,)

        for candidate in candidates:
            reason = self._safe_error_text(candidate, max_reason_length)
            if reason:
                return reason
        return ""

    def _get_content_policy_warning_message(
            self,
            *,
            model: str = "",
            label: str = "",
            image_count: int = 0,
            elapsed: float = 0.0,
            remaining: str = "∞",
            group_balance: str = "-",
            prompt: str = "",
            reason: str = "",
            batch_count: Optional[int] = None,
            batch_index: Optional[int] = None,
            max_batch_concurrency: Optional[int] = None,
    ) -> str:
        message = str(
            self.conf.get(
                "content_policy_warning_message",
                self.DEFAULT_CONTENT_POLICY_WARNING_MESSAGE,
            ) or ""
        ).strip()
        variables: Dict[str, Any] = {
            "model": model,
            "label": label,
            "image_count": image_count,
            "elapsed": f"{elapsed:.2f}",
            "remaining": remaining,
            "group_balance": group_balance,
            "prompt": prompt[:50],
            "reason": reason,
        }
        if batch_count is not None:
            variables.update(
                {
                    "batch_count": batch_count,
                    "batch_index": batch_index,
                    "max_batch_concurrency": max_batch_concurrency,
                }
            )
        return self._format_template(
            message or self.DEFAULT_CONTENT_POLICY_WARNING_MESSAGE,
            variables,
        )

    @staticmethod
    def _get_content_policy_response_reason(data: Any) -> str:
        """提取 HTTP 200 响应中未以 error 字段返回的安全完成原因。"""
        if not isinstance(data, dict):
            return ""

        prompt_feedback = data.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            return str(prompt_feedback["blockReason"])

        safety_finish_reasons = {
            "SAFETY",
            "IMAGE_SAFETY",
            "CONTENT_FILTER",
            "CONTENT_POLICY_VIOLATION",
            "PROHIBITED_CONTENT",
            "BLOCKED",
            "BLOCKLIST",
        }
        for candidate in data.get("candidates") or []:
            if isinstance(candidate, dict):
                finish_reason = str(candidate.get("finishReason") or "").upper()
                if finish_reason in safety_finish_reasons:
                    return finish_reason
        for choice in data.get("choices") or []:
            if isinstance(choice, dict):
                finish_reason = str(choice.get("finish_reason") or "").upper()
                if finish_reason in safety_finish_reasons:
                    return finish_reason
        return ""

    @staticmethod
    def _build_api_error(error_type: str, message: str, **kwargs: Any) -> Dict[str, Any]:
        detail = kwargs.pop("detail", None)
        return {
            "__api_error__": True,
            "error_type": error_type,
            "message": message,
            "detail": detail if detail is not None else message,
            **kwargs,
        }

    def _extract_provider_error_fields(self, data: Any) -> Dict[str, str]:
        result = {
            "provider_code": "",
            "provider_type": "",
            "provider_message": "",
        }

        if not isinstance(data, dict):
            result["provider_message"] = self._safe_error_text(data)
            return result

        err = data.get("error")
        if isinstance(err, dict):
            result["provider_code"] = self._safe_error_text(err.get("code") or err.get("status") or "")
            result["provider_type"] = self._safe_error_text(err.get("type") or err.get("param") or "")
            result["provider_message"] = self._safe_error_text(err.get("message") or err)
            return result
        if isinstance(err, str):
            result["provider_message"] = self._safe_error_text(err)
            return result

        for key in ("message", "detail", "msg"):
            if data.get(key):
                result["provider_message"] = self._safe_error_text(data.get(key))
                break
        result["provider_code"] = self._safe_error_text(data.get("code") or data.get("status") or "")
        return result

    @staticmethod
    def _format_template(template: str, variables: Dict[str, Any]) -> str:
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace("{" + key + "}", "" if value is None else str(value))
        return rendered

    def _select_error_template(self, http_status: int, error_type: str) -> str:
        keys = []
        if error_type == "timeout":
            keys.append("error_timeout_message")
        elif error_type in {"no_image", "image_parse_error"}:
            keys.append("error_200_no_image_message")
        elif http_status == 400:
            keys.append("error_400_message")
        elif http_status == 401:
            keys.append("error_401_message")
        elif http_status == 403:
            keys.append("error_403_message")
        elif http_status == 404:
            keys.append("error_404_message")
        elif http_status == 429:
            keys.append("error_429_message")
        elif 500 <= http_status < 600:
            keys.append("error_500_message")

        keys.append("error_default_message")
        for key in keys:
            template = str(self.conf.get(key, "") or "").strip()
            if template:
                return template
        return ""

    def _format_error_message(
            self,
            status_text: str,
            elapsed: float,
            detail: Any,
            http_status: int = 0,
            **context: Any,
    ) -> str:
        """构造错误消息：支持结构化错误、自定义模板和默认变量。"""
        max_detail_len = _normalize_positive_int(self.conf.get("error_detail_max_length", 800), 800, 80)
        error_data = detail if isinstance(detail, dict) and detail.get("__api_error__") else {}

        message = self._safe_error_text(error_data.get("message") if error_data else detail, max_detail_len)
        detail_text = self._safe_error_text(error_data.get("detail") if error_data else detail, max_detail_len)
        error_type = self._safe_error_text(error_data.get("error_type") or "unknown")
        provider_message = self._safe_error_text(
            error_data.get("provider_message") or message or detail_text,
            max_detail_len,
        )
        provider_code = self._safe_error_text(error_data.get("provider_code") or "")
        provider_type = self._safe_error_text(error_data.get("provider_type") or "")

        variables = {
            "status_text": status_text,
            "status_code": http_status,
            "http_status": http_status,
            "elapsed": f"{elapsed:.2f}",
            "error_type": error_type,
            "message": message,
            "detail": detail_text,
            "provider_message": provider_message,
            "provider_code": provider_code,
            "provider_type": provider_type,
            "request_id": self._safe_error_text(error_data.get("request_id") or ""),
            "model": self._safe_error_text(context.get("model") or error_data.get("model") or ""),
            "api_mode": self._safe_error_text(context.get("api_mode") or error_data.get("api_mode") or ""),
            "endpoint_type": self._safe_error_text(error_data.get("endpoint_type") or ""),
            "endpoint": self._safe_error_text(error_data.get("endpoint") or error_data.get("url") or "", 500),
            "url": self._safe_error_text(error_data.get("url") or error_data.get("endpoint") or "", 500),
            "image_url": self._safe_error_text(error_data.get("image_url") or "", 500),
            "image_count": context.get("image_count", error_data.get("image_count", "")),
            "prompt": self._safe_error_text(context.get("prompt") or error_data.get("prompt") or "", 300),
        }

        custom_template = self._select_error_template(http_status, error_type)
        if custom_template:
            rendered = self._format_template(custom_template, variables)
            if http_status == 429:
                tip = str(self.conf.get("error_429_custom_tip", "") or "").strip()
                if tip:
                    rendered += "\n" + self._format_template(tip, variables)
            return rendered

        enable_http_status = self.conf.get("enable_http_status_code", False)
        if enable_http_status and http_status > 0:
            summary = f"❌ {status_text} [HTTP {http_status}] ({elapsed:.2f}s)"
        else:
            summary = f"❌ {status_text} ({elapsed:.2f}s)"

        if self.conf.get("send_error_reason", True) and provider_message:
            safe_reason = provider_message.replace("图片下载失败", "图片获取未完成").replace("失败", "未完成")
            summary += f"\n原因: {safe_reason}"

        if variables["image_url"] and variables["image_url"] not in summary:
            summary += f"\n图片链接: {variables['image_url']}"

        if http_status == 429:
            tip = str(self.conf.get("error_429_custom_tip", "") or "").strip()
            if tip:
                summary += "\n" + self._format_template(tip, variables)

        send_full_error = self.conf.get("send_full_error", False)
        if send_full_error:
            context_lines = [
                f"错误类型: {error_type}",
                f"Provider Code: {provider_code or '-'}",
                f"Request ID: {variables['request_id'] or '-'}",
                f"API模式: {variables['api_mode'] or '-'}",
                f"端点: {variables['endpoint_type'] or '-'} {variables['endpoint'] or ''}".strip(),
                f"详细错误: {detail_text or provider_message or message}",
            ]
            return summary + "\n" + "\n".join(context_lines)

        if self.conf.get("send_error_context", False):
            parts = []
            if variables["request_id"]:
                parts.append(f"request_id={variables['request_id']}")
            if variables["endpoint_type"]:
                parts.append(f"endpoint={variables['endpoint_type']}")
            if variables["model"]:
                parts.append(f"model={variables['model']}")
            if parts:
                summary += "\n调试信息: " + " | ".join(parts)

        if self.conf.get("debug_mode", False):
            logger.error(f"调试模式错误详情: {detail}")
        return summary

    async def _call_api(
            self,
            image_bytes_list: List[bytes],
            prompt: str,
            override_model: str | None = None,
            resolution: Optional[str] = None,
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
            return_actual_model: bool = False,
            return_request_context: bool = False,
            on_attempt: Optional[Callable[[str, Any, int, bool], Awaitable[None]]] = None,
    ) -> Any:
        """按模型映射顺序调用接口，失败时切换到下一个热备模型。"""
        source_model = str(
            override_model or self.conf.get("model", "nano-banana") or "nano-banana"
        ).strip() or "nano-banana"
        candidate_models = self._get_model_failover_candidates(source_model)
        last_result: bytes | str | Dict[str, Any] = self._build_api_error(
            "config_error",
            "未找到可调用模型。",
            model=source_model,
        )
        last_status = 0
        last_context = self._get_request_context(source_model, source_model, bool(image_bytes_list))

        for index, candidate_model in enumerate(candidate_models):
            request_context = self._get_request_context(
                source_model,
                candidate_model,
                bool(image_bytes_list),
            )
            candidate_images = self._limit_reference_images(
                candidate_model,
                image_bytes_list,
                request_context["parameters"],
            )
            request_context["image_bytes_list"] = candidate_images
            result, http_status = await self._call_api_once(
                candidate_images,
                prompt,
                override_model=candidate_model,
                request_context=request_context,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                force_aspect_ratio=force_aspect_ratio,
            )
            if isinstance(result, bytes):
                if index > 0:
                    logger.info(
                        f"模型热备切换成功: source={source_model}, model={candidate_model}, "
                        f"attempt={index + 1}/{len(candidate_models)}"
                    )
                if return_request_context:
                    return result, http_status, request_context
                if return_actual_model:
                    return result, http_status, candidate_model
                return result, http_status

            last_result, last_status, last_context = result, http_status, request_context
            should_stop = self._should_stop_model_failover(http_status, result)
            if on_attempt and not should_stop and index + 1 < len(candidate_models):
                await on_attempt(request_context["actual_model"], result, http_status, False)
            if should_stop:
                logger.warning(
                    f"模型调用失败，命中违规内容或配置错误码并停止热备切换: "
                    f"source={source_model}, failed_model={candidate_model}, "
                    f"status={http_status}"
                )
                break
            if index + 1 < len(candidate_models):
                error_type = result.get("error_type", "unknown") if isinstance(result, dict) else "unknown"
                logger.warning(
                    f"模型调用失败，切换热备模型: source={source_model}, "
                    f"failed_model={candidate_model}, status={http_status}, "
                    f"error_type={error_type}, next_model={candidate_models[index + 1]}"
                )

        if return_request_context:
            return last_result, last_status, last_context
        if return_actual_model:
            return last_result, last_status, last_context["actual_model"]
        return last_result, last_status

    async def _call_api_once(
            self,
            image_bytes_list: List[bytes],
            prompt: str,
            override_model: str | None = None,
            request_context: Optional[Dict[str, Any]] = None,
            resolution: Optional[str] = None,
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
    ) -> Tuple[bytes | str | Dict[str, Any], int]:
        """
        调用API生成图片
        返回: (结果, HTTP状态码) 元组，其中结果可以是bytes(成功)或str(错误信息)，状态码为0表示未获取到
        """
        model_name = (request_context or {}).get("actual_model") or override_model or self.conf.get("model", "nano-banana")
        model_name = str(model_name or "").strip() or "nano-banana"
        if request_context is None:
            request_context = self._get_request_context(model_name, model_name, bool(image_bytes_list))
        parameters = request_context.get("parameters")
        api_mode = request_context.get("api_route") or "generic"
        endpoint_type = request_context.get("endpoint_type") or "chat_completions"
        route_model = request_context.get("route_model") or model_name
        final_url = ""

        def make_error(error_type: str, message: str, status: int = 0, **kwargs: Any) -> Tuple[Dict[str, Any], int]:
            return (
                self._build_api_error(
                    error_type,
                    message,
                    http_status=status,
                    api_mode=api_mode,
                    model=model_name,
                    endpoint_type=endpoint_type,
                    endpoint=final_url or base_url,
                    url=final_url or base_url,
                    image_count=len(image_bytes_list),
                    prompt=prompt,
                    **kwargs,
                ),
                status,
            )

        base_url = self._get_api_base_url()

        if not base_url:
            base_url = ""
            return make_error("config_error", "API URL 未配置", 0)

        api_key = await self._get_pool_api_key(api_mode)
        if not api_key:
            return make_error("config_error", "无可用 API Key (请在共享 Key 池中添加 Key)", 0)

        # --- 构造最终 Prompt (支持按模型指定预设提示词模板) ---
        final_prompt = self._build_final_prompt(prompt, model_name, len(image_bytes_list))

        headers = {
            "Connection": "keep-alive"
        }
        request_user_agent = " ".join(
            str(
                self.conf.get(
                    "request_user_agent",
                    self.DEFAULT_REQUEST_USER_AGENT,
                ) or ""
            ).splitlines()
        ).strip()
        if request_user_agent:
            headers["User-Agent"] = request_user_agent

        payload: Dict[str, Any] = {}
        form_data: aiohttp.FormData | None = None
        final_url = base_url

        if api_mode == "gemini":
            headers["Content-Type"] = "application/json"
            final_url = self._resolve_gemini_endpoint_url(base_url, model_name)
            headers["x-goog-api-key"] = api_key

            parts = [{"text": final_prompt}]
            for img in image_bytes_list:
                b64 = base64.b64encode(img).decode("utf-8")
                parts.append({
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": b64
                    }
                })

            payload = {
                "contents": [{"parts": parts}],
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                ],
                "toolConfig": {
                    "functionCallingConfig": {
                        "mode": "NONE"
                    }
                }
            }
            generation_config: Dict[str, Any] = {}
            if max_output_tokens := self._get_max_output_tokens(model_name, parameters):
                generation_config["maxOutputTokens"] = max_output_tokens
            if image_config := self._get_gemini_image_config(
                    model_name,
                    image_bytes_list,
                    aspect_ratio,
                    force_aspect_ratio,
                    parameters,
            ):
                generation_config["imageConfig"] = image_config
            if generation_config:
                payload["generationConfig"] = generation_config

        else:
            headers["Authorization"] = f"Bearer {api_key}"
            generic_endpoint_type = endpoint_type
            if generic_endpoint_type not in self.GENERIC_ENDPOINT_PATHS:
                generic_endpoint_type = self._get_generic_endpoint_type_for_model(
                    route_model,
                    has_images=len(image_bytes_list) > 0,
                    parameters=parameters,
                )
            final_url = self._resolve_generic_endpoint_url(base_url, generic_endpoint_type)
            endpoint_type = generic_endpoint_type

            if generic_endpoint_type == "images_edits" and not image_bytes_list:
                return make_error("request_build_error", "Images Edits 端点需要至少一张输入图片。", 0)

            if generic_endpoint_type == "images_edits":
                form_data = self._build_generic_images_edits_form(
                    model_name,
                    final_prompt,
                    image_bytes_list,
                    resolution,
                    aspect_ratio,
                    force_aspect_ratio,
                    parameters,
                )
            elif generic_endpoint_type == "images_generations":
                headers["Content-Type"] = "application/json"
                payload = self._build_generic_images_payload(
                    model_name,
                    final_prompt,
                    image_bytes_list,
                    resolution,
                    aspect_ratio,
                    force_aspect_ratio,
                    parameters,
                )
            else:
                headers["Content-Type"] = "application/json"
                messages = []
                if system_instruction := self._get_chat_completions_system_prompt():
                    messages.append({"role": "system", "content": system_instruction})

                if len(image_bytes_list) > 0:
                    # 包含图片的 Vision 请求结构
                    user_content_list = [{"type": "text", "text": final_prompt}]
                    for img in image_bytes_list:
                        user_content_list.append({
                            "type": "image_url",
                            "image_url": {"url": self._image_bytes_to_data_url(img)}
                        })
                    messages.append({"role": "user", "content": user_content_list})
                else:
                    # 纯文本请求结构
                    messages.append({"role": "user", "content": final_prompt})

                use_stream = self.conf.get("use_stream", True)
                payload = {
                    "model": model_name,
                    "stream": use_stream,
                    "messages": messages
                }
                if max_output_tokens := self._get_max_output_tokens(model_name, parameters):
                    payload["max_tokens"] = max_output_tokens

        safe_log_url = self._sanitize_request_log_url(final_url)
        logger.info(
            f"调用图片生成端点: model={model_name}, endpoint_type={endpoint_type}, "
            f"endpoint={safe_log_url}, image_count={len(image_bytes_list)}"
        )

        if form_data is not None:
            body_type = "multipart/form-data"
            request_parameters = self._build_images_edits_log_parameters(
                model_name,
                final_prompt,
                image_bytes_list,
                resolution,
                aspect_ratio,
                force_aspect_ratio,
                parameters,
            )
        else:
            body_type = "application/json"
            request_parameters = payload

        request_log = {
            "method": "POST",
            "url": safe_log_url,
            "api_mode": api_mode,
            "endpoint_type": endpoint_type,
            "body_type": body_type,
            "headers": self._sanitize_request_log_value(headers),
            "parameters": self._sanitize_request_log_value(request_parameters),
        }
        logger.info(
            "提交生图请求全部参数:\n"
            + json.dumps(request_log, ensure_ascii=False, indent=2)
        )

        timeout_cfg = _build_client_timeout(self.request_timeout, self.download_timeout)
        http_status = 0  # 初始化HTTP状态码

        try:
            if not self.iwf:
                return make_error("config_error", "工作流未初始化", 0)

            session_request_kwargs = self.iwf.get_request_kwargs()
            request_kwargs = {
                "headers": headers,
                "timeout": timeout_cfg,
                **session_request_kwargs,
            }
            if form_data is not None:
                request_kwargs["data"] = form_data
            else:
                request_kwargs["json"] = payload

            async with self.iwf.create_client_session(timeout=timeout_cfg) as session:
                async with session.post(
                    final_url,
                    **request_kwargs,
                ) as resp:

                    # 立即捕获HTTP状态码
                    http_status = resp.status
                    request_id = (
                        resp.headers.get("x-request-id")
                        or resp.headers.get("request-id")
                        or resp.headers.get("cf-ray")
                        or ""
                    )

                    # 先检查基本的HTTP错误
                    if resp.status == 404 and api_mode == "gemini":
                        return make_error(
                            "http_error",
                            f"API 404错误: 模型 '{model_name}' 不存在或路径错误。",
                            404,
                            detail=f"URL: {final_url}",
                            provider_message=f"模型 '{model_name}' 不存在或路径错误。",
                            request_id=request_id,
                        )

                    if resp.status != 200:
                        text = await resp.text()
                        try:
                            error_json = json.loads(text) if text.strip() else {}
                        except json.JSONDecodeError:
                            error_json = text
                        provider_fields = self._extract_provider_error_fields(error_json)
                        provider_message = provider_fields.get("provider_message") or text or f"HTTP {resp.status}"
                        return make_error(
                            "http_error",
                            f"API 请求失败 (HTTP {resp.status}): {provider_message}",
                            resp.status,
                            detail=self._safe_error_text(text, 2000),
                            raw_response=self._safe_error_text(text, 2000),
                            request_id=request_id,
                            **provider_fields,
                        )

                    if api_mode == "generic" and payload.get("stream"):
                        full_content = ""
                        buffer = b""
                        try:
                            # 修复流式 Chunk too big 问题：
                            # 使用 iter_chunked 绕过 aiohttp 默认的单行长度限制
                            async for chunk in resp.content.iter_chunked(4096):
                                buffer += chunk
                                while b'\n' in buffer:
                                    try:
                                        line_data, buffer = buffer.split(b'\n', 1)
                                        line_str = line_data.decode('utf-8').strip()

                                        if not line_str or line_str.startswith(":"):
                                            continue
                                        if line_str == "data: [DONE]":
                                            break
                                        if line_str.startswith("data: "):
                                            json_str = line_str[6:]
                                            try:
                                                chunk_json = json.loads(json_str)
                                                if "choices" in chunk_json and len(chunk_json["choices"]) > 0:
                                                    delta = chunk_json["choices"][0].get("delta", {})
                                                    if "content" in delta:
                                                        full_content += delta["content"]
                                            except json.JSONDecodeError:
                                                continue
                                    except ValueError:
                                        # 解码失败等情况，跳过当前行
                                        break

                            # 构造完整的响应对象，供后续提取图片使用
                            data = {
                                "choices": [{
                                    "message": {
                                        "content": full_content
                                    }
                                }]
                            }
                        except Exception as e:
                            logger.error(f"流式响应解析失败: {e}", exc_info=True)
                            return make_error(
                                "stream_parse_error",
                                f"流式响应解析错误: {e}",
                                http_status,
                                detail=str(e),
                                request_id=request_id,
                            )
                    else:
                        text = await resp.text()
                        try:
                            data = json.loads(text) if text.strip() else {}
                        except json.JSONDecodeError as e:
                            return make_error(
                                "json_parse_error",
                                f"响应不是有效 JSON: {e}",
                                http_status,
                                detail=self._safe_error_text(text, 2000),
                                raw_response=self._safe_error_text(text, 2000),
                                request_id=request_id,
                            )

                    if isinstance(data, dict) and "error" in data:
                        provider_fields = self._extract_provider_error_fields(data)
                        provider_message = provider_fields.get("provider_message") or json.dumps(
                            data["error"],
                            ensure_ascii=False,
                        )
                        error_type = (
                            "content_policy_violation"
                            if self._is_content_policy_violation(provider_fields)
                            else "upstream_error"
                        )
                        return make_error(
                            error_type,
                            provider_message,
                            http_status,
                            detail=data["error"],
                            request_id=request_id,
                            **provider_fields,
                        )

                    if safety_reason := self._get_content_policy_response_reason(data):
                        return make_error(
                            "safety_block",
                            f"上游安全拦截: {safety_reason}",
                            http_status,
                            detail={"safety_reason": safety_reason},
                            provider_message=safety_reason,
                            request_id=request_id,
                        )

                    candidates = self._extract_image_urls_from_response(data)
                    url_or_b64 = candidates[0] if candidates else None

                    if not url_or_b64:
                        # 检查是否启用"无图片时返回完整响应"选项
                        return_full = self.conf.get("return_full_response_on_no_image", False)
                        if return_full:
                            # 返回完整的响应数据，不截断
                            full_response = json.dumps(data, ensure_ascii=False, indent=2)
                            no_image_detail = f"生成失败，无图片数据。\n完整响应:\n{full_response}"
                        else:
                            if isinstance(data, dict):
                                response_shape = ", ".join(str(k) for k in data.keys()) or "空对象"
                            else:
                                response_shape = type(data).__name__
                            no_image_detail = f"生成失败，无图片数据。响应结构: {response_shape}"
                        return make_error(
                            "no_image",
                            "生成成功但响应中没有可识别的图片数据。",
                            http_status,
                            detail=no_image_detail,
                            provider_message=no_image_detail,
                            request_id=request_id,
                        )

                    decode_error: Exception | None = None
                    download_failed_urls: List[str] = []

                    for candidate in candidates:
                        if candidate.startswith("data:"):
                            try:
                                return (self._decode_data_url_image(candidate), http_status)
                            except Exception as e:
                                decode_error = e
                                logger.warning(f"图片Base64解码失败({e})，尝试下一个候选")
                                continue

                        downloaded_image = await self.iwf._download_image(candidate)
                        if downloaded_image:
                            return (downloaded_image, http_status)
                        download_failed_urls.append(candidate)

                    # 所有候选均失败：优先返回可手动访问的图片链接，其次返回解码错误
                    if download_failed_urls:
                        failed_url = download_failed_urls[0]
                        logger.warning(f"图片获取未完成，返回图片链接: {failed_url}")
                        return make_error(
                            "download_error",
                            "图片获取未完成，请手动访问链接查看。",
                            http_status,
                            detail=f"图片获取未完成，请手动访问链接查看: {failed_url}",
                            provider_message="图片获取未完成，请手动访问链接查看。",
                            image_url=failed_url,
                            request_id=request_id,
                        )

                    return make_error(
                        "image_decode_error",
                        f"图片Base64解码失败: {decode_error}",
                        http_status,
                        detail=str(decode_error),
                        request_id=request_id,
                    )

        except asyncio.TimeoutError:
            return make_error("timeout", "请求超时", 0)
        except Exception as e:
            logger.error(f"API 调用异常: {e}", exc_info=True)
            return make_error("system_error", f"系统错误: {e}", 0, detail=str(e))

    # 修复：使用 ctx=None 替代 *args 以避免 _empty() 错误，同时兼容框架传递的额外参数
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_figurine_request(self, event: AstrMessageEvent, ctx=None):
        if self.conf.get("prefix", True) and not event.is_at_or_wake_command:
            return

        text = event.message_str.strip()
        if not text:
            return

        tokens = text.split()
        if not tokens:
            return

        raw_cmd_token = tokens[0].strip()
        (
            command_token,
            batch_count,
            requested_batch_count,
            temp_model_idx,
            requested_resolution,
            requested_aspect_ratio,
        ) = self._parse_command_token(raw_cmd_token)
        consumed_tokens = 1

        cmd = command_token
        if not cmd:
            return
        if cmd in self.RESERVED_COMMAND_NAMES:
            logger.warning(f"通用消息处理器忽略专用命令: {cmd}")
            return

        if cmd in self._get_preset_list_commands():
            if maintenance_message := self._get_maintenance_message():
                yield self._reply_plain_result(event, maintenance_message)
                event.stop_event()
                return
            yield await self._build_preset_list_result(event)
            event.stop_event()
            return

        # 指令解析
        extra_prefixes = self._get_extra_prefixes()
        matched_extra_prefix = extra_prefixes[0]
        user_prompt = ""
        is_bnn = False
        bnn_preset_name = ""

        base_cmd = cmd
        append_text = ""

        # 检查是否允许追加自定义内容
        allow_append = self.conf.get("allow_append_to_preset", True)
        separator = self.conf.get("append_separator", "%")
        
        if allow_append and separator and separator in cmd:
            parts = cmd.split(separator, 1)
            if len(parts) == 2:
                base_cmd = parts[0].strip()
                append_text = parts[1].strip()
                logger.info(f"检测到分隔符'{separator}'分割: 基础命令='{base_cmd}', 追加内容='{append_text}'")

        if base_cmd == self._get_help_command():
            if maintenance_message := self._get_maintenance_message():
                yield self._reply_plain_result(event, maintenance_message)
            else:
                yield self._get_help_result(event)
            event.stop_event()
            return

        if base_cmd in extra_prefixes:
            matched_extra_prefix = base_cmd
            remaining_tokens = tokens[consumed_tokens:]
            user_prompt, bnn_preset_name = self._resolve_bnn_prompt(
                " ".join(remaining_tokens),
                allow_append,
                separator,
            )
            is_bnn = True
            if bnn_preset_name:
                logger.info(
                    f"自定义提示词命中预设: prefix='{matched_extra_prefix}', "
                    f"preset='{bnn_preset_name}'"
                )

        elif base_cmd in self.prompt_map:
            user_prompt = self.prompt_map[base_cmd]
            if append_text:
                user_prompt = user_prompt + append_text
                logger.info(f"将追加内容'{append_text}'添加到预设 prompt 后面")

        if not user_prompt:
            if not is_bnn:
                return

        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        sender_id = self._norm_id(event.get_sender_id())
        group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None

        # --- 图片获取 (融合逻辑) ---
        images_to_process = []
        is_text_to_image = False

        if self.iwf:
            # [修改] ImageWorkflow.get_images 现在不会自动获取头像
            img_bytes_list = await self.iwf.get_images(event)

            if not img_bytes_list:
                # [修改] 智能判断 BNN 模式
                if is_bnn:
                    # bnn 模式 + 无图 = 纯文生图
                    if not user_prompt:
                        yield self._reply_plain_result(event, f"请在指令后添加描述。例如: #{matched_extra_prefix} 一个可爱的女孩")
                        return
                    is_text_to_image = True
                    images_to_process = []
                    logger.info("BNN模式下未检测到图片，自动切换为纯文生图模式")
                else:
                    # 手办化等预设模式 + 无图 = 尝试取发送者头像 (兼容旧习惯)
                    logger.info(f"预设模式下未检测到图片，尝试获取发送者 [{sender_id}] 的头像...")
                    if avatar := await self.iwf._get_avatar(sender_id):
                        img_bytes_list = [avatar]
                        logger.info("成功获取发送者头像作为图生图源")
                    else:
                        yield self._reply_plain_result(event, "请发送或引用一张图片。")
                        return
            else:
                # 检测到图片，走图生图
                is_text_to_image = False
                logger.info("检测到明确的图片输入，模式确定为图生图")

            if not is_text_to_image and img_bytes_list:
                images_to_process = img_bytes_list

        if not is_bnn and user_prompt and not is_text_to_image:
            image_urls = self._extract_image_urls_from_text(user_prompt)
            if image_urls:
                logger.info(f"在预设内容中发现 {len(image_urls)} 个图片链接: {image_urls}")
                for image_url in image_urls:
                    if downloaded_image := await self._download_preset_image(image_url):
                        images_to_process.append(downloaded_image)
                        logger.info(f"成功下载预设内容中的图片: {image_url}")
                    else:
                        logger.warning(f"无法下载预设内容中的图片: {image_url}")

        display_cmd = cmd
        if is_bnn:
            if not is_text_to_image:
                MAX_IMAGES = self.conf.get("max_images_count", 10)
                if len(images_to_process) > MAX_IMAGES:
                    images_to_process = images_to_process[:MAX_IMAGES]
                    yield self._reply_plain_result(event, f"🎨 检测到 {len(img_bytes_list)} 张图片，已选取前 {MAX_IMAGES} 张…")

            display_cmd = bnn_preset_name or (user_prompt[:10] + '...' if len(user_prompt) > 10 else user_prompt)
        elif len(images_to_process) > 0:
            MAX_FIGURINE_IMAGES = self.conf.get("max_images_count", 10)
            if len(images_to_process) > MAX_FIGURINE_IMAGES:
                images_to_process = images_to_process[:MAX_FIGURINE_IMAGES]
                yield self._reply_plain_result(
                    event,
                    f"🎨 检测到 {len(img_bytes_list)} 张图片（含@用户头像），已选取前 {MAX_FIGURINE_IMAGES} 张…")

        if append_text:
            display_cmd = f"{base_cmd}%{append_text[:5]}..."

        override_model_name = None
        all_models = self._get_all_models()
        if temp_model_idx is not None:
            if 1 <= temp_model_idx <= len(all_models):
                override_model_name = all_models[temp_model_idx - 1]
            else:
                yield self._reply_plain_result(event, f"⚠️ 指定的模型序号 {temp_model_idx} 无效，将使用默认模型。")
        if override_model_name is None:
            override_model_name = self._get_command_model(base_cmd)

        display_label = display_cmd
        base_model_name = (self.conf.get("model", "nano-banana") or "nano-banana").strip() or "nano-banana"
        model_in_use = (override_model_name or base_model_name).strip() or base_model_name
        candidate_models = self._get_model_failover_candidates(model_in_use)
        initial_actual_model = candidate_models[0]
        candidate_contexts = [
            self._get_request_context(model_in_use, candidate_model, bool(images_to_process))
            for candidate_model in candidate_models
        ]
        initial_request_context = candidate_contexts[0]
        initial_request_context["image_bytes_list"] = self._limit_reference_images(
            initial_actual_model,
            images_to_process,
            initial_request_context["parameters"],
        )
        max_per_invocation_cost = max(
            self._get_required_invocation_cost(
                context["actual_model"],
                requested_resolution,
                image_bytes_list=self._limit_reference_images(
                    context["actual_model"],
                    images_to_process,
                    context["parameters"],
                ),
                aspect_ratio=requested_aspect_ratio,
                parameters=context["parameters"],
            )
            for context in candidate_contexts
        )
        required_cost = max_per_invocation_cost * batch_count
        allowed, deduction_source, deny_message = self._resolve_generation_access(
            event,
            sender_id,
            group_id,
            required_cost,
        )
        if not allowed:
            if deny_message:
                yield self._reply_plain_result(event, deny_message)
            return

        if requested_batch_count and requested_batch_count > batch_count:
            yield self._reply_plain_result(event, f"⚠️ 本次倍率最高为 {batch_count}，已按 {batch_count} 次并发生成。")

        endpoint_display = self._get_endpoint_display_for_request(
            initial_actual_model,
            has_images=bool(images_to_process),
            source_model=model_in_use,
        )
        show_model_info = self.conf.get("show_model_info", False)

        mode_prefix = ""
        action_type = "文生图" if is_text_to_image else "图生图"
        configured_batch_concurrency = _normalize_positive_int(
            self.conf.get("max_batch_concurrency", 4),
            4,
        )
        max_batch_concurrency = min(
            batch_count,
            configured_batch_concurrency,
            20,
        )
        batch_suffix = f" x{batch_count}" if batch_count > 1 else ""
        if batch_count > 1 and max_batch_concurrency < batch_count:
            batch_suffix += f"（并发{max_batch_concurrency}）"

        # 检查是否有自定义开始消息模板
        if is_text_to_image:
            custom_start_template = self.conf.get("custom_text2img_start_message", "").strip()
        else:
            custom_start_template = self.conf.get("custom_img2img_start_message", "").strip()
        
        if custom_start_template:
            # 使用自定义模板 - 添加所有可用参数
            info_msg = (custom_start_template
                .replace("{mode_prefix}", mode_prefix)
                .replace("{action_type}", action_type)
                .replace("{label}", display_label)
                .replace("{prompt}", user_prompt[:50] if user_prompt else display_label)
                .replace("{model}", model_in_use)
                .replace("{endpoint}", endpoint_display)
                .replace("{image_count}", str(len(images_to_process)))
                .replace("{batch_count}", str(batch_count))
                .replace("{max_batch_concurrency}", str(max_batch_concurrency)))
        else:
            # 使用默认格式
            info_msg = (
                f"🎨 收到{mode_prefix}{action_type}请求{batch_suffix}，正在生成 [{display_label}]...\n"
                f"端点: {endpoint_display}"
            )

        # 超限参考图阶梯加费提示（按首个候选模型估算；实际按各批次命中的模型结算）
        extra_cost_preview = self._get_extra_reference_image_charge(
            initial_actual_model,
            self._limit_reference_images(
                initial_actual_model,
                images_to_process,
                candidate_contexts[0]["parameters"],
            ),
            candidate_contexts[0]["parameters"],
        )
        if extra_cost_preview > 0:
            global_limit_preview = _normalize_positive_int(self.conf.get("max_images_count", 10), 10)
            params_preview = candidate_contexts[0]["parameters"]
            soft_preview = min(
                _normalize_nonnegative_int((params_preview or {}).get("reference_image_limit", 0)),
                global_limit_preview,
            )
            excess_preview = max(0, min(len(images_to_process), global_limit_preview) - soft_preview)
            info_msg += (
                f"\n🎨 检测到 {excess_preview} 张参考图超出软限，本次每批次将额外加收 {format_amount(extra_cost_preview)} 元。"
            )

        yield self._reply_plain_result(event, info_msg)

        batch_semaphore = asyncio.Semaphore(max_batch_concurrency)

        async def _record_failover_attempt(
            failed_model: str,
            failed_result: Any,
            failed_status: int,
            succeeded: bool,
        ):
            failed_context = self._get_request_context(
                model_in_use,
                failed_model,
                bool(images_to_process),
            )
            failed_context["image_bytes_list"] = self._limit_reference_images(
                failed_model,
                images_to_process,
                failed_context["parameters"],
            )
            await self._settle_usage_generation(
                event=event,
                source="chat",
                sender_id=sender_id,
                group_id=group_id,
                logical_model=model_in_use,
                actual_model=failed_context["actual_model"],
                has_images=bool(failed_context["image_bytes_list"]),
                outcome="failed",
                http_status=failed_status,
                output_count=0,
                charged_amount=0,
                deduction_source=None,
                note="热备切换前的中间失败",
                request_context=failed_context,
            )

        async def _call_api_with_batch_limit(batch_index: int):
            async with batch_semaphore:
                task_start_time = datetime.now()
                try:
                    result = await self._call_api(
                        images_to_process,
                        user_prompt,
                        override_model=override_model_name,
                        resolution=requested_resolution,
                        aspect_ratio=requested_aspect_ratio,
                        return_request_context=True,
                        on_attempt=_record_failover_attempt,
                    )
                except Exception as exc:
                    result = exc
                task_elapsed = (datetime.now() - task_start_time).total_seconds()
                return batch_index, result, task_elapsed

        start_time = datetime.now()
        tasks = [
            asyncio.create_task(_call_api_with_batch_limit(index))
            for index in range(1, batch_count + 1)
        ]

        success_count = 0
        failed_deduction_amount = 0
        content_policy_violation_detected = False
        content_policy_warning_context: Optional[Dict[str, Any]] = None
        first_error = None
        first_error_status = 0
        first_error_model = initial_actual_model
        first_error_context: Optional[Dict[str, Any]] = None

        for completed_task in asyncio.as_completed(tasks):
            index, result, elapsed = await completed_task

            if isinstance(result, Exception):
                await self._settle_usage_generation(
                    event=event,
                    source="chat",
                    sender_id=sender_id,
                    group_id=group_id,
                    logical_model=model_in_use,
                    actual_model=initial_request_context["actual_model"],
                    has_images=bool(initial_request_context["image_bytes_list"]),
                    outcome="failed",
                    http_status=0,
                    output_count=0,
                    charged_amount=0,
                    deduction_source=None,
                    note="聊天生成系统错误",
                    request_context=initial_request_context,
                )
                if first_error is None:
                    first_error = {
                        "type": "system_error",
                        "message": f"系统错误: {result}",
                        "detail": str(result),
                    }
                    first_error_status = 0
                    first_error_context = initial_request_context
                continue

            res, http_status, request_context = result
            actual_model = request_context["actual_model"]
            candidate_images = request_context["image_bytes_list"]
            parameters = request_context["parameters"]
            invocation_cost = self._get_required_invocation_cost(
                actual_model,
                requested_resolution,
                image_bytes_list=candidate_images,
                aspect_ratio=requested_aspect_ratio,
                parameters=parameters,
            )
            should_deduct = self._should_deduct_generation_result(
                res,
                http_status,
                actual_model,
                parameters,
            )
            deduction_amount = (
                invocation_cost
                if isinstance(res, bytes)
                else self._get_violation_deduction_cost(
                    actual_model,
                    requested_resolution,
                    candidate_images,
                    parameters,
                    aspect_ratio=requested_aspect_ratio,
                )
            )
            should_send_content_policy_warning = self._should_send_content_policy_warning(
                http_status,
                res,
            )
            outcome = "success" if isinstance(res, bytes) else "failed"
            await self._settle_usage_generation(
                event=event,
                source="chat",
                sender_id=sender_id,
                group_id=group_id,
                logical_model=model_in_use,
                actual_model=actual_model,
                has_images=bool(candidate_images),
                outcome=outcome,
                http_status=http_status,
                output_count=1 if isinstance(res, bytes) else 0,
                charged_amount=deduction_amount if should_deduct else 0,
                deduction_source=deduction_source if should_deduct else None,
                request_context=request_context,
            )

            if not isinstance(res, bytes):
                if should_send_content_policy_warning:
                    content_policy_violation_detected = True
                    if content_policy_warning_context is None:
                        content_policy_warning_context = {
                            "model": actual_model,
                            "elapsed": elapsed,
                            "batch_index": index,
                            "reason": self._get_content_policy_warning_reason(res),
                        }
                if should_deduct:
                    failed_deduction_amount += deduction_amount
                if first_error is None:
                    first_error = res
                    first_error_status = http_status
                    first_error_model = actual_model
                    first_error_context = request_context
                continue

            success_count += 1
            await self._record_daily_usage(sender_id, group_id)

            if success_count == 1 and base_cmd in self.prompt_map and not is_bnn:
                await self._save_preset_image(base_cmd, res)

            # 检查是否有自定义成功消息模板
            custom_success_template = self.conf.get("custom_success_message", "").strip()
            if custom_success_template:
                remaining_text = self._get_remaining_balance_text(
                    deduction_source,
                    sender_id,
                    group_id,
                )
                group_balance_text = self._get_group_balance_text(deduction_source, group_id)
                cost_text = self._get_generation_cost_text(deduction_source, should_deduct, deduction_amount)
                cost_value = self._get_generation_cost_value(deduction_source, should_deduct, deduction_amount)

                # 替换占位符
                message_text = custom_success_template.replace("{model}", actual_model).replace("{label}", display_label).replace("{image_count}", str(len(images_to_process))).replace("{elapsed}", f"{elapsed:.2f}").replace("{remaining}", remaining_text).replace("{group_balance}", group_balance_text).replace("{cost}", cost_value).replace("{prompt}", user_prompt[:50]).replace("{batch_count}", str(batch_count)).replace("{batch_index}", str(index)).replace("{max_batch_concurrency}", str(max_batch_concurrency))
            else:
                # 使用默认消息格式
                status_text = "生成成功"
                caption_parts = [f"✅ {status_text} ({elapsed:.2f}s)", f"预设: {display_label}"]
                if batch_count > 1:
                    caption_parts.append(f"批次: {index}/{batch_count}")

                if deduction_source == 'free':
                    caption_parts.append("余额: ∞")
                else:
                    if group_id and self.conf.get("enable_group_limit", False):
                        caption_parts.append(f"本群余额: {format_amount(self._get_group_balance(group_id))} 元")
                    if self.conf.get("enable_user_limit", True):
                        caption_parts.append(f"用户余额: {format_amount(self._get_user_balance(sender_id))} 元")

                if show_model_info:
                    caption_parts.append(f"模型: {actual_model}")

                caption_parts.append(f"本次消耗: {self._get_generation_cost_text(deduction_source, should_deduct, deduction_amount)}")

                message_text = " | ".join(caption_parts)

            yield self._build_image_result(event, res, message_text)

        total_elapsed = (datetime.now() - start_time).total_seconds()
        if content_policy_violation_detected:
            warning_context = content_policy_warning_context or {}
            warning_message = self._get_content_policy_warning_message(
                model=str(warning_context.get("model") or initial_actual_model),
                label=display_label,
                image_count=len(images_to_process),
                elapsed=float(warning_context.get("elapsed") or total_elapsed),
                remaining=self._get_remaining_balance_text(
                    deduction_source,
                    sender_id,
                    group_id,
                ),
                group_balance=self._get_group_balance_text(deduction_source, group_id),
                prompt=user_prompt,
                reason=str(warning_context.get("reason") or ""),
                batch_count=batch_count,
                batch_index=warning_context.get("batch_index"),
                max_batch_concurrency=max_batch_concurrency,
            )
            if failed_deduction_amount and deduction_source in ["group", "user"]:
                warning_message += f"\n本次违规已扣除费用：{format_amount(failed_deduction_amount)} 元"
            yield self._reply_plain_result(
                event,
                warning_message,
            )
        elif success_count == 0:
            status_text = "生成失败"
            msg = self._format_error_message(
                status_text,
                total_elapsed,
                first_error,
                first_error_status,
                model=first_error_model,
                api_mode=(first_error_context or initial_request_context)["api_route"],
                prompt=user_prompt,
                image_count=len(images_to_process),
            )
            if failed_deduction_amount and deduction_source in ["group", "user"]:
                msg += f"\n(失败状态码命中扣费设置，已扣除 {format_amount(failed_deduction_amount)} 元)"
            if show_model_info:
                msg += f"\n模型: {first_error_model}"
            yield self._reply_plain_result(event, msg)
        elif success_count < batch_count:
            summary = f"⚠️ 批量生成完成：成功 {success_count}/{batch_count}，失败 {batch_count - success_count} 次。"
            if failed_deduction_amount and deduction_source in ["group", "user"]:
                summary += f"\n其中失败请求按错误码设置扣除 {format_amount(failed_deduction_amount)} 元。"
            yield self._reply_plain_result(event, summary)

        event.stop_event()

    def _get_help_result(self, event: AstrMessageEvent):
        """生成合并转发帮助消息对象"""
        help_text = self._render_help_text()

        bot_uin = "2854196310"
        try:
            if hasattr(event, "robot") and event.robot:
                bot_uin = str(event.robot.id)
            elif hasattr(event, "bot") and hasattr(event.bot, "self_id"):
                bot_uin = str(event.bot.self_id)
        except:
            pass

        node = Node(
            name="手办化助手",
            uin=str(bot_uin),
            content=[Plain(help_text)]
        )
        return event.chain_result([Nodes(nodes=[node])])

    # 修复：使用 ctx=None 替代 *args
    @filter.command("文生图", prefix_optional=True)
    async def on_text_to_image(self, event: AstrMessageEvent, ctx=None):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        raw_cmd = event.message_str.strip()
        cmd_name = "文生图"
        override_model_name = None

        cmd_pos = raw_cmd.find(cmd_name)
        prompt = raw_cmd[cmd_pos + len(cmd_name):].strip() if cmd_pos != -1 else raw_cmd

        match = re.match(r"^[\(（](\d+)[\)）]\s*(.*)", prompt)
        if match:
            idx = int(match.group(1))
            prompt = match.group(2)
            all_models = self._get_all_models()
            if 1 <= idx <= len(all_models):
                override_model_name = all_models[idx - 1]
            else:
                yield self._reply_plain_result(event, f"⚠️ 指定的模型序号 {idx} 无效。")
                return

        prompt = prompt.strip()
        if not prompt:
            yield self._reply_plain_result(event, "请提供描述。用法: #文生图 [可选:(序号)] <描述>")
            return

        sender_id = self._norm_id(event.get_sender_id())
        group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None

        base_model_name = (self.conf.get("model", "nano-banana") or "nano-banana").strip() or "nano-banana"
        model_in_use = (override_model_name or base_model_name).strip() or base_model_name
        candidate_models = self._get_model_failover_candidates(model_in_use)
        initial_actual_model = candidate_models[0]
        initial_request_context = self._get_request_context(
            model_in_use,
            initial_actual_model,
            False,
        )
        required_cost = max(
            self._get_required_invocation_cost(
                context["actual_model"],
                parameters=context["parameters"],
            )
            for context in (
                self._get_request_context(model_in_use, candidate_model, False)
                for candidate_model in candidate_models
            )
        )
        allowed, deduction_source, deny_message = self._resolve_generation_access(
            event,
            sender_id,
            group_id,
            required_cost,
        )
        if not allowed:
            if deny_message:
                yield self._reply_plain_result(event, deny_message)
            return

        display_prompt = prompt[:10] + "..." if len(prompt) > 10 else prompt
        mode_prefix = ""
        
        endpoint_display = self._get_endpoint_display_for_request(
            initial_actual_model,
            has_images=False,
            source_model=model_in_use,
        )
        show_model_info = self.conf.get("show_model_info", False)
        
        # 检查是否有自定义文生图开始消息模板
        custom_start_template = self.conf.get("custom_text2img_start_message", "").strip()
        if custom_start_template:
            # 使用自定义模板 - 添加所有可用参数
            info_str = (custom_start_template
                .replace("{mode_prefix}", mode_prefix)
                .replace("{prompt}", display_prompt)
                .replace("{model}", model_in_use)
                .replace("{endpoint}", endpoint_display))
        else:
            # 使用默认格式
            info_str = (
                f"🎨 收到{mode_prefix}文生图请求，正在生成 [{display_prompt}]\n"
                f"端点: {endpoint_display}"
            )
        
        yield self._reply_plain_result(event, info_str)

        async def _record_text_to_image_failover_attempt(
            failed_model: str,
            failed_result: Any,
            failed_status: int,
            succeeded: bool,
        ):
            failed_context = self._get_request_context(
                model_in_use,
                failed_model,
                False,
            )
            await self._settle_usage_generation(
                event=event,
                source="chat",
                sender_id=sender_id,
                group_id=group_id,
                logical_model=model_in_use,
                actual_model=failed_context["actual_model"],
                has_images=False,
                outcome="failed",
                http_status=failed_status,
                output_count=0,
                charged_amount=0,
                deduction_source=None,
                note="热备切换前的中间失败",
                request_context=failed_context,
            )

        start_time = datetime.now()
        try:
            res, http_status, request_context = await self._call_api(
                [],
                prompt,
                override_model=override_model_name,
                return_request_context=True,
                on_attempt=_record_text_to_image_failover_attempt,
            )
        except Exception as exc:
            elapsed = (datetime.now() - start_time).total_seconds()
            await self._settle_usage_generation(
                event=event,
                source="chat",
                sender_id=sender_id,
                group_id=group_id,
                logical_model=model_in_use,
                actual_model=initial_request_context["actual_model"],
                has_images=False,
                outcome="failed",
                http_status=0,
                output_count=0,
                charged_amount=0,
                deduction_source=None,
                note="文生图系统错误",
                request_context=initial_request_context,
            )
            msg = self._format_error_message(
                "生成失败",
                elapsed,
                {"error_type": "system_error", "message": str(exc)},
                0,
                model=initial_request_context["actual_model"],
                api_mode=initial_request_context["api_route"],
                prompt=prompt,
                image_count=0,
            )
            if show_model_info:
                msg += f"\n模型: {initial_actual_model}"
            yield self._reply_plain_result(event, msg)
            event.stop_event()
            return
        elapsed = (datetime.now() - start_time).total_seconds()
        actual_model = request_context["actual_model"]
        parameters = request_context["parameters"]
        invocation_cost = self._get_required_invocation_cost(
            actual_model,
            image_bytes_list=request_context["image_bytes_list"],
            parameters=parameters,
        )
        should_deduct = self._should_deduct_generation_result(
            res,
            http_status,
            actual_model,
            parameters,
        )
        deduction_amount = (
            invocation_cost
            if isinstance(res, bytes)
            else self._get_violation_deduction_cost(
                actual_model,
                parameters=parameters,
            )
        )
        should_send_content_policy_warning = self._should_send_content_policy_warning(
            http_status,
            res,
        )
        outcome = "success" if isinstance(res, bytes) else "failed"
        await self._settle_usage_generation(
            event=event,
            source="chat",
            sender_id=sender_id,
            group_id=group_id,
            logical_model=model_in_use,
            actual_model=actual_model,
            has_images=False,
            outcome=outcome,
            http_status=http_status,
            output_count=1 if isinstance(res, bytes) else 0,
            charged_amount=deduction_amount if should_deduct else 0,
            deduction_source=deduction_source if should_deduct else None,
            request_context=request_context,
        )

        if isinstance(res, bytes):
            await self._record_daily_usage(sender_id, group_id)

            # 检查是否有自定义成功消息模板
            custom_success_template = self.conf.get("custom_success_message", "").strip()
            if custom_success_template:
                remaining_text = self._get_remaining_balance_text(
                    deduction_source,
                    sender_id,
                    group_id,
                )
                group_balance_text = self._get_group_balance_text(deduction_source, group_id)
                cost_text = self._get_generation_cost_text(deduction_source, should_deduct, deduction_amount)
                cost_value = self._get_generation_cost_value(deduction_source, should_deduct, deduction_amount)

                # 替换占位符（文生图没有携带图片，image_count为0）
                message_text = custom_success_template.replace("{model}", actual_model).replace("{label}", display_prompt).replace("{image_count}", "0").replace("{elapsed}", f"{elapsed:.2f}").replace("{remaining}", remaining_text).replace("{group_balance}", group_balance_text).replace("{cost}", cost_value).replace("{prompt}", prompt[:50])
            else:
                # 使用默认消息格式
                status_text = "生成成功"
                caption_parts = [f"✅ {status_text} ({elapsed:.2f}s)"]
                if deduction_source == 'free':
                    caption_parts.append("余额: ∞")
                else:
                    if group_id and self.conf.get("enable_group_limit", False):
                        caption_parts.append(f"本群余额: {format_amount(self._get_group_balance(group_id))} 元")
                    if self.conf.get("enable_user_limit", True):
                        caption_parts.append(f"用户余额: {format_amount(self._get_user_balance(sender_id))} 元")
                if show_model_info:
                    caption_parts.append(f"模型: {actual_model}")

                caption_parts.append(f"本次消耗: {self._get_generation_cost_text(deduction_source, should_deduct, deduction_amount)}")

                message_text = " | ".join(caption_parts)

            yield self._build_image_result(event, res, message_text)
        else:
            if should_send_content_policy_warning:
                msg = self._get_content_policy_warning_message(
                    model=actual_model,
                    label=display_prompt,
                    image_count=0,
                    elapsed=elapsed,
                    remaining=self._get_remaining_balance_text(
                        deduction_source,
                        sender_id,
                        group_id,
                    ),
                    group_balance=self._get_group_balance_text(deduction_source, group_id),
                    prompt=prompt,
                    reason=self._get_content_policy_warning_reason(res),
                )
                if should_deduct and deduction_source in ["group", "user"]:
                    msg += f"\n本次违规已扣除费用：{format_amount(deduction_amount)} 元"
            else:
                status_text = "生成失败"
                msg = self._format_error_message(
                    status_text,
                    elapsed,
                    res,
                    http_status,
                    model=actual_model,
                    api_mode=request_context["api_route"],
                    prompt=prompt,
                    image_count=0,
                )
                if show_model_info:
                    msg += f"\n模型: {actual_model}"
                if should_deduct and deduction_source in ["group", "user"]:
                    msg += f"\n(失败状态码命中扣费设置，已扣除 {format_amount(deduction_amount)} 元)"
            yield self._reply_plain_result(event, msg)

        event.stop_event()

    @filter.command("手办化预设增加", prefix_optional=True)
    async def add_preset_prompt(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            return

        cmd_prefix = "手办化预设增加"
        clean_msg = (event.message_str or "").strip().lstrip("#/ ").strip()

        if clean_msg.startswith(cmd_prefix):
            clean_msg = clean_msg[len(cmd_prefix):].strip()

        if ":" not in clean_msg:
            yield event.plain_result('格式错误, 示例: #手办化预设增加 触发词:提示词')
            return

        key, new_value = map(str.strip, clean_msg.split(":", 1))
        current_presets = self._normalized_preset_items()
        try:
            key = self._dashboard_command_name(key, "预设指令")
            existing_commands = {
                preset["command"]
                for preset in current_presets
                if preset["command"] != key
            }
            if conflict_message := self._preset_command_conflict_message(
                key,
                preset_commands=existing_commands,
            ):
                raise ValueError(conflict_message)
            if not new_value or len(new_value) > 20_000:
                raise ValueError("预设提示词不能为空且不能超过 20000 字符")
        except ValueError as exc:
            yield self._reply_plain_result(event, f"❌ {exc}")
            return

        schema_aliases = self._schema_default_preset_aliases()
        found = False
        for preset in current_presets:
            if preset["command"] == key:
                preset["prompt"] = new_value
                found = True
                break
        if not found:
            preset = {"__template_key": "preset", "command": key, "prompt": new_value}
            if alias := schema_aliases.get(key):
                preset["legacy_alias"] = alias
            current_presets.append(preset)

        self.conf["prompt_list"] = current_presets
        try:
            await self._persist_configuration()
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

        await self._load_prompt_map()
        yield event.plain_result(f"✅ 已保存预设:\n{key}:{new_value}")

    @filter.command("手办化预设查看", prefix_optional=True)
    async def preview_preset_prompt(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        raw = event.message_str.strip()
        parts = raw.split()
        if len(parts) < 2:
            yield event.plain_result("用法: #手办化预设查看 <关键词>")
            return

        keyword = parts[1].strip()
        prompt_content = self.prompt_map.get(keyword)

        if prompt_content:
            yield event.plain_result(f"🔍 关键词【{keyword}】的提示词：\n\n{prompt_content}")
        else:
            yield event.plain_result(f"❌ 未找到关键词【{keyword}】的预设。")

    def _image_data_to_bytes(self, image_data: object) -> bytes | None:
        if isinstance(image_data, bytes):
            return image_data
        if isinstance(image_data, str):
            if image_data.startswith("base64://"):
                try:
                    return base64.b64decode(image_data[len("base64://"):])
                except Exception:
                    return None
            if image_data.startswith("data:") and "," in image_data:
                try:
                    return base64.b64decode(image_data.split(",", 1)[1])
                except Exception:
                    return None
            path = Path(image_data)
            if path.exists():
                try:
                    return path.read_bytes()
                except Exception:
                    return None
        return None

    @staticmethod
    def _is_valid_image_bytes(data: bytes | None) -> bool:
        return bool(data and (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")))

    async def _build_preset_list_result(self, event: AstrMessageEvent):
        if not self.prompt_map:
            return event.plain_result("⚠️ 当前没有可用的预设。")

        built_in = []
        custom = []
        default_commands = self._get_default_preset_commands()

        for key in self.prompt_map:
            if key in default_commands:
                built_in.append(key)
            else:
                custom.append(key)

        built_in.sort()
        custom.sort()

        # 合并所有预设并按名称排序
        all_presets = []
        for preset in built_in:
            all_presets.append((preset, True))  # True表示内置预设
        for preset in custom:
            all_presets.append((preset, False))  # False表示自定义预设

        # 按预设名称排序
        all_presets.sort(key=lambda x: x[0])

        if not all_presets:
            return event.plain_result("⚠️ 当前没有可用的预设。")

        try:
            list_image = await self._render_preset_list_image(all_presets)
            if list_image:
                return event.chain_result([Image.fromBytes(list_image)])

        except Exception as e:
            logger.error(f"创建预设列表预览图失败: {e}")

        plain_msg = "📜 **可用预设列表**\n"
        plain_msg += "==================\n"

        if built_in:
            plain_msg += "📌 **内置预设**:\n"
            for preset in built_in:
                plain_msg += f"  • {preset}\n"
            plain_msg += "\n"

        if custom:
            plain_msg += "✨ **自定义预设**:\n"
            for preset in custom:
                plain_msg += f"  • {preset}\n"
        else:
            plain_msg += "✨ **自定义预设**: (无)\n\n"

        plain_msg += "==================\n"
        plain_msg += "使用方法: #预设名 [图片]"
        return event.plain_result(plain_msg)

    async def _render_preset_list_image(self, presets: List[Tuple[str, bool]]) -> bytes | None:
        render_func = getattr(self, "html_render", None)
        if not callable(render_func):
            logger.warning("FigurinePro 预设列表图片跳过：html_render 不可用")
            return None

        try:
            image_data = await render_func(
                self._build_preset_list_html(presets),
                {},
                False,
                dict(self.PRESET_LIST_RENDER_OPTIONS),
            )
        except Exception as exc:
            logger.warning(f"FigurinePro 预设列表图片渲染失败: {exc}")
            return None

        image_bytes = self._image_data_to_bytes(image_data)
        if not self._is_valid_image_bytes(image_bytes):
            logger.warning("FigurinePro 预设列表图片渲染返回了无效图片数据")
            return None
        return image_bytes

    def _build_preset_list_html(self, presets: List[Tuple[str, bool]]) -> str:
        cards = []
        for preset_name, is_built_in in presets:
            image_path = self._get_preset_image_path(preset_name)
            image_src = ""
            if image_path:
                try:
                    image_bytes = Path(image_path).read_bytes()
                    image_src = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
                except Exception:
                    image_src = ""

            label = "内置" if is_built_in else "自定义"
            label_class = "builtin" if is_built_in else "custom"
            image_html = (
                f'<img src="{image_src}" alt="{html.escape(preset_name)}">'
                if image_src
                else '<div class="placeholder">暂无预览</div>'
            )
            model = self._get_command_model(preset_name)
            model_html = f'<div class="model">{html.escape(model)}</div>' if model else ""
            cards.append(
                f"""
                <article class="card">
                  <div class="thumb">{image_html}</div>
                  <div class="meta">
                    <span class="badge {label_class}">{label}</span>
                    <h2>{html.escape(preset_name)}</h2>
                    {model_html}
                  </div>
                </article>
                """
            )

        command_text = " / ".join(f"#{cmd}" for cmd in self._get_preset_list_commands())
        template_name = str(self.conf.get("preset_list_template", "default") or "default").strip()
        template_file = self.PRESET_LIST_TEMPLATE_FILES.get(template_name, self.PRESET_LIST_TEMPLATE_FILES["default"])
        template_path = Path(__file__).resolve().parent / "templates" / template_file
        try:
            template = template_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(f"预设列表模板读取失败，使用内置兜底模板: {exc}")
            template = """
            <!doctype html>
            <html><head><meta charset="utf-8"></head><body>
              <main>
                <h1>{{ title }}</h1>
                <p>{{ subtitle }}</p>
                <div>{{ command_text }}</div>
                <section>{{ cards_html }}</section>
              </main>
            </body></html>
            """

        replacements = {
            "{{ title }}": "手办化预设列表",
            "{{ subtitle }}": "每个快捷预设名会在生成成功后更新预览图",
            "{{ command_text }}": html.escape(command_text),
            "{{ cards_html }}": "".join(cards),
        }
        for placeholder, value in replacements.items():
            template = template.replace(placeholder, value)
        return template

    # ---------------- 统计与存储 ----------------

    async def _load_user_balances(self):
        self.user_balances = await self._load_balance_data(
            self.user_balances_file, self.legacy_user_counts_file
        )

    async def _load_group_balances(self):
        self.group_balances = await self._load_balance_data(
            self.group_balances_file, self.legacy_group_counts_file
        )

    async def _load_balance_data(self, balance_file: Path, legacy_counts_file: Path) -> Dict[str, int]:
        """优先读取余额文件（厘）；不存在时迁移旧「次数」文件（次 × 汇率 → 厘）。"""
        legacy = not balance_file.exists()
        source = legacy_counts_file if legacy else balance_file
        if not source.exists():
            return {}
        try:
            content = await asyncio.to_thread(source.read_text, "utf-8")
            values = json.loads(content)
        except Exception:
            return {}
        if not isinstance(values, dict):
            return {}
        balances: Dict[str, int] = {}
        for subject_id, value in values.items():
            key = str(subject_id or "").strip()
            if not key:
                continue
            raw = _normalize_nonnegative_int(value)
            if legacy:
                balances[key] = yuan_to_amount(raw * LEGACY_COUNT_TO_YUAN)
            else:
                balances[key] = raw
        return balances

    async def _save_user_balances(self):
        try:
            data = json.dumps(self.user_balances, indent=4)
            await asyncio.to_thread(self.user_balances_file.write_text, data, "utf-8")
        except:
            pass

    def _get_user_balance(self, uid: str) -> int:
        return self.user_balances.get(self._norm_id(uid), 0)

    async def _deduct_user_balance(self, uid: str, amount: int = 0):
        uid = self._norm_id(uid)
        balance = self._get_user_balance(uid)
        if amount <= 0 or balance <= 0:
            return
        deduction = min(amount, balance)
        self.user_balances[uid] = balance - deduction
        await self._save_user_balances()

    async def _load_group_balances(self):
        self.group_balances = await self._load_balance_data(
            self.group_balances_file, self.legacy_group_counts_file
        )

    async def _save_group_balances(self):
        try:
            data = json.dumps(self.group_balances, indent=4)
            await asyncio.to_thread(self.group_balances_file.write_text, data, "utf-8")
        except:
            pass

    def _get_group_balance(self, group_id: str) -> int:
        return self.group_balances.get(self._norm_id(group_id), 0)

    async def _deduct_group_balance(self, group_id: str, amount: int = 0):
        gid = self._norm_id(group_id)
        balance = self._get_group_balance(gid)
        if amount <= 0 or balance <= 0:
            return
        deduction = min(amount, balance)
        self.group_balances[gid] = balance - deduction
        await self._save_group_balances()

    async def _load_user_checkin_data(self):
        if not self.user_checkin_file.exists():
            self.user_checkin_data = {}
            return
        try:
            content = await asyncio.to_thread(self.user_checkin_file.read_text, "utf-8")
            self.user_checkin_data = json.loads(content)
        except:
            self.user_checkin_data = {}

    async def _save_user_checkin_data(self):
        try:
            data = json.dumps(self.user_checkin_data, indent=4)
            await asyncio.to_thread(self.user_checkin_file.write_text, data, "utf-8")
        except:
            pass

    async def _load_daily_stats(self):
        if not self.daily_stats_file.exists():
            self.daily_stats = {"date": "", "users": {}, "groups": {}}
            return
        try:
            content = await asyncio.to_thread(self.daily_stats_file.read_text, "utf-8")
            self.daily_stats = json.loads(content)
        except:
            self.daily_stats = {"date": "", "users": {}, "groups": {}}

    async def _save_daily_stats(self):
        try:
            data = json.dumps(self.daily_stats, indent=4)
            await asyncio.to_thread(self.daily_stats_file.write_text, data, "utf-8")
        except:
            pass

    async def _record_daily_usage(self, user_id: str, group_id: str | None):
        today = datetime.now().strftime("%Y-%m-%d")

        if self.daily_stats.get("date") != today:
            self.daily_stats = {
                "date": today,
                "users": {},
                "groups": {}
            }

        uid = self._norm_id(user_id)
        current_u = self.daily_stats["users"].get(uid, 0)
        self.daily_stats["users"][uid] = current_u + 1

        if group_id:
            gid = self._norm_id(group_id)
            current_g = self.daily_stats["groups"].get(gid, 0)
            self.daily_stats["groups"][gid] = current_g + 1

        await self._save_daily_stats()

    async def _load_preset_images(self):
        if not self.preset_images_file.exists():
            self.preset_images = {}
            return
        try:
            content = await asyncio.to_thread(self.preset_images_file.read_text, "utf-8")
            self.preset_images = json.loads(content)
        except:
            self.preset_images = {}

    async def _save_preset_images(self):
        try:
            data = json.dumps(self.preset_images, indent=4)
            await asyncio.to_thread(self.preset_images_file.write_text, data, "utf-8")
        except:
            pass

    async def _save_preset_image(self, preset_key: str, image_bytes: bytes):
        """保存预设图片到文件和记录中"""
        try:
            # 生成文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{preset_key}_{timestamp}.png"
            filepath = self.preset_images_dir / filename

            # 保存图片文件
            await asyncio.to_thread(filepath.write_bytes, image_bytes)

            # 删除旧的图片文件（如果存在）
            if preset_key in self.preset_images:
                old_filename = self.preset_images[preset_key]
                old_filepath = self.preset_images_dir / old_filename
                if old_filepath.exists():
                    await asyncio.to_thread(old_filepath.unlink)

            # 更新记录
            self.preset_images[preset_key] = filename
            await self._save_preset_images()

            logger.info(f"已保存预设图片: {preset_key} -> {filename}")
            return True
        except Exception as e:
            logger.error(f"保存预设图片失败: {preset_key}, 错误: {e}")
            return False

    def _get_preset_image_path(self, preset_key: str) -> Optional[str]:
        """获取预设图片的文件路径"""
        if preset_key not in self.preset_images:
            return None

        filename = self.preset_images[preset_key]
        filepath = self.preset_images_dir / filename

        if filepath.exists():
            return str(filepath)
        else:
            # 文件不存在，清理记录
            del self.preset_images[preset_key]
            asyncio.create_task(self._save_preset_images())
            return None

    async def _cleanup_preset_images(self, max_age_days: int = 30):
        """清理超过指定天数的预设图片"""
        try:
            current_time = datetime.now()
            cleaned_count = 0

            for preset_key, filename in list(self.preset_images.items()):
                filepath = self.preset_images_dir / filename
                if filepath.exists():
                    # 获取文件创建时间
                    file_time = datetime.fromtimestamp(filepath.stat().st_mtime)
                    age_days = (current_time - file_time).days

                    if age_days > max_age_days:
                        # 删除文件和记录
                        await asyncio.to_thread(filepath.unlink)
                        del self.preset_images[preset_key]
                        cleaned_count += 1
                        logger.info(f"清理过期预设图片: {preset_key} ({filename})")

            if cleaned_count > 0:
                await self._save_preset_images()
                logger.info(f"预设图片清理完成，共清理 {cleaned_count} 个文件")

            return cleaned_count
        except Exception as e:
            logger.error(f"清理预设图片失败: {e}")
            return 0

    @filter.command("预设图片清理", prefix_optional=True)
    async def on_cleanup_preset_images(self, event: AstrMessageEvent):
        """清理过期的预设图片"""
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            yield event.plain_result("❌ 只有管理员可以执行此操作。")
            return

        # 默认清理30天前的图片
        max_age_days = 30
        args = event.message_str.strip().split()
        if len(args) > 1 and args[1].isdigit():
            max_age_days = int(args[1])

        cleaned_count = await self._cleanup_preset_images(max_age_days)

        total_images = len(self.preset_images)
        msg = f"✅ 预设图片清理完成！\n"
        msg += f"📊 清理了 {cleaned_count} 个过期图片\n"
        msg += f"📁 当前剩余 {total_images} 个预设图片\n"
        msg += f"⏰ 清理条件: 超过 {max_age_days} 天的图片"

        yield event.plain_result(msg)

    @filter.command("预设图片统计", prefix_optional=True)
    async def on_preset_images_stats(self, event: AstrMessageEvent):
        """输出预设图片统计信息"""
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            yield event.plain_result("❌ 只有管理员可以执行此操作。")
            return

        total_images = len(self.preset_images)

        # 统计文件大小
        total_size = 0
        for filename in self.preset_images.values():
            filepath = self.preset_images_dir / filename
            if filepath.exists():
                total_size += filepath.stat().st_size

        # 转换为MB
        total_size_mb = total_size / (1024 * 1024)

        # 显示每个预设的图片信息
        msg = f"📊 **预设图片统计**\n"
        msg += f"==================\n"
        msg += f"📁 总预设数: {total_images}\n"
        msg += f"💾 总大小: {total_size_mb:.2f} MB\n"
        msg += f"📂 存储目录: {self.preset_images_dir}\n\n"

        if total_images > 0:
            msg += "📸 **详细列表**:\n"
            for preset, filename in sorted(self.preset_images.items()):
                filepath = self.preset_images_dir / filename
                if filepath.exists():
                    size_mb = filepath.stat().st_size / (1024 * 1024)
                    msg += f"  • {preset}: {size_mb:.2f} MB\n"

        yield event.plain_result(msg)

    @filter.command("手办化今日统计", prefix_optional=True)
    async def get_daily_stats_report(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            yield event.plain_result("❌ 权限不足")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            yield event.plain_result(f"📊 {today} 今日暂无统计数据。")
            return

        users_sorted = sorted(self.daily_stats["users"].items(), key=lambda x: x[1], reverse=True)[:10]
        groups_sorted = sorted(self.daily_stats["groups"].items(), key=lambda x: x[1], reverse=True)[:10]

        msg = f"📊 **手办化今日统计 ({today})**\n"
        msg += "--------------------\n"
        msg += "👥 **群组生成排行**:\n"
        if groups_sorted:
            for i, (gid, count) in enumerate(groups_sorted):
                msg += f"{i + 1}. 群{gid}: {count}张\n"
        else:
            msg += "(无数据)\n"

        msg += "\n👤 **用户生成排行**:\n"
        if users_sorted:
            for i, (uid, count) in enumerate(users_sorted):
                msg += f"{i + 1}. {uid}: {count}张\n"
        else:
            msg += "(无数据)\n"

        yield event.plain_result(msg)

    @filter.command("手办化签到", prefix_optional=True)
    async def on_checkin(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.conf.get("enable_checkin", False):
            yield event.plain_result("📅 签到未开启。")
            return

        uid = self._norm_id(event.get_sender_id())
        today = datetime.now().strftime("%Y-%m-%d")

        if self.user_checkin_data.get(uid) == today:
            yield event.plain_result(f"已签到。当前余额: {format_amount(self._get_user_balance(uid))} 元")
            return

        reward = _normalize_charge_amount(self.conf.get("checkin_fixed_reward", 3), 3)
        if self.conf.get("enable_random_checkin", False):
            max_r = _normalize_charge_amount(self.conf.get("checkin_random_reward_max", 5), 5)
            reward = random.randint(1, max(1, max_r))

        await self._adjust_usage_balance(
            event=event,
            subject_type="user",
            subject_id=uid,
            amount=reward,
            source="checkin",
            note="每日签到奖励",
        )
        self.user_checkin_data[uid] = today
        await self._save_user_checkin_data()

        yield event.plain_result(f"🎉 签到成功 +{format_amount(reward)} 元。")

    @filter.command("手办化增加用户余额", prefix_optional=True)
    async def on_add_user_balance(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            return

        text = event.message_str.strip()
        at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
        target, amount = None, 0

        if at_seg:
            target = self._norm_id(at_seg.qq)
            match = re.search(r"(\d+(?:\.\d{1,3})?)\s*$", text)
            if match:
                amount = yuan_to_amount(match.group(1))
        else:
            match = re.search(r"(\d+)\s+(\d+(?:\.\d{1,3})?)", text)
            if match:
                target, amount = self._norm_id(match.group(1)), yuan_to_amount(match.group(2))

        if not target:
            return
        if amount <= 0:
            yield event.plain_result("❌ 请输入有效的金额（正数，精确到 0.001 元），例：#手办化增加用户余额 @用户 0.5")
            return

        old_balance = self._get_user_balance(target)
        new_balance = await self._adjust_usage_balance(
            event=event,
            subject_type="user",
            subject_id=target,
            amount=amount,
            source="chat_admin",
            actor=self._norm_id(event.get_sender_id()),
            note="聊天管理员增加用户余额",
            snapshot_identity=False,
        )

        msg = f"✅ 已为用户 {target} 增加 {format_amount(amount)} 元。\n"
        msg += f"📊 变动: {format_amount(old_balance)} + {format_amount(amount)} = {format_amount(new_balance)} 元\n"
        msg += f"👤 用户余额: {format_amount(new_balance)} 元"
        if gid := event.get_group_id():
            msg += f"\n👥 本群余额: {format_amount(self._get_group_balance(self._norm_id(gid)))} 元"

        yield event.plain_result(msg)

    @filter.command("手办化增加群组余额", prefix_optional=True)
    async def on_add_group_balance(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            return

        match = re.search(r"(\d+)\s+(\d+(?:\.\d{1,3})?)", event.message_str.strip())
        if match:
            gid, amount = self._norm_id(match.group(1)), yuan_to_amount(match.group(2))

            if amount <= 0:
                yield event.plain_result("❌ 请输入有效的金额（正数，精确到 0.001 元），例：#手办化增加群组余额 123456 0.5")
                return

            old_balance = self._get_group_balance(gid)
            new_balance = await self._adjust_usage_balance(
                event=event,
                subject_type="group",
                subject_id=gid,
                amount=amount,
                source="chat_admin",
                actor=self._norm_id(event.get_sender_id()),
                note="聊天管理员增加群组余额",
                snapshot_identity=False,
            )

            msg = f"✅ 已为群 {gid} 增加 {format_amount(amount)} 元。\n"
            msg += f"📊 变动: {format_amount(old_balance)} + {format_amount(amount)} = {format_amount(new_balance)} 元\n"
            msg += f"👥 本群余额: {format_amount(new_balance)} 元"

            yield event.plain_result(msg)

    @filter.command("手办化查询余额", prefix_optional=True)
    async def on_query_balance(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        uid = self._norm_id(event.get_sender_id())

        if self.is_global_admin(event):
            at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
            if at_seg:
                uid = self._norm_id(at_seg.qq)
            else:
                parts = event.message_str.strip().split()
                if len(parts) > 1 and parts[1].isdigit():
                    uid = self._norm_id(parts[1])

        msg = f"👤 用户 {uid} 余额: {format_amount(self._get_user_balance(uid))} 元"
        if gid := event.get_group_id():
            msg += f"\n👥 本群余额: {format_amount(self._get_group_balance(self._norm_id(gid)))} 元"

        yield event.plain_result(msg)

    async def terminate(self):
        if self.iwf:
            await self.iwf.terminate()
        if self.usage_store:
            try:
                await self.usage_store.close()
            except Exception as exc:
                logger.warning(f"关闭用量账本失败: {exc}")
            finally:
                self.usage_store = None
        logger.info("[FigurinePro] 插件已终止")
