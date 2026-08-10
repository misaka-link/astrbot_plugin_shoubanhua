import asyncio
import base64
import functools
import html
import io
import json
import math
import random
import re
import unicodedata
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Reply, Plain, Node, Nodes
from astrbot.core.platform.astr_message_event import AstrMessageEvent

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
    "支持第三方 OpenAI 绘图格式、Gemini 路由和 Seedream 专属图片参数的文生图/图生图插件",
    "1.8.0",
    "https://github.com/misaka-link/astrbot_plugin_shoubanhua",
)
class FigurineProPlugin(Star):
    BUILT_IN_CMD_MAP = {
        "手办化": "figurine_1", "手办化2": "figurine_2", "手办化3": "figurine_3",
        "手办化4": "figurine_4", "手办化5": "figurine_5", "手办化6": "figurine_6",
        "Q版化": "q_version",
        "痛屋化": "pain_room_1", "痛屋化2": "pain_room_2",
        "痛车化": "pain_car",
        "cos化": "cos", "cos自拍": "cos_selfie",
        "孤独的我": "clown",
        "第三视角": "view_3", "鬼图": "ghost", "第一视角": "view_1",
    }

    GENERIC_ENDPOINT_PATHS = {
        "chat_completions": "/v1/chat/completions",
        "images_generations": "/v1/images/generations",
        "images_edits": "/v1/images/edits",
    }
    DEFAULT_GENERIC_API_URL = "https://api.bltcy.ai/v1/chat/completions"
    DEFAULT_GEMINI_API_URL = "https://generativelanguage.googleapis.com"
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

    IMAGE_QUALITY_OPTIONS = {"low", "medium", "high", "auto"}
    IMAGE_MODERATION_OPTIONS = {"auto", "low"}
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
                    logger.info(f"ImageWorkflow 使用 SOCKS 代理: {normalized_proxy}")
                else:
                    self.proxy = normalized_proxy
                    logger.info(f"ImageWorkflow 使用 HTTP 代理: {normalized_proxy}")

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
            
            # 统计各种来源的图片数量
            reply_image_count = 0
            message_image_count = 0

            logger.info("=== 开始获取图片资源 ===")
            logger.info(f"消息平台: {event.platform}")
            logger.info(f"消息内容: {event.message_str}")

            # 1. 处理回复链中的图片
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and seg.chain:
                    logger.info(f"发现回复链，长度: {len(seg.chain)}")
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            logger.info("在回复链中发现图片")
                            if s_chain.url and (img := await self._load_bytes(s_chain.url)):
                                img_bytes_list.append(img)
                                reply_image_count += 1
                                logger.info("成功从回复链URL加载图片")
                            elif s_chain.file and (img := await self._load_bytes(s_chain.file)):
                                img_bytes_list.append(img)
                                reply_image_count += 1
                                logger.info("成功从回复链文件加载图片")

            # 2. 处理当前消息中的图片
            for seg in event.message_obj.message:
                if isinstance(seg, Image):
                    logger.info("在当前消息中发现图片")
                    if seg.url and (img := await self._load_bytes(seg.url)):
                        img_bytes_list.append(img)
                        message_image_count += 1
                        logger.info("成功从当前消息URL加载图片")
                    elif seg.file and (img := await self._load_bytes(seg.file)):
                        img_bytes_list.append(img)
                        message_image_count += 1
                        logger.info("成功从当前消息文件加载图片")

            # 3. 处理@用户（支持多@）
            for seg in event.message_obj.message:
                if isinstance(seg, At):
                    at_user_ids.append(str(seg.qq))
                    logger.info(f"发现@用户: {seg.qq}")

            # 4. 处理命令文本中的@用户（从文本提取QQ号）
            import re
            text_at_matches = re.findall(r'@(\d+)', event.message_str)
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

        self.user_counts_file = self.plugin_data_dir / "user_counts.json"
        self.group_counts_file = self.plugin_data_dir / "group_counts.json"
        self.user_checkin_file = self.plugin_data_dir / "user_checkin.json"
        self.daily_stats_file = self.plugin_data_dir / "daily_stats.json"
        self.preset_images_file = self.plugin_data_dir / "preset_images.json"
        self.preset_images_dir = self.plugin_data_dir / "preset_images"

        self.user_counts: Dict[str, int] = {}
        self.group_counts: Dict[str, int] = {}
        self.user_checkin_data: Dict[str, str] = {}
        self.daily_stats: Dict[str, Any] = {}
        self.prompt_map: Dict[str, str] = {}
        self.preset_images: Dict[str, str] = {}  # 预设词 -> 图片文件名映射
        self.request_timeout = 120
        self.download_timeout = 240
        self.max_download_bytes = 120 * 1024 * 1024

        self.generic_key_index = 0
        self.gemini_key_index = 0
        self.key_lock = asyncio.Lock()

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

        await self._load_user_counts()
        await self._load_group_counts()
        await self._load_user_checkin_data()
        await self._load_daily_stats()
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
            image_bytes_list = self._limit_reference_images(model_name, image_bytes_list)
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
        batch_semaphore = asyncio.Semaphore(max_concurrency)

        async def call_generation(batch_index: int):
            async with batch_semaphore:
                try:
                    result = await self._call_api(
                        image_bytes_list,
                        user_prompt,
                        override_model=model_name,
                        aspect_ratio=normalized_aspect_ratio,
                        force_aspect_ratio=bool(normalized_aspect_ratio),
                        return_actual_model=True,
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
                continue

            generated_image, http_status, actual_model = result
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
                continue

            successful_models.append(actual_model)
            sender_id = self._norm_id(event.get_sender_id())
            group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None
            await self._record_daily_usage(sender_id, group_id)
            await event.send(self._reply_chain_result(event, [
                Image.fromBytes(generated_image),
                Plain(f"LLM 工具生成完成 {batch_index}/{normalized_batch_count} | 模型: {actual_model}"),
            ]))

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
            if hasattr(self.conf, "save"):
                self.conf.save()
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
        prompt_list = self.conf.get("prompt_list", [])
        prompts_cfg = self.conf.get("prompts", {})
        if not isinstance(prompt_list, list):
            prompt_list = []

        migrated: List[Dict[str, str]] = []
        changed = False
        seen = set()

        def add_item(command: Any, prompt: Any):
            nonlocal changed
            key = str(command or "").strip()
            value = str(prompt or "").strip()
            if not key or not value or key in seen:
                return
            seen.add(key)
            migrated.append({"__template_key": "preset", "command": key, "prompt": value})

        for item in prompt_list:
            if isinstance(item, dict):
                key = str(item.get("command") or item.get("指令") or item.get("name") or "").strip()
                value = str(item.get("prompt") or item.get("提示词") or item.get("value") or "").strip()
                if key and value:
                    add_item(key, value)
                    if item.get("__template_key") != "preset" or set(item.keys()) != {"__template_key", "command", "prompt"}:
                        changed = True
                else:
                    changed = True
            elif isinstance(item, str) and ":" in item:
                key, value = item.split(":", 1)
                add_item(key, value)
                changed = True
            elif item:
                changed = True

        if isinstance(prompts_cfg, dict):
            for key, value in prompts_cfg.items():
                if key in self.BUILT_IN_CMD_MAP.values() or key in self.BUILT_IN_CMD_MAP:
                    continue
                if isinstance(value, dict) and "default" in value:
                    add_item(key, value["default"])
                    changed = True
                elif isinstance(value, str):
                    add_item(key, value)
                    changed = True

        if changed:
            self.conf["prompt_list"] = migrated
            try:
                if hasattr(self.conf, "save"):
                    self.conf.save()
                logger.info(f"已自动迁移 prompt_list 到对象列表格式，共 {len(migrated)} 条")
            except Exception as e:
                logger.error(f"自动迁移 prompt_list 配置失败: {e}")

    async def _load_prompt_map(self):
        self.prompt_map.clear()

        # 1. 内置基础映射 (硬编码的指令)
        for k in self.BUILT_IN_CMD_MAP.keys():
            self.prompt_map[k] = "[内置预设]"

        # 2. 从配置的 prompts 加载
        prompts_cfg = self.conf.get("prompts", {})
        if isinstance(prompts_cfg, dict):
            for k, v in prompts_cfg.items():
                if isinstance(v, dict) and "default" in v:
                    self.prompt_map[k] = v["default"]
                elif isinstance(v, str):
                    self.prompt_map[k] = v

        # 3. 从 prompt_list 加载
        prompt_list = self.conf.get("prompt_list", [])
        if isinstance(prompt_list, list):
            for item in prompt_list:
                if isinstance(item, dict):
                    k = str(item.get("command") or item.get("指令") or item.get("name") or "").strip()
                    v = str(item.get("prompt") or item.get("提示词") or item.get("value") or "").strip()
                    if k and v:
                        self.prompt_map[k] = v
                elif isinstance(item, str) and ":" in item:
                    k, v = item.split(":", 1)
                    self.prompt_map[k.strip()] = v.strip()

    def _get_custom_preset_prompt(self, preset_name: str) -> Optional[str]:
        prompt = self.prompt_map.get((preset_name or "").strip())
        if prompt and prompt != "[内置预设]":
            return prompt
        return None

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

    def _get_custom_command_model_bindings_text(self) -> str:
        """返回自定义提示词前缀与实际选择模型的帮助文本。"""
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
        return "\n".join(
            f"{command}{' ' * (max_command_width - self._get_text_display_width(command))} -> {model}"
            for command, model in bindings
        )

    def _render_help_text(self) -> str:
        help_text = str(self.conf.get("help_text", "帮助文档未配置") or "")
        return help_text.replace(
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

    def _get_model_parameter_map(self) -> Dict[str, Dict[str, Any]]:
        mapping: Dict[str, Dict[str, Any]] = {}
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

        def add_item(
                model: Any,
                quality: Any = "auto",
                moderation: Any = "auto",
                adaptive_aspect_ratio: Any = False,
                adaptive_resolution: Any = "1K",
                auto_upgrade_1k_adaptive_resolution: Any = False,
                default_resolution: Any = "auto",
                send_default_size: Any = False,
                max_output_tokens: Any = 0,
                deduction_count: Any = 1,
                deduct_on_violation: Any = False,
                force_resolution_limit: Any = False,
                enable_gpt_parameters: Any = False,
                omit_n_parameter: Any = False,
                enable_gemini_parameters: Any = False,
                gemini_resolution: Any = "auto",
                gemini_adaptive_aspect_ratio: Any = False,
                gemini_aspect_ratio: Any = "auto",
                reference_image_limit: Any = 0,
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
                seedream_optimize_prompt_mode: Any = "standard",
        ):
            model_name = str(model or "").strip()
            if not model_name:
                return
            auto_upgrade_1k = normalize_bool(auto_upgrade_1k_adaptive_resolution)
            mapping[model_name] = {
                "quality": normalize_option(quality, self.IMAGE_QUALITY_OPTIONS, "auto"),
                "moderation": normalize_option(moderation, self.IMAGE_MODERATION_OPTIONS, "auto"),
                "adaptive_aspect_ratio": normalize_bool(adaptive_aspect_ratio),
                "adaptive_resolution": normalize_resolution(adaptive_resolution),
                "auto_upgrade_1k_adaptive_resolution": auto_upgrade_1k,
                "default_resolution": normalize_default_resolution(default_resolution),
                "send_default_size": normalize_bool(send_default_size),
                "max_output_tokens": _normalize_nonnegative_int(max_output_tokens),
                "deduction_count": _normalize_positive_int(deduction_count, 1),
                "deduct_on_violation": normalize_bool(deduct_on_violation),
                "force_resolution_limit": (
                    normalize_bool(force_resolution_limit) and not auto_upgrade_1k
                ),
                "enable_gpt_parameters": normalize_bool(enable_gpt_parameters),
                "omit_n_parameter": normalize_bool(omit_n_parameter),
                "enable_gemini_parameters": normalize_bool(enable_gemini_parameters),
                "gemini_resolution": normalize_gemini_resolution(gemini_resolution),
                "gemini_adaptive_aspect_ratio": normalize_bool(gemini_adaptive_aspect_ratio),
                "gemini_aspect_ratio": normalize_gemini_aspect_ratio(gemini_aspect_ratio),
                "reference_image_limit": _normalize_nonnegative_int(reference_image_limit),
                "enable_seedream_parameters": normalize_bool(enable_seedream_parameters),
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
                "seedream_optimize_prompt_mode": normalize_seedream_prompt_optimization(seedream_optimize_prompt_mode),
            }

        if isinstance(raw_list, dict):
            for model_name, parameters in raw_list.items():
                if isinstance(parameters, dict):
                    add_item(
                        model_name,
                        get_value(parameters, "quality", "质量", default="auto"),
                        get_value(parameters, "moderation", "审核", default="auto"),
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
                        get_value(parameters, "deduction_count", "该模型扣除次数", "扣除次数", default=1),
                        get_value(
                            parameters,
                            "deduct_on_violation",
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
                get_value(item, "deduction_count", "该模型扣除次数", "扣除次数", default=1),
                get_value(
                    item,
                    "deduct_on_violation",
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
                    "seedream_optimize_prompt_mode",
                    "Seedream提示词优化模式",
                    default="standard",
                ),
            )

        return mapping

    def _get_model_parameters(self, model_name: str) -> Dict[str, str]:
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
        if not parameters or not parameters.get("enable_gpt_parameters"):
            return {}
        return {
            "quality": parameters["quality"],
            "moderation": parameters["moderation"],
        }

    def _get_max_output_tokens(self, model_name: str) -> int:
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
        model_limit = _normalize_nonnegative_int((parameters or {}).get("max_output_tokens", 0))
        if model_limit:
            return model_limit
        return _normalize_nonnegative_int(
            self.conf.get("max_output_tokens", self.conf.get("gemini_max_output_tokens", 0))
        )

    def _get_seedream_parameters(self, model_name: str) -> Optional[Dict[str, Any]]:
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
        if not parameters or not parameters.get("enable_seedream_parameters"):
            return None
        return parameters

    def _get_reference_image_limit(self, model_name: str) -> int:
        global_limit = _normalize_positive_int(self.conf.get("max_images_count", 10), 10)
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
        model_limit = _normalize_nonnegative_int((parameters or {}).get("reference_image_limit", 0))
        return min(model_limit, global_limit) if model_limit else global_limit

    def _limit_reference_images(self, model_name: str, image_bytes_list: List[bytes]) -> List[bytes]:
        return image_bytes_list[:self._get_reference_image_limit(model_name)]

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
    ) -> Optional[str]:
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
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
    ) -> Dict[str, str]:
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
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
    ) -> Optional[Tuple[str, str, bool]]:
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
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
    ) -> Optional[str]:
        details = self._get_adaptive_image_size_details(
            model_name,
            image_bytes_list,
            resolution,
            aspect_ratio,
            force_aspect_ratio,
        )
        return details[0] if details else None

    def _should_omit_n_parameter(self, model_name: str) -> bool:
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
        return bool(
            parameters
            and parameters.get("enable_gpt_parameters")
            and parameters.get("omit_n_parameter")
        )

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

    def _build_seedream_adaptive_size(
            self,
            model_name: str,
            image_bytes_list: List[bytes],
            aspect_ratio: Optional[str],
    ) -> str:
        parameters = self._get_seedream_parameters(model_name) or {}
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
    ) -> Dict[str, Any]:
        parameters = self._get_seedream_parameters(model_name)
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
    ) -> Dict[str, str]:
        model_parameters = self._get_model_parameter_map().get((model_name or "").strip())
        if not model_parameters:
            return {}

        parameters = self._get_model_parameters(model_name)
        adaptive_size = self._get_adaptive_image_size(
            model_name,
            image_bytes_list,
            resolution,
            aspect_ratio,
            force_aspect_ratio,
        )
        if adaptive_size:
            parameters["size"] = adaptive_size
        elif model_parameters.get("send_default_size"):
            parameters["size"] = model_parameters["default_resolution"]
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
                if hasattr(self.conf, "save"):
                    self.conf.save()
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
            if hasattr(self.conf, "save"):
                self.conf.save()
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

    def _get_api_route_for_model(self, model_name: str) -> str:
        gemini_models = self._normalize_model_list(self.conf.get("gemini_model_list", []))
        return "gemini" if (model_name or "").strip() in gemini_models else "generic"

    def _get_generic_endpoint_type_for_model(self, model_name: str, has_images: bool) -> str:
        """根据端点模型列表和输入图片决定 Generic 模式的请求端点。

        同一模型同时配置在 Images Edits 与 Images Generations 列表时，
        图生图优先走 Edits，文生图走 Generations。
        """
        if not self._has_generic_endpoint_model_routes():
            return "chat_completions"

        normalized_model = (model_name or "").strip()
        if not normalized_model:
            return "chat_completions"

        if has_images:
            if self._get_seedream_parameters(normalized_model) and normalized_model in self._get_endpoint_models("images_generations"):
                endpoint_order = ("images_generations", "images_edits", "chat_completions")
            else:
                endpoint_order = ("images_edits", "images_generations", "chat_completions")
        else:
            endpoint_order = ("images_generations", "chat_completions")

        for endpoint_type in endpoint_order:
            if normalized_model in self._get_endpoint_models(endpoint_type):
                return endpoint_type

        return "chat_completions"

    def _get_endpoint_display_for_request(self, model_name: str, has_images: bool) -> str:
        """返回开始消息中显示的当前请求端点。"""
        if self._get_api_route_for_model(model_name) == "gemini":
            return "generateContent"

        endpoint_type = self._get_generic_endpoint_type_for_model(model_name, has_images)
        return self.GENERIC_ENDPOINT_DISPLAY_NAMES.get(
            endpoint_type,
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
            self.conf["model"] = new_model
            try:
                if hasattr(self.conf, "save"):
                    self.conf.save()
            except:
                pass
            yield event.plain_result(f"✅ 切换成功！\n当前默认模型: **{new_model}**")
        else:
            yield event.plain_result(f"❌ 序号无效。")

    async def _get_pool_api_key(self, mode: str, allow_generic_fallback: bool = False) -> str | None:
        keys = []
        async with self.key_lock:
            if mode == "gemini":
                keys = self.conf.get("gemini_api_keys", [])
                if not keys and allow_generic_fallback:
                    keys = self.conf.get("generic_api_keys", [])
                    if keys:
                        key = keys[self.generic_key_index]
                        self.generic_key_index = (self.generic_key_index + 1) % len(keys)
                        return key
            else:
                keys = self.conf.get("generic_api_keys", [])

            if not keys: return None

            if mode == "gemini":
                key = keys[self.gemini_key_index]
                self.gemini_key_index = (self.gemini_key_index + 1) % len(keys)
                return key
            else:
                key = keys[self.generic_key_index]
                self.generic_key_index = (self.generic_key_index + 1) % len(keys)
                return key

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

    def _get_api_base_url(self, api_mode: str) -> Tuple[str, bool]:
        """Return the configured base URL and whether Gemini reuses the Generic service."""
        generic_url = str(
            self.conf.get("generic_api_url", self.DEFAULT_GENERIC_API_URL) or ""
        ).strip()
        if api_mode != "gemini":
            return generic_url, False

        # A Generic service root can expose both OpenAI and Gemini-compatible APIs.
        # Keep the legacy Gemini URL for users who configured a full OpenAI endpoint.
        if self._is_generic_service_root(generic_url):
            return generic_url, True

        gemini_url = str(
            self.conf.get("gemini_api_url", self.DEFAULT_GEMINI_API_URL) or ""
        ).strip()
        return gemini_url, False

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

    @staticmethod
    def _resolve_gemini_endpoint_url(url: str, model_name: str) -> str:
        base_url = (url or "").strip() or "https://generativelanguage.googleapis.com"
        model = (model_name or "").strip()
        if "{model}" in base_url:
            return base_url.replace("{model}", model)

        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        lower_path = path.lower()

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
        sensitive_names = {"api_key", "apikey", "key", "token", "access_token"}
        sanitized_query = [
            (name, "<redacted>" if name.lower() in sensitive_names else value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return parsed._replace(query=urlencode(sanitized_query)).geturl()

    def _build_images_edits_log_parameters(
            self,
            model_name: str,
            final_prompt: str,
            image_bytes_list: List[bytes],
            resolution: Optional[str],
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
    ) -> Dict[str, Any]:
        parameters: Dict[str, Any] = {
            "model": model_name,
            "prompt": final_prompt,
        }
        if not self._should_omit_n_parameter(model_name):
            parameters["n"] = "1"
        parameters.update(self._get_image_request_parameters(
            model_name,
            image_bytes_list,
            resolution,
            aspect_ratio,
            force_aspect_ratio,
        ))
        parameters["image"] = "<image omitted>"
        return parameters

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
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model_name,
            "prompt": final_prompt,
        }
        seedream_parameters = self._get_seedream_parameters(model_name)
        if seedream_parameters:
            payload.update(self._get_seedream_request_parameters(
                model_name,
                image_bytes_list,
                aspect_ratio,
                force_aspect_ratio,
            ))
        else:
            if not self._should_omit_n_parameter(model_name):
                payload["n"] = 1
            payload.update(self._get_image_request_parameters(
                model_name,
                image_bytes_list,
                resolution,
                aspect_ratio,
                force_aspect_ratio,
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
    ) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("model", model_name)
        form.add_field("prompt", final_prompt)
        if not self._should_omit_n_parameter(model_name):
            form.add_field("n", "1")
        for field_name, value in self._get_image_request_parameters(
                model_name,
                image_bytes_list,
                resolution,
                aspect_ratio,
                force_aspect_ratio,
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
            msg = "❌ 本群或您的使用次数已用尽 (优先扣除群次数)。"
        else:
            msg = "❌ 您的使用次数已用完。"

        if self.conf.get("enable_checkin", False) and self.conf.get("enable_user_limit", True):
            msg += "\n📅 可发送 \"#手办化签到\" 获取次数（触发前缀/唤醒请按实际配置调整）。"

        return msg

    def _get_required_invocation_cost(
            self,
            model_name: str = "",
            resolution: Optional[str] = None,
            has_images: bool = False,
            image_bytes_list: Optional[List[bytes]] = None,
            aspect_ratio: Optional[str] = None,
    ) -> int:
        if image_bytes_list and self._get_api_route_for_model(model_name) == "generic":
            endpoint_type = self._get_generic_endpoint_type_for_model(
                model_name,
                has_images=True,
            )
            if endpoint_type in {"images_generations", "images_edits"}:
                adaptive_details = self._get_adaptive_image_size_details(
                    model_name,
                    image_bytes_list,
                    resolution,
                    aspect_ratio,
                )
                if adaptive_details and adaptive_details[2]:
                    return _normalize_positive_int(
                        self.conf.get("resolution_2k_cost", 2),
                        2,
                    )

        parameters = self._get_model_parameter_map().get((model_name or "").strip())
        return _normalize_positive_int((parameters or {}).get("deduction_count", 1), 1)

    def _get_violation_deduction_cost(self, model_name: str) -> int:
        """违规失败固定按实际调用模型的基础扣次结算。"""
        parameters = self._get_model_parameter_map().get((model_name or "").strip())
        return _normalize_positive_int((parameters or {}).get("deduction_count", 1), 1)

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
    ) -> bool:
        if isinstance(result, bytes):
            return True
        if not self._should_send_content_policy_warning(http_status, result):
            return False

        parameters = self._get_model_parameter_map().get((model_name or "").strip())
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
            await self._decrease_group_count(group_id, amount)
        elif deduction_source == "user":
            await self._decrease_user_count(sender_id, amount)

    def _get_remaining_count_text(
            self,
            deduction_source: Optional[str],
            sender_id: str,
            group_id: Optional[str],
    ) -> str:
        if deduction_source == "free":
            return "∞"

        remaining_parts = []
        if self.conf.get("enable_user_limit", True):
            remaining_parts.append(str(self._get_user_count(sender_id)))
        if group_id and self.conf.get("enable_group_limit", False):
            remaining_parts.append(f"群{self._get_group_count(group_id)}")
        return "/".join(remaining_parts) if remaining_parts else "∞"

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
            if self._get_group_count(group_id) >= required_cost:
                return True, "group", None

        if self.conf.get("enable_user_limit", True):
            if self._get_user_count(sender_id) >= required_cost:
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
                message_id = raw_message.get("message_id") or raw_message.get("id")

        if message_id in (None, ""):
            return []
        return [Reply(id=message_id)]

    def _reply_plain_result(self, event: AstrMessageEvent, text: str):
        return event.chain_result([*self._build_reply_chain(event), Plain(text)])

    def _reply_chain_result(self, event: AstrMessageEvent, chain: List[Any]):
        return event.chain_result([*self._build_reply_chain(event), *chain])

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
        actual_model = source_model

        for index, candidate_model in enumerate(candidate_models):
            actual_model = candidate_model
            candidate_images = self._limit_reference_images(candidate_model, image_bytes_list)
            result, http_status = await self._call_api_once(
                candidate_images,
                prompt,
                override_model=candidate_model,
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
                if return_actual_model:
                    return result, http_status, candidate_model
                return result, http_status

            last_result, last_status = result, http_status
            if self._should_stop_model_failover(http_status, result):
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

        if return_actual_model:
            return last_result, last_status, actual_model
        return last_result, last_status

    async def _call_api_once(
            self,
            image_bytes_list: List[bytes],
            prompt: str,
            override_model: str | None = None,
            resolution: Optional[str] = None,
            aspect_ratio: Optional[str] = None,
            force_aspect_ratio: bool = False,
    ) -> Tuple[bytes | str | Dict[str, Any], int]:
        """
        调用API生成图片
        返回: (结果, HTTP状态码) 元组，其中结果可以是bytes(成功)或str(错误信息)，状态码为0表示未获取到
        """
        model_name = override_model or self.conf.get("model", "nano-banana")
        api_mode = self._get_api_route_for_model(model_name)
        endpoint_type = "gemini_generate_content" if api_mode == "gemini" else "chat_completions"
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

        base_url, gemini_uses_generic_service = self._get_api_base_url(api_mode)

        if not base_url:
            base_url = ""
            return make_error("config_error", "API URL 未配置", 0)

        api_key = await self._get_pool_api_key(
            api_mode,
            allow_generic_fallback=gemini_uses_generic_service,
        )
        if not api_key:
            key_pool_name = "gemini 或 generic" if gemini_uses_generic_service else api_mode
            return make_error("config_error", f"无可用 API Key (请在 {key_pool_name} 池中添加Key)", 0)

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
            if max_output_tokens := self._get_max_output_tokens(model_name):
                generation_config["maxOutputTokens"] = max_output_tokens
            if image_config := self._get_gemini_image_config(
                    model_name,
                    image_bytes_list,
                    aspect_ratio,
                    force_aspect_ratio,
            ):
                generation_config["imageConfig"] = image_config
            if generation_config:
                payload["generationConfig"] = generation_config

        else:
            headers["Authorization"] = f"Bearer {api_key}"
            routed_endpoint_type = self._get_generic_endpoint_type_for_model(
                model_name,
                has_images=len(image_bytes_list) > 0,
            )
            if routed_endpoint_type:
                generic_endpoint_type = routed_endpoint_type
                final_url = self._resolve_generic_endpoint_url(base_url, generic_endpoint_type)
            else:
                generic_endpoint_type = self._detect_generic_endpoint_type(base_url)
                if self._is_generic_base_url(base_url):
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
                if max_output_tokens := self._get_max_output_tokens(model_name):
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

                    url_or_b64 = self._extract_image_url_from_response(data)

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

                    if url_or_b64.startswith("data:"):
                        try:
                            b64 = url_or_b64.split(",", 1)[-1]
                            return (base64.b64decode(b64), http_status)
                        except Exception as e:
                            return make_error(
                                "image_decode_error",
                                f"图片Base64解码失败: {e}",
                                http_status,
                                detail=str(e),
                                request_id=request_id,
                            )
                    else:
                        # 尝试下载图片，如果下载失败则返回图片链接
                        downloaded_image = await self.iwf._download_image(url_or_b64)
                        if downloaded_image:
                            return (downloaded_image, http_status)
                        else:
                            logger.warning(f"图片获取未完成，返回图片链接: {url_or_b64}")
                            return make_error(
                                "download_error",
                                "图片获取未完成，请手动访问链接查看。",
                                http_status,
                                detail=f"图片获取未完成，请手动访问链接查看: {url_or_b64}",
                                provider_message="图片获取未完成，请手动访问链接查看。",
                                image_url=url_or_b64,
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
            val = self.prompt_map.get(base_cmd)
            if val and val != "[内置预设]":
                user_prompt = val
                if append_text:
                    user_prompt = user_prompt + append_text
                    logger.info(f"将追加内容'{append_text}'添加到预设prompt后面")

        if not user_prompt and not is_bnn:
            cmd_map = self.BUILT_IN_CMD_MAP
            if base_cmd in cmd_map:
                key = cmd_map[base_cmd]
                user_prompt = self.prompt_map.get(key) or self.prompt_map.get(base_cmd)
                if append_text:
                    user_prompt = user_prompt + append_text
                    logger.info(f"将追加内容'{append_text}'添加到映射命令prompt后面")

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
        max_per_invocation_cost = max(
            self._get_required_invocation_cost(
                candidate_model,
                requested_resolution,
                image_bytes_list=images_to_process,
                aspect_ratio=requested_aspect_ratio,
            )
            for candidate_model in candidate_models
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
        
        yield self._reply_plain_result(event, info_msg)

        batch_semaphore = asyncio.Semaphore(max_batch_concurrency)

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
                        return_actual_model=True,
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

        for completed_task in asyncio.as_completed(tasks):
            index, result, elapsed = await completed_task

            if isinstance(result, Exception):
                if first_error is None:
                    first_error = {
                        "type": "system_error",
                        "message": f"系统错误: {result}",
                        "detail": str(result),
                    }
                    first_error_status = 0
                continue

            res, http_status, actual_model = result
            invocation_cost = self._get_required_invocation_cost(
                actual_model,
                requested_resolution,
                image_bytes_list=images_to_process,
                aspect_ratio=requested_aspect_ratio,
            )
            should_deduct = self._should_deduct_generation_result(
                res,
                http_status,
                actual_model,
            )
            deduction_amount = (
                invocation_cost
                if isinstance(res, bytes)
                else self._get_violation_deduction_cost(actual_model)
            )
            should_send_content_policy_warning = self._should_send_content_policy_warning(
                http_status,
                res,
            )
            if should_deduct:
                await self._deduct_generation_cost(
                    deduction_source,
                    sender_id,
                    group_id,
                    deduction_amount,
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
                continue

            success_count += 1
            await self._record_daily_usage(sender_id, group_id)

            if success_count == 1 and base_cmd in self.prompt_map and not is_bnn:
                await self._save_preset_image(base_cmd, res)

            # 检查是否有自定义成功消息模板
            custom_success_template = self.conf.get("custom_success_message", "").strip()
            if custom_success_template:
                remaining_text = self._get_remaining_count_text(
                    deduction_source,
                    sender_id,
                    group_id,
                )
                
                # 替换占位符
                message_text = custom_success_template.replace("{model}", actual_model).replace("{label}", display_label).replace("{image_count}", str(len(images_to_process))).replace("{elapsed}", f"{elapsed:.2f}").replace("{remaining}", remaining_text).replace("{prompt}", user_prompt[:50]).replace("{batch_count}", str(batch_count)).replace("{batch_index}", str(index)).replace("{max_batch_concurrency}", str(max_batch_concurrency))
            else:
                # 使用默认消息格式
                status_text = "生成成功"
                caption_parts = [f"✅ {status_text} ({elapsed:.2f}s)", f"预设: {display_label}"]
                if batch_count > 1:
                    caption_parts.append(f"批次: {index}/{batch_count}")

                if deduction_source == 'free':
                    caption_parts.append("剩余: ∞")
                else:
                    if group_id and self.conf.get("enable_group_limit", False):
                        caption_parts.append(f"本群剩余: {self._get_group_count(group_id)}")
                    if self.conf.get("enable_user_limit", True):
                        caption_parts.append(f"用户剩余: {self._get_user_count(sender_id)}")

                if show_model_info:
                    caption_parts.append(f"模型: {actual_model}")

                message_text = " | ".join(caption_parts)

            yield self._reply_chain_result(event, [Image.fromBytes(res), Plain(message_text)])

        total_elapsed = (datetime.now() - start_time).total_seconds()
        if content_policy_violation_detected:
            warning_context = content_policy_warning_context or {}
            warning_message = self._get_content_policy_warning_message(
                model=str(warning_context.get("model") or initial_actual_model),
                label=display_label,
                image_count=len(images_to_process),
                elapsed=float(warning_context.get("elapsed") or total_elapsed),
                remaining=self._get_remaining_count_text(
                    deduction_source,
                    sender_id,
                    group_id,
                ),
                prompt=user_prompt,
                reason=str(warning_context.get("reason") or ""),
                batch_count=batch_count,
                batch_index=warning_context.get("batch_index"),
                max_batch_concurrency=max_batch_concurrency,
            )
            if failed_deduction_amount and deduction_source in ["group", "user"]:
                warning_message += f"\n本次违规已扣除次数：{failed_deduction_amount} 次"
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
                api_mode=self._get_api_route_for_model(first_error_model),
                prompt=user_prompt,
                image_count=len(images_to_process),
            )
            if failed_deduction_amount and deduction_source in ["group", "user"]:
                msg += f"\n(失败状态码命中扣次设置，已扣除 {failed_deduction_amount} 次)"
            if show_model_info:
                msg += f"\n模型: {first_error_model}"
            yield self._reply_plain_result(event, msg)
        elif success_count < batch_count:
            summary = f"⚠️ 批量生成完成：成功 {success_count}/{batch_count}，失败 {batch_count - success_count} 次。"
            if failed_deduction_amount and deduction_source in ["group", "user"]:
                summary += f"\n其中失败请求按错误码设置扣除 {failed_deduction_amount} 次。"
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
        required_cost = max(
            self._get_required_invocation_cost(candidate_model)
            for candidate_model in candidate_models
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
        
        endpoint_display = self._get_endpoint_display_for_request(initial_actual_model, has_images=False)
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

        start_time = datetime.now()
        res, http_status, actual_model = await self._call_api(
            [],
            prompt,
            override_model=override_model_name,
            return_actual_model=True,
        )
        elapsed = (datetime.now() - start_time).total_seconds()
        invocation_cost = self._get_required_invocation_cost(actual_model)
        should_deduct = self._should_deduct_generation_result(
            res,
            http_status,
            actual_model,
        )
        deduction_amount = (
            invocation_cost
            if isinstance(res, bytes)
            else self._get_violation_deduction_cost(actual_model)
        )
        should_send_content_policy_warning = self._should_send_content_policy_warning(
            http_status,
            res,
        )
        if should_deduct:
            await self._deduct_generation_cost(
                deduction_source,
                sender_id,
                group_id,
                deduction_amount,
            )

        if isinstance(res, bytes):
            await self._record_daily_usage(sender_id, group_id)

            # 检查是否有自定义成功消息模板
            custom_success_template = self.conf.get("custom_success_message", "").strip()
            if custom_success_template:
                remaining_text = self._get_remaining_count_text(
                    deduction_source,
                    sender_id,
                    group_id,
                )
                
                # 替换占位符（文生图没有携带图片，image_count为0）
                message_text = custom_success_template.replace("{model}", actual_model).replace("{label}", display_prompt).replace("{image_count}", "0").replace("{elapsed}", f"{elapsed:.2f}").replace("{remaining}", remaining_text).replace("{prompt}", prompt[:50])
            else:
                # 使用默认消息格式
                status_text = "生成成功"
                caption_parts = [f"✅ {status_text} ({elapsed:.2f}s)"]
                if deduction_source == 'free':
                    caption_parts.append("剩余: ∞")
                else:
                    if group_id and self.conf.get("enable_group_limit", False):
                        caption_parts.append(f"本群剩余: {self._get_group_count(group_id)}")
                    if self.conf.get("enable_user_limit", True):
                        caption_parts.append(f"用户剩余: {self._get_user_count(sender_id)}")
                if show_model_info:
                    caption_parts.append(f"模型: {actual_model}")

                message_text = " | ".join(caption_parts)

            yield self._reply_chain_result(event, [Image.fromBytes(res), Plain(message_text)])
        else:
            if should_send_content_policy_warning:
                msg = self._get_content_policy_warning_message(
                    model=actual_model,
                    label=display_prompt,
                    image_count=0,
                    elapsed=elapsed,
                    remaining=self._get_remaining_count_text(
                        deduction_source,
                        sender_id,
                        group_id,
                    ),
                    prompt=prompt,
                    reason=self._get_content_policy_warning_reason(res),
                )
                if should_deduct and deduction_source in ["group", "user"]:
                    msg += f"\n本次违规已扣除次数：{deduction_amount} 次"
            else:
                status_text = "生成失败"
                msg = self._format_error_message(
                    status_text,
                    elapsed,
                    res,
                    http_status,
                    model=actual_model,
                    api_mode=self._get_api_route_for_model(actual_model),
                    prompt=prompt,
                    image_count=0,
                )
                if show_model_info:
                    msg += f"\n模型: {actual_model}"
                if should_deduct and deduction_source in ["group", "user"]:
                    msg += f"\n(失败状态码命中扣次设置，已扣除 {deduction_amount} 次)"
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

        prompt_list = self.conf.get("prompt_list", [])
        if not isinstance(prompt_list, list):
            prompt_list = []

        found = False
        for idx, item in enumerate(prompt_list):
            if isinstance(item, dict):
                item_key = str(item.get("command") or item.get("指令") or "").strip()
                if item_key == key:
                    prompt_list[idx] = {"__template_key": "preset", "command": key, "prompt": new_value}
                    found = True
                    break
            elif isinstance(item, str) and item.strip().startswith(key + ":"):
                prompt_list[idx] = f"{key}:{new_value}"
                found = True
                break

        if not found:
            prompt_list.append({"__template_key": "preset", "command": key, "prompt": new_value})

        self.conf["prompt_list"] = prompt_list
        try:
            if hasattr(self.conf, "save"):
                self.conf.save()
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

        for key, val in self.prompt_map.items():
            if val == "[内置预设]":
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

    async def _load_user_counts(self):
        if not self.user_counts_file.exists():
            self.user_counts = {}
            return
        try:
            content = await asyncio.to_thread(self.user_counts_file.read_text, "utf-8")
            self.user_counts = json.loads(content)
        except:
            self.user_counts = {}

    async def _save_user_counts(self):
        try:
            data = json.dumps(self.user_counts, indent=4)
            await asyncio.to_thread(self.user_counts_file.write_text, data, "utf-8")
        except:
            pass

    def _get_user_count(self, uid: str) -> int:
        return self.user_counts.get(self._norm_id(uid), 0)

    async def _decrease_user_count(self, uid: str, amount: int = 1):
        uid = self._norm_id(uid)
        count = self._get_user_count(uid)
        if amount <= 0 or count <= 0:
            return
        deduction = min(amount, count)
        self.user_counts[uid] = count - deduction
        await self._save_user_counts()

    async def _load_group_counts(self):
        if not self.group_counts_file.exists():
            self.group_counts = {}
            return
        try:
            content = await asyncio.to_thread(self.group_counts_file.read_text, "utf-8")
            self.group_counts = json.loads(content)
        except:
            self.group_counts = {}

    async def _save_group_counts(self):
        try:
            data = json.dumps(self.group_counts, indent=4)
            await asyncio.to_thread(self.group_counts_file.write_text, data, "utf-8")
        except:
            pass

    def _get_group_count(self, group_id: str) -> int:
        return self.group_counts.get(self._norm_id(group_id), 0)

    async def _decrease_group_count(self, group_id: str, amount: int = 1):
        gid = self._norm_id(group_id)
        count = self._get_group_count(gid)
        if amount <= 0 or count <= 0:
            return
        deduction = min(amount, count)
        self.group_counts[gid] = count - deduction
        await self._save_group_counts()

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
        msg += "👥 **群组消耗排行**:\n"
        if groups_sorted:
            for i, (gid, count) in enumerate(groups_sorted):
                msg += f"{i + 1}. 群{gid}: {count}次\n"
        else:
            msg += "(无数据)\n"

        msg += "\n👤 **用户消耗排行**:\n"
        if users_sorted:
            for i, (uid, count) in enumerate(users_sorted):
                msg += f"{i + 1}. {uid}: {count}次\n"
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
            yield event.plain_result(f"已签到。剩余: {self._get_user_count(uid)}")
            return

        reward = int(self.conf.get("checkin_fixed_reward", 3))
        if self.conf.get("enable_random_checkin", False):
            max_r = int(self.conf.get("checkin_random_reward_max", 5))
            reward = random.randint(1, max(1, max_r))

        self.user_counts[uid] = self._get_user_count(uid) + reward
        await self._save_user_counts()
        self.user_checkin_data[uid] = today
        await self._save_user_checkin_data()

        yield event.plain_result(f"🎉 签到成功 +{reward}次。")

    @filter.command("手办化增加用户次数", prefix_optional=True)
    async def on_add_user_counts(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            return

        text = event.message_str.strip()
        at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
        target, count = None, 0

        if at_seg:
            target = self._norm_id(at_seg.qq)
            match = re.search(r"(\d+)\s*$", text)
            if match:
                count = int(match.group(1))
        else:
            match = re.search(r"(\d+)\s+(\d+)", text)
            if match:
                target, count = self._norm_id(match.group(1)), int(match.group(2))

        if target:
            old_cnt = self._get_user_count(target)
            new_cnt = old_cnt + count
            self.user_counts[target] = new_cnt
            await self._save_user_counts()

            msg = f"✅ 已为用户 {target} 增加 {count} 次。\n"
            msg += f"📊 变动: {old_cnt} + {count} = {new_cnt}\n"
            msg += f"👤 用户剩余: {new_cnt}"
            if gid := event.get_group_id():
                msg += f"\n👥 本群剩余: {self._get_group_count(self._norm_id(gid))}"

            yield event.plain_result(msg)

    @filter.command("手办化增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if maintenance_message := self._get_maintenance_message():
            yield self._reply_plain_result(event, maintenance_message)
            event.stop_event()
            return

        if not self.is_global_admin(event):
            return

        match = re.search(r"(\d+)\s+(\d+)", event.message_str.strip())
        if match:
            gid, count = self._norm_id(match.group(1)), int(match.group(2))

            old_cnt = self._get_group_count(gid)
            new_cnt = old_cnt + count
            self.group_counts[gid] = new_cnt
            await self._save_group_counts()

            msg = f"✅ 已为群 {gid} 增加 {count} 次。\n"
            msg += f"📊 变动: {old_cnt} + {count} = {new_cnt}\n"
            msg += f"👥 本群剩余: {new_cnt}"

            yield event.plain_result(msg)

    @filter.command("手办化查询次数", prefix_optional=True)
    async def on_query_counts(self, event: AstrMessageEvent):
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

        msg = f"👤 用户 {uid} 剩余: {self._get_user_count(uid)}"
        if gid := event.get_group_id():
            msg += f"\n👥 本群剩余: {self._get_group_count(self._norm_id(gid))}"

        yield event.plain_result(msg)

    async def terminate(self):
        if self.iwf:
            await self.iwf.terminate()
        logger.info("[FigurinePro] 插件已终止")
