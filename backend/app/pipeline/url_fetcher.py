"""URL 网页内容抓取与正文提取

把一个网页链接（通用网页 / 微信公众号永久图文链接等）抓回并提取出标题与正文，
输出为 Markdown 文本，供上层当作一篇 ``.md`` 文档喂入既有入库管线（load→chunk→
embed→index），不引入新的处理分支。

设计原则（数据流向清晰、禁止过度封装）：
- 抓取（httpx）与正文提取（trafilatura）是两个无状态纯步骤，串成一个函数返回结果。
- 失败均抛 :class:`UrlFetchError`，由 API 层转成明确的用户提示（4xx），不静默吞错。
- 不做反爬对抗（不带 Cookie 池 / 代理 / headless）：仅覆盖「公开可直接 GET 到正文」
  的页面（通用文章、微信公众号 mp.weixin.qq.com/s/ 永久链接）。JS 动态渲染的强反爬
  平台（小红书 / 抖音等）不在本期范围。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# 抓取超时（秒）：连接 + 读取。网页抓取应快速失败，避免拖住请求。
_FETCH_TIMEOUT = 15.0

# 抓取响应体大小上限（字节）：防止超大页面打爆内存。10 MB 足够覆盖图文页面。
_MAX_CONTENT_BYTES = 10 * 1024 * 1024

# 提取正文的最小长度（字符）：低于此值视为抽取失败（多半是反爬墙 / 空壳页面）。
_MIN_CONTENT_CHARS = 50

# 模拟常见桌面浏览器 UA，提升公开页面（含公众号）的可抓取率。
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class UrlFetchError(Exception):
    """URL 抓取或正文提取失败（业务输入问题，由 API 层转 4xx 提示）。"""


@dataclass
class FetchedArticle:
    """抓取并提取后的文章结果。

    Attributes:
        title: 文章标题（提取失败时回退为域名）。
        markdown: 正文的 Markdown 文本（含标题与来源链接抬头）。
        url: 规范化后的原始链接。
        site: 来源站点域名（如 ``mp.weixin.qq.com``）。
        author: 作者 / 公众号名（可能为空）。
        published: 发布时间字符串（可能为空）。
        cover_image_url: 封面图（og:image）链接，可能为空。
    """

    title: str
    markdown: str
    url: str
    site: str
    author: str = ""
    published: str = ""
    cover_image_url: str = ""
    extra: dict = field(default_factory=dict)


def _normalize_url(url: str) -> str:
    """校验并规范化 URL：仅允许 http/https，去首尾空白。"""
    cleaned = (url or "").strip()
    if not cleaned:
        raise UrlFetchError("链接不能为空")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise UrlFetchError("仅支持 http / https 链接")
    if not parsed.netloc:
        raise UrlFetchError("链接格式不正确，缺少域名")
    return cleaned


async def _download_html(url: str) -> str:
    """GET 抓取页面 HTML，带超时、跳转跟随与体积上限保护。

    抛 :class:`UrlFetchError`：网络错误 / 非 2xx / 非 HTML / 超大页面。
    """
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers=_DEFAULT_HEADERS,
            max_redirects=5,
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException as e:
        raise UrlFetchError("抓取超时，目标网页响应过慢") from e
    except httpx.HTTPError as e:
        raise UrlFetchError(f"抓取失败，无法访问该链接：{e}") from e

    if resp.status_code >= 400:
        raise UrlFetchError(f"抓取失败，目标网页返回状态码 {resp.status_code}")

    content_type = resp.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type and "text" not in content_type:
        raise UrlFetchError(
            f"该链接不是网页内容（content-type={content_type or '未知'}），暂不支持转存"
        )

    raw = resp.content
    if len(raw) > _MAX_CONTENT_BYTES:
        raise UrlFetchError("网页内容过大，超过 10 MB 上限")

    # httpx 已按响应头/编码声明解码；encoding 缺失时回退 utf-8 容错。
    return resp.text or raw.decode("utf-8", errors="ignore")


def _extract_article(html: str, url: str) -> FetchedArticle:
    """用 trafilatura 从 HTML 提取标题与正文，组装为 Markdown。

    trafilatura 缺失（未安装）或抽取为空时抛 :class:`UrlFetchError`。
    """
    try:
        import trafilatura
        from trafilatura.settings import use_config
    except ImportError as e:  # pragma: no cover - 依赖未安装时的清晰提示
        raise UrlFetchError(
            "正文提取依赖未安装（trafilatura），请先安装后再使用链接转存功能"
        ) from e

    # 关闭信号量超时（trafilatura 默认用信号，子线程中不可用），并提取为 markdown。
    cfg = use_config()
    cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_images=False,
        include_tables=True,
        with_metadata=False,
        favor_precision=True,
        config=cfg,
    )

    if not extracted or len(extracted.strip()) < _MIN_CONTENT_CHARS:
        raise UrlFetchError(
            "未能从该链接提取到正文内容，可能是需要登录、动态渲染或有反爬限制的页面"
        )

    # 单独取元数据（标题/作者/时间/站点），失败不影响正文。
    title = ""
    author = ""
    published = ""
    sitename = ""
    cover_image_url = ""
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            title = (meta.title or "").strip()
            author = (meta.author or "").strip()
            published = (meta.date or "").strip()
            sitename = (meta.sitename or "").strip()
            cover_image_url = (getattr(meta, "image", "") or "").strip()
    except Exception as e:  # noqa: BLE001 - 元数据为锦上添花，失败仅降级
        logger.warning("URL 元数据提取失败，仅使用正文: %s", e)

    # 元数据无 image 时，从原始 HTML 兜底解析 og:image / twitter:image。
    if not cover_image_url:
        cover_image_url = _extract_og_image(html)

    site = urlparse(url).netloc
    if not title:
        title = sitename or site or "网页内容"

    markdown = _build_markdown(
        title=title, body=extracted.strip(), url=url,
        author=author, published=published, site=site,
    )
    return FetchedArticle(
        title=title, markdown=markdown, url=url, site=site,
        author=author, published=published, cover_image_url=cover_image_url,
    )


def _build_markdown(
    *, title: str, body: str, url: str, author: str, published: str, site: str
) -> str:
    """组装带来源抬头的 Markdown，便于答案溯源回原始链接。"""
    lines = [f"# {title}", ""]
    meta_bits: list[str] = []
    if author:
        meta_bits.append(f"作者：{author}")
    if published:
        meta_bits.append(f"发布时间：{published}")
    if site:
        meta_bits.append(f"来源：{site}")
    if meta_bits:
        lines.append("> " + " | ".join(meta_bits))
    lines.append(f"> 原文链接：{url}")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


async def fetch_article(url: str) -> FetchedArticle:
    """抓取链接并提取正文为 Markdown（对外唯一入口）。

    流程：规范化 URL → GET 抓 HTML → trafilatura 提取标题/正文 → 组装 Markdown。
    任一步失败抛 :class:`UrlFetchError`（含明确中文原因）。

    Args:
        url: 用户粘贴的网页链接（通用文章 / 微信公众号永久图文链接）。

    Returns:
        :class:`FetchedArticle`，``markdown`` 字段可直接作为 ``.md`` 文档入库。
    """
    normalized = _normalize_url(url)
    html = await _download_html(normalized)
    return _extract_article(html, normalized)


# og:image / twitter:image 的 meta 标签匹配（容忍属性顺序与单双引号）。
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*>',
    re.IGNORECASE,
)
_CONTENT_ATTR_RE = re.compile(r'content=["\']([^"\']+)["\']', re.IGNORECASE)

# 封面图下载体积上限（字节）：超大图不下载，避免拖慢转存。
_MAX_COVER_BYTES = 5 * 1024 * 1024


def _extract_og_image(html: str) -> str:
    """从 HTML 兜底解析封面图链接（og:image / twitter:image）。无则返回空串。"""
    if not html:
        return ""
    m = _OG_IMAGE_RE.search(html)
    if not m:
        return ""
    c = _CONTENT_ATTR_RE.search(m.group(0))
    return c.group(1).strip() if c else ""


async def download_image(image_url: str, *, referer: str | None = None) -> bytes | None:
    """下载封面图字节（带 referer 绕过防盗链、体积上限、类型校验）。

    失败返回 None（封面图为锦上添花，绝不阻断转存主流程）。``referer`` 传原文链接，
    用于绕过微信公众号等的图片防盗链。
    """
    url = (image_url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    headers = dict(_DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT, follow_redirects=True, headers=headers, max_redirects=5
        ) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            return None
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "image" not in ctype:
            return None
        data = resp.content
        if not data or len(data) > _MAX_COVER_BYTES:
            return None
        return data
    except Exception as e:  # noqa: BLE001 - 封面图下载失败静默降级
        logger.warning("封面图下载失败 %s: %s", url, e)
        return None


def safe_filename_from_title(title: str, *, max_len: int = 120) -> str:
    """把文章标题转为合法 ``.md`` 文件名（去非法字符、限长、兜底）。

    与 :func:`app.api.validators.validate_filename` 的禁止字符对齐：去除
    ``/ \\ < > : " | ? *`` 与控制字符，折叠空白，去首尾点/空格。
    """
    name = re.sub(r'[/\\<>:"|?*\x00-\x1f]', " ", title or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = "网页内容"
    if len(name) > max_len:
        name = name[:max_len].rstrip().strip(".")
    return f"{name}.md"
