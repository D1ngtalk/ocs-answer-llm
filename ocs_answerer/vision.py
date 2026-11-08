# -*- coding: utf-8 -*-
"""图片识别辅助：从题目里找出图片、下载、转成 data URL 交给视觉模型。"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ocs.vision")

_IMG_URL_RE = re.compile(
    r"(?:https?:)?//[^\s\"'<>()\[\]{}]+?\.(?:png|jpe?g|webp|bmp)(?:\?[^\s\"'<>()\[\]{}]*)?",
    re.I,
)
_IMG_TAG_RE = re.compile(r"<img[^>]+src\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))", re.I)
_DATA_URL_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+")

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class ImagePart:
    source: str
    data_url: str
    sha1: str
    mime: str
    # 与题干里 [图片N] 的编号一一对应（1 开始）；老版本 OCS 的 DOM 抓图按出现顺序编号
    index: int = 0

    def as_message_part(self) -> dict:
        return {"type": "image_url", "image_url": {"url": self.data_url}}


def extract_image_sources(*texts: str) -> list[str]:
    """从题目/选项文本里提取图片地址（含 <img src>、裸链接、data URL）。"""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _IMG_TAG_RE.finditer(text):
            value = next((g for g in match.groups() if g), "")
            if value:
                found.append(value.strip())
        for match in _IMG_URL_RE.finditer(text):
            url = match.group(0)
            if url.startswith("//"):
                url = "https:" + url
            found.append(url)
        for match in _DATA_URL_RE.finditer(text):
            found.append(re.sub(r"\s+", "", match.group(0)))
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def split_sources(value: str | list[str] | None) -> list[str]:
    """把 image 参数（可能是逗号/换行分隔的字符串，或 data URL 数组）拆成列表。"""
    if not value:
        return []
    if isinstance(value, list):
        items = [str(v) for v in value]
    elif str(value).strip().startswith("data:image/"):
        # data URL 自身含逗号，不能拆分
        items = [str(value).strip()]
    else:
        items = re.split(r"[,\n\r;]+", str(value))
    bogus = {"", "undefined", "null", "none", "[object object]"}
    return [item.strip() for item in items if item and item.strip().lower() not in bogus]


def _load_local(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    mime = _MIME_BY_EXT.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "image/png"
    return data, mime


def _download(url: str, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "image/*,*/*;q=0.8"})
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme and parsed.netloc:
        request.add_header("Referer", f"{parsed.scheme}://{parsed.netloc}/")
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if (parsed.hostname or "") in ("127.0.0.1", "localhost", "::1")
        else urllib.request.build_opener()
    )
    with opener.open(request, timeout=timeout) as response:
        data = response.read()
        mime = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        mime = _MIME_BY_EXT.get(Path(parsed.path).suffix.lower(), "image/png")
    return data, mime


def _decode_data_url(url: str, max_bytes: int) -> tuple[bytes, str] | None:
    header, _, payload = url.partition(",")
    if not payload:
        return None
    mime = header[5:].split(";")[0] or "image/png"
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=False)
    except (ValueError, TypeError):
        return None
    if len(data) > max_bytes:
        return None
    return data, mime


def collect_images(
    sources: list[str],
    *,
    max_images: int = 3,
    max_bytes: int = 8 * 1024 * 1024,
    timeout: float = 20,
) -> list[ImagePart]:
    """把图片地址转成可以直接塞进 messages 的 ImagePart 列表。"""
    parts: list[ImagePart] = []
    for position, source in enumerate(sources, start=1):
        if len(parts) >= max_images:
            break
        try:
            if source.startswith("data:image/"):
                decoded = _decode_data_url(source, max_bytes)
                if not decoded:
                    log.warning("跳过无法解析的 data URL 图片")
                    continue
                data, mime = decoded
            elif source.lower().startswith(("http://", "https://", "//")):
                if source.startswith("//"):
                    source = "https:" + source
                data, mime = _download(source, timeout)
            else:
                # 其余按本地文件处理
                path = Path(source)
                if not path.is_absolute():
                    path = Path.cwd() / path
                if not path.exists():
                    log.warning("图片文件不存在：%s", path)
                    continue
                data, mime = _load_local(path)
        except Exception as exc:  # 单张图失败不影响整题
            log.warning("图片获取失败（%s）：%s", str(exc)[:120], source[:120])
            continue

        if not data:
            continue
        if len(data) > max_bytes:
            log.warning("图片过大已跳过（%.1fMB）：%s", len(data) / 1048576, source[:120])
            continue
        if mime == "image/gif":
            log.warning("视觉模型通常不支持 GIF，已跳过：%s", source[:120])
            continue

        b64 = base64.b64encode(data).decode("ascii")
        parts.append(
            ImagePart(
                source=source[:500],
                data_url=f"data:{mime};base64,{b64}",
                sha1=hashlib.sha1(data).hexdigest(),
                mime=mime,
                index=position,
            )
        )
    return parts


def images_fingerprint(parts: list[ImagePart]) -> str:
    if not parts:
        return ""
    digest = hashlib.sha1("|".join(p.sha1 for p in parts).encode("ascii")).hexdigest()
    return digest
