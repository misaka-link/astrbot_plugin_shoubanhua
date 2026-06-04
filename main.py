import asyncio
import base64
import functools
import html
import io
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Reply, Plain, Node, Nodes
from astrbot.core.platform.astr_message_event import AstrMessageEvent


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
    "支持第三方OpenAI绘图格式和Gemini模型路由，文生图/图生图插件",
    "1.7.6",
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

    GENERIC_ENDPOINT_MODEL_LIST_KEYS = {
        "chat_completions": "chat_completions_model_list",
        "images_generations": "images_generations_model_list",
        "images_edits": "images_edits_model_list",
    }

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
        await self._migrate_command_model_list_config()
        await self._migrate_extra_prefix_config()
        await self._migrate_prompt_list_config()
        await self._load_prompt_map()
        await self._load_preset_images()

        # 创建预设图片目录
        if not self.preset_images_dir.exists():
            self.preset_images_dir.mkdir(parents=True, exist_ok=True)

        logger.info("FigurinePro 插件已加载")

        g_keys = self.conf.get("generic_api_keys", [])
        o_keys = self.conf.get("gemini_api_keys", [])

        if not g_keys and not o_keys:
            logger.warning("FigurinePro: 未配置任何 API Key")

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
        configured = str(self.conf.get("preset_list_command", "lm列表") or "").strip()
        commands = [configured, "lm列表", "lmlist", "预设列表"]
        return [cmd for cmd in self._dedupe_preserve_order(commands) if cmd]

    def _get_api_route_for_model(self, model_name: str) -> str:
        gemini_models = self._normalize_model_list(self.conf.get("gemini_model_list", []))
        return "gemini" if (model_name or "").strip() in gemini_models else "generic"

    def _get_generic_endpoint_type_for_model(self, model_name: str, has_images: bool) -> str:
        """根据端点模型列表决定 Generic 模式的请求端点。"""
        if not self._has_generic_endpoint_model_routes():
            return "chat_completions"

        normalized_model = (model_name or "").strip()
        if not normalized_model:
            return "chat_completions"

        if has_images:
            endpoint_order = ("images_edits", "images_generations", "chat_completions")
        else:
            endpoint_order = ("images_generations", "chat_completions")

        for endpoint_type in endpoint_order:
            if normalized_model in self._get_endpoint_models(endpoint_type):
                return endpoint_type

        return "chat_completions"

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in self.context.get_config().get("admins_id", [])

    def _norm_id(self, raw_id: Any) -> str:
        if raw_id is None:
            return ""
        return str(raw_id).strip()

    @filter.command("切换API模式", aliases={"SwitchApi"}, prefix_optional=True)
    async def on_switch_api_mode(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result("❌ 只有管理员可以执行此操作。")
            return

        msg = "ℹ️ API 模式切换已移除。\n"
        msg += "现在按模型自动路由：模型在后台 `gemini_model_list` 中就走 Gemini 端点，否则走 Generic 端点。\n"
        msg += "可用 `#切换模型 <序号>` 修改默认模型。"
        yield event.plain_result(msg)

    @filter.command("切换模型", aliases={"SwitchModel", "模型列表"}, prefix_optional=True)
    async def on_switch_model(self, event: AstrMessageEvent):
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

    async def _get_pool_api_key(self, mode: str) -> str | None:
        keys = []
        async with self.key_lock:
            if mode == "gemini":
                keys = self.conf.get("gemini_api_keys", [])
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
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model_name,
            "prompt": final_prompt,
            "n": 1,
        }

        if image_bytes_list:
            image_inputs = [self._image_bytes_to_data_url(img) for img in image_bytes_list]
            if len(image_inputs) == 1:
                payload["image"] = image_inputs[0]
            else:
                payload["images"] = image_inputs

        return payload

    def _build_generic_images_edits_form(
            self,
            model_name: str,
            final_prompt: str,
            image_bytes_list: List[bytes],
    ) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("model", model_name)
        form.add_field("prompt", final_prompt)
        form.add_field("n", "1")

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

    def _get_required_invocation_cost(self) -> int:
        return 1

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

    async def _call_api(self, image_bytes_list: List[bytes], prompt: str,
                        override_model: str | None = None) -> Tuple[bytes | str | Dict[str, Any], int]:
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

        if api_mode == "gemini":
            base_url = self.conf.get("gemini_api_url", "https://generativelanguage.googleapis.com")
        else:
            base_url = self.conf.get("generic_api_url", "https://api.bltcy.ai/v1/chat/completions")

        if not base_url:
            base_url = ""
            return make_error("config_error", "API URL 未配置", 0)

        api_key = await self._get_pool_api_key(api_mode)
        if not api_key:
            return make_error("config_error", f"无可用 API Key (请在 {api_mode} 池中添加Key)", 0)

        # --- 构造最终 Prompt (注入指令以强制画图) ---
        if len(image_bytes_list) > 0:
            # 图生图 - 使用默认模板
            final_prompt = f"Re-imagine the attached image with the following style/description: {prompt}. Draw it directly. Do not analyze."
        else:
            # 文生图 - 使用默认模板
            final_prompt = f"Generate a high quality image based on this description: {prompt}"

        headers = {
            "Connection": "keep-alive"
        }

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
                "generationConfig": {"maxOutputTokens": 2048},
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
                form_data = self._build_generic_images_edits_form(model_name, final_prompt, image_bytes_list)
            elif generic_endpoint_type == "images_generations":
                headers["Content-Type"] = "application/json"
                payload = self._build_generic_images_payload(model_name, final_prompt, image_bytes_list)
            else:
                headers["Content-Type"] = "application/json"
                messages = []
                # 优化 System Prompt，极度严格地禁止聊天，强制画图模式
                system_instruction = (
                    "You are an expert AI artist tool. Your ONLY job is to generate images based on user inputs. "
                    "Do NOT describe the image. Do NOT ask questions. Do NOT start a conversation. "
                    "Directly output the generated image url or data."
                )
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
                    "max_tokens": 4000,
                    "stream": use_stream,
                    "messages": messages
                }

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
                        return make_error(
                            "upstream_error",
                            provider_message,
                            http_status,
                            detail=data["error"],
                            request_id=request_id,
                            **provider_fields,
                        )

                    if isinstance(data, dict) and "promptFeedback" in data:
                        pf = data["promptFeedback"]
                        if pf.get("blockReason"):
                            return make_error(
                                "safety_block",
                                f"Gemini 安全拦截: {pf['blockReason']}",
                                http_status,
                                detail=pf,
                                provider_message=str(pf.get("blockReason") or ""),
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

        def _normalize_token(token: str) -> Tuple[str, Optional[int]]:
            token = token.strip()
            match = re.search(r"[\(（](\d+)[\)）]$", token)
            if match:
                idx = int(match.group(1))
                return token[:match.start()].strip(), idx
            return token, None

        def _extract_batch_count(token: str) -> Tuple[str, int, Optional[int]]:
            token = token.strip()
            default_batch = _normalize_positive_int(self.conf.get("default_batch_count", 1), 1)
            max_batch = _normalize_positive_int(self.conf.get("max_batch_multiplier", 4), 4)
            default_batch = min(default_batch, max_batch)
            symbol = str(self.conf.get("batch_multiplier_symbol", "*") or "").strip()
            if not symbol:
                return token, default_batch, None

            match = re.search(rf"{re.escape(symbol)}(\d+)$", token)
            if not match:
                return token, default_batch, None

            requested_count = max(1, int(match.group(1)))
            batch_count = min(requested_count, max_batch)
            return token[:match.start()].strip(), batch_count, requested_count

        raw_cmd_token = tokens[0].strip()
        command_token, batch_count, requested_batch_count = _extract_batch_count(raw_cmd_token)
        command_token, temp_model_idx = _normalize_token(command_token)
        if requested_batch_count is None:
            command_token, temp_model_idx = _normalize_token(raw_cmd_token)
            command_token, batch_count, requested_batch_count = _extract_batch_count(command_token)
        consumed_tokens = 1

        cmd = command_token
        if not cmd:
            return

        if cmd in self._get_preset_list_commands():
            yield await self._build_preset_list_result(event)
            event.stop_event()
            return

        # 指令解析
        extra_prefixes = self._get_extra_prefixes()
        matched_extra_prefix = extra_prefixes[0]
        user_prompt = ""
        is_bnn = False

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

        if base_cmd in extra_prefixes:
            matched_extra_prefix = base_cmd
            remaining_tokens = tokens[consumed_tokens:]
            user_prompt = " ".join(remaining_tokens).strip()
            is_bnn = True

        elif base_cmd in self.prompt_map:
            val = self.prompt_map.get(base_cmd)
            if val and val != "[内置预设]":
                user_prompt = val
                if append_text:
                    user_prompt = user_prompt + append_text
                    logger.info(f"将追加内容'{append_text}'添加到预设prompt后面")

        if not user_prompt and not is_bnn:
            cmd_map = {**self.BUILT_IN_CMD_MAP, "手办化帮助": "help"}
            if base_cmd in cmd_map:
                key = cmd_map[base_cmd]
                if key == "help":
                    yield self._get_help_result(event)
                    return
                user_prompt = self.prompt_map.get(key) or self.prompt_map.get(base_cmd)
                if append_text:
                    user_prompt = user_prompt + append_text
                    logger.info(f"将追加内容'{append_text}'添加到映射命令prompt后面")

        if not user_prompt:
            if not is_bnn:
                return

        # --- 权限与次数逻辑 ---
        sender_id = self._norm_id(event.get_sender_id())
        group_id = self._norm_id(event.get_group_id()) if event.get_group_id() else None

        user_blacklist = [self._norm_id(x) for x in (self.conf.get("user_blacklist") or [])]
        if sender_id in user_blacklist: return

        if group_id:
            group_blacklist = [self._norm_id(x) for x in (self.conf.get("group_blacklist") or [])]
            if group_id in group_blacklist: return

        raw_g_whitelist = self.conf.get("group_whitelist") or []
        group_whitelist = [self._norm_id(x) for x in raw_g_whitelist]

        raw_u_whitelist = self.conf.get("user_whitelist") or []
        user_whitelist = [self._norm_id(x) for x in raw_u_whitelist]

        is_master = self.is_global_admin(event)
        deduction_source = None
        per_invocation_cost = self._get_required_invocation_cost()
        required_cost = per_invocation_cost * batch_count

        if is_master:
            deduction_source = 'free'
        elif group_id and group_id in group_whitelist:
            deduction_source = 'free'
        elif group_id and len(group_whitelist) > 0:
            yield self._reply_plain_result(event, "❌ 本群未授权使用此功能。")
            return
        elif len(user_whitelist) > 0 and sender_id not in user_whitelist:
            return

        if deduction_source is None:
            if group_id and self.conf.get("enable_group_limit", False):
                g_cnt = self._get_group_count(group_id)
                if g_cnt >= required_cost:
                    deduction_source = 'group'

            if deduction_source is None and self.conf.get("enable_user_limit", True):
                u_cnt = self._get_user_count(sender_id)
                if u_cnt >= required_cost:
                    deduction_source = 'user'

            if deduction_source is None:
                if not self.conf.get("enable_group_limit", False) and not self.conf.get("enable_user_limit", True):
                    deduction_source = 'free'
                else:
                    yield self._reply_plain_result(
                        event,
                        self._build_limit_exhausted_message(group_id)
                    )
                    return

        if requested_batch_count and requested_batch_count > batch_count:
            yield self._reply_plain_result(event, f"⚠️ 本次倍率最高为 {batch_count}，已按 {batch_count} 次并发生成。")

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

            display_cmd = user_prompt[:10] + '...' if len(user_prompt) > 10 else user_prompt
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
                .replace("{image_count}", str(len(images_to_process)))
                .replace("{batch_count}", str(batch_count))
                .replace("{max_batch_concurrency}", str(max_batch_concurrency)))
        else:
            # 使用默认格式
            info_msg = f"🎨 收到{mode_prefix}{action_type}请求{batch_suffix}，正在生成 [{display_label}]..."
        
        yield self._reply_plain_result(event, info_msg)

        if deduction_source == 'group' and group_id:
            await self._decrease_group_count(group_id, required_cost)
        elif deduction_source == 'user':
            await self._decrease_user_count(sender_id, required_cost)

        batch_semaphore = asyncio.Semaphore(max_batch_concurrency)

        async def _call_api_with_batch_limit(batch_index: int):
            async with batch_semaphore:
                task_start_time = datetime.now()
                try:
                    result = await self._call_api(
                        images_to_process,
                        user_prompt,
                        override_model=override_model_name,
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
        first_error = None
        first_error_status = 0

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

            res, http_status = result
            if not isinstance(res, bytes):
                if first_error is None:
                    first_error = res
                    first_error_status = http_status
                continue

            success_count += 1
            await self._record_daily_usage(sender_id, group_id)

            if success_count == 1 and base_cmd in self.prompt_map and not is_bnn:
                await self._save_preset_image(base_cmd, res)

            # 检查是否有自定义成功消息模板
            custom_success_template = self.conf.get("custom_success_message", "").strip()
            if custom_success_template:
                # 计算剩余次数
                if deduction_source == 'free':
                    remaining_text = "∞"
                else:
                    remaining_parts = []
                    if self.conf.get("enable_user_limit", True):
                        remaining_parts.append(str(self._get_user_count(sender_id)))
                    if group_id and self.conf.get("enable_group_limit", False):
                        remaining_parts.append(f"群{self._get_group_count(group_id)}")
                    remaining_text = "/".join(remaining_parts) if remaining_parts else "∞"
                
                # 替换占位符
                message_text = custom_success_template.replace("{model}", model_in_use).replace("{label}", display_label).replace("{image_count}", str(len(images_to_process))).replace("{elapsed}", f"{elapsed:.2f}").replace("{remaining}", remaining_text).replace("{prompt}", user_prompt[:50]).replace("{batch_count}", str(batch_count)).replace("{batch_index}", str(index)).replace("{max_batch_concurrency}", str(max_batch_concurrency))
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
                    caption_parts.append(f"模型: {model_in_use}")

                message_text = " | ".join(caption_parts)

            yield self._reply_chain_result(event, [Image.fromBytes(res), Plain(message_text)])

        total_elapsed = (datetime.now() - start_time).total_seconds()
        if success_count == 0:
            status_text = "生成失败"
            msg = self._format_error_message(
                status_text,
                total_elapsed,
                first_error,
                first_error_status,
                model=model_in_use,
                api_mode=self._get_api_route_for_model(model_in_use),
                prompt=user_prompt,
                image_count=len(images_to_process),
            )
            if deduction_source in ['group', 'user']:
                msg += "\n(注: 触发即扣次)"
            if show_model_info:
                msg += f"\n模型: {model_in_use}"
            yield self._reply_plain_result(event, msg)
        elif success_count < batch_count:
            yield self._reply_plain_result(event, f"⚠️ 批量生成完成：成功 {success_count}/{batch_count}，失败 {batch_count - success_count} 次。")

        event.stop_event()

    def _get_help_result(self, event: AstrMessageEvent):
        """生成合并转发帮助消息对象"""
        help_text = self.conf.get("help_text", "帮助文档未配置")

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

        required_cost = self._get_required_invocation_cost()

        deduction_source = None
        if self.is_global_admin(event):
            deduction_source = 'free'
        else:
            if group_id and self.conf.get("enable_group_limit", False):
                if self._get_group_count(group_id) >= required_cost:
                    deduction_source = 'group'

            if deduction_source is None and self.conf.get("enable_user_limit", True):
                if self._get_user_count(sender_id) >= required_cost:
                    deduction_source = 'user'

            if deduction_source is None:
                if not self.conf.get("enable_group_limit", False) and not self.conf.get("enable_user_limit", True):
                    deduction_source = 'free'
                else:
                    yield self._reply_plain_result(
                        event,
                        self._build_limit_exhausted_message(group_id)
                    )
                    return

        display_prompt = prompt[:10] + "..." if len(prompt) > 10 else prompt
        mode_prefix = ""
        
        base_model_name = (self.conf.get("model", "nano-banana") or "nano-banana").strip() or "nano-banana"
        model_in_use = (override_model_name or base_model_name).strip() or base_model_name
        show_model_info = self.conf.get("show_model_info", False)
        
        # 检查是否有自定义文生图开始消息模板
        custom_start_template = self.conf.get("custom_text2img_start_message", "").strip()
        if custom_start_template:
            # 使用自定义模板 - 添加所有可用参数
            info_str = (custom_start_template
                .replace("{mode_prefix}", mode_prefix)
                .replace("{prompt}", display_prompt)
                .replace("{model}", model_in_use))
        else:
            # 使用默认格式
            info_str = f"🎨 收到{mode_prefix}文生图请求，正在生成 [{display_prompt}]"
        
        yield self._reply_plain_result(event, info_str)

        if deduction_source == 'group' and group_id:
            await self._decrease_group_count(group_id, required_cost)
        elif deduction_source == 'user':
            await self._decrease_user_count(sender_id, required_cost)

        start_time = datetime.now()
        res, http_status = await self._call_api([], prompt, override_model=override_model_name)
        elapsed = (datetime.now() - start_time).total_seconds()

        if isinstance(res, bytes):
            await self._record_daily_usage(sender_id, group_id)

            # 检查是否有自定义成功消息模板
            custom_success_template = self.conf.get("custom_success_message", "").strip()
            if custom_success_template:
                # 计算剩余次数
                if deduction_source == 'free':
                    remaining_text = "∞"
                else:
                    remaining_parts = []
                    if self.conf.get("enable_user_limit", True):
                        remaining_parts.append(str(self._get_user_count(sender_id)))
                    if group_id and self.conf.get("enable_group_limit", False):
                        remaining_parts.append(f"群{self._get_group_count(group_id)}")
                    remaining_text = "/".join(remaining_parts) if remaining_parts else "∞"
                
                # 替换占位符（文生图没有携带图片，image_count为0）
                message_text = custom_success_template.replace("{model}", model_in_use).replace("{label}", display_prompt).replace("{image_count}", "0").replace("{elapsed}", f"{elapsed:.2f}").replace("{remaining}", remaining_text).replace("{prompt}", prompt[:50])
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
                    caption_parts.append(f"模型: {model_in_use}")

                message_text = " | ".join(caption_parts)

            yield self._reply_chain_result(event, [Image.fromBytes(res), Plain(message_text)])
        else:
            status_text = "生成失败"
            msg = self._format_error_message(
                status_text,
                elapsed,
                res,
                http_status,
                model=model_in_use,
                api_mode=self._get_api_route_for_model(model_in_use),
                prompt=prompt,
                image_count=0,
            )
            if show_model_info:
                msg += f"\n模型: {model_in_use}"
            yield self._reply_plain_result(event, msg)

        event.stop_event()

    @filter.command("lm添加", aliases={"lma"}, prefix_optional=True)
    async def add_lm_prompt(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return

        full_msg = event.message_str or ""
        clean_msg = full_msg.strip()

        cmd_prefix = "lm添加"
        if "lma" in clean_msg.lower() and not clean_msg.startswith(cmd_prefix):
            cmd_prefix = "lma"

        if clean_msg.lower().startswith(cmd_prefix.lower()):
            clean_msg = clean_msg[len(cmd_prefix):].strip()

        clean_msg = clean_msg.lstrip("#/ ")

        if ":" not in clean_msg:
            yield event.plain_result('格式错误, 示例: #lm添加 触发词:提示词')
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

    @filter.command("lm查看", aliases={"lmv", "lm预览"}, prefix_optional=True)
    async def lm_preview_prompt(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        parts = raw.split()
        if len(parts) < 2:
            yield event.plain_result("用法: #lm查看 <关键词>")
            return

        keyword = parts[1].strip()
        prompt_content = self.prompt_map.get(keyword)

        if prompt_content:
            yield event.plain_result(f"🔍 关键词【{keyword}】的提示词：\n\n{prompt_content}")
        else:
            yield event.plain_result(f"❌ 未找到关键词【{keyword}】的预设。")

    @filter.command("lm列表", aliases={"lmlist", "预设列表"}, prefix_optional=True)
    async def on_get_preset_list(self, event: AstrMessageEvent):
        """输出所有可用预设列表预览图。"""
        yield await self._build_preset_list_result(event)

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

    @filter.command("lm帮助", aliases={"lmh", "手办化帮助"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        keyword = parts[1] if len(parts) > 1 else ""

        if not keyword:
            yield self._get_help_result(event)
            return

        prompt = self.prompt_map.get(keyword)
        content = f"📄 预设 [{keyword}] 内容:\n{prompt}" if prompt else f"❌ 未找到 [{keyword}]"

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
            content=[Plain(content)]
        )
        yield event.chain_result([Nodes(nodes=[node])])

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

    @filter.command("手办化添加key", prefix_optional=True)
    async def on_add_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return

        new_keys = event.message_str.strip().split()[1:]
        if not new_keys:
            yield event.plain_result("格式错误。用法: #手办化添加key [generic|gemini] <key1> ...")
            return

        target_mode = "generic"
        if new_keys[0].lower() in {"gemini", "g"}:
            target_mode = "gemini"
            new_keys = new_keys[1:]
        elif new_keys[0].lower() in {"generic", "openai", "o"}:
            new_keys = new_keys[1:]
        elif new_keys[0].lower() in ["power", "强力", "p"]:
            yield event.plain_result("⚠️ 强力模式已移除。请使用 generic 或 gemini Key 池。")
            return

        if not new_keys:
            yield event.plain_result("格式错误。用法: #手办化添加key [generic|gemini] <key1> ...")
            return

        target_field = "gemini_api_keys" if target_mode == "gemini" else "generic_api_keys"
        mode_desc = f"【{target_mode}】"

        keys = self.conf.get(target_field, [])
        added = [k for k in new_keys if k not in keys]
        keys.extend(added)
        self.conf[target_field] = keys

        if hasattr(self.conf, "save"):
            self.conf.save()

        yield event.plain_result(f"✅ 已向 {mode_desc} 模式添加 {len(added)} 个Key。")

    @filter.command("手办化key列表", prefix_optional=True)
    async def on_list_keys(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return

        generic_keys = self.conf.get("generic_api_keys", [])
        gemini_keys = self.conf.get("gemini_api_keys", [])

        msg = "🔑 Key池列表\n\n"
        msg += f"📌 Generic ({len(generic_keys)}个):\n"
        msg += ("\n".join([f"{i + 1}. {k[:6]}..." for i, k in enumerate(generic_keys)]) if generic_keys else "(空)")
        msg += f"\n\n📌 Gemini ({len(gemini_keys)}个):\n"
        msg += ("\n".join([f"{i + 1}. {k[:6]}..." for i, k in enumerate(gemini_keys)]) if gemini_keys else "(空)")

        yield event.plain_result(msg)

    @filter.command("手办化删除key", prefix_optional=True)
    async def on_delete_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return

        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("格式: #手办化删除key <序号|all>")
            return

        target_mode = "generic"
        param_index = 1
        if parts[1].lower() in {"gemini", "g"}:
            target_mode = "gemini"
            param_index = 2
        elif parts[1].lower() in {"generic", "openai", "o"}:
            param_index = 2

        if len(parts) <= param_index:
            yield event.plain_result("格式: #手办化删除key [generic|gemini] <序号|all>")
            return

        param = parts[param_index]
        if param.lower() in ["power", "强力", "p"]:
            yield event.plain_result("⚠️ 强力模式已移除。请使用 generic 或 gemini Key 池。")
            return

        target_field = "gemini_api_keys" if target_mode == "gemini" else "generic_api_keys"
        mode_desc = f"【{target_mode}】"

        keys = self.conf.get(target_field, [])

        if param == "all":
            self.conf[target_field] = []
        elif param.isdigit():
            idx = int(param) - 1
            if 0 <= idx < len(keys):
                keys.pop(idx)
                self.conf[target_field] = keys

        if hasattr(self.conf, "save"):
            self.conf.save()

        yield event.plain_result(f"✅ 已从 {mode_desc} 模式删除Key。")

    async def terminate(self):
        if self.iwf:
            await self.iwf.terminate()
        logger.info("[FigurinePro] 插件已终止")
