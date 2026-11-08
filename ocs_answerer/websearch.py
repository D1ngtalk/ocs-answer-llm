# -*- coding: utf-8 -*-
"""联网搜索：为时事类题目补充最新资料。

内置渠道：
* ``bing``       —— 抓取 cn.bing.com 搜索结果，免 key；
* ``duckduckgo`` —— 抓取 html.duckduckgo.com，免 key；
* ``tavily`` / ``serper`` / ``bocha`` —— 需要 API key，质量更稳定。

配置里按顺序尝试，第一个有结果的渠道生效。
"""

from __future__ import annotations

import gzip
import html as html_lib
import io
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("ocs.search")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<[^>]{1,300}>")
_WS_RE = re.compile(r"[\s\u3000\xa0]+")


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet, "source": self.source}


def clean(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> str:
    request = urllib.request.Request(url, data=data)
    request.add_header("User-Agent", _UA)
    request.add_header("Accept", "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8")
    request.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    request.add_header("Accept-Encoding", "gzip, deflate")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "replace")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    merged = {"Content-Type": "application/json"}
    merged.update(headers)
    text = _request(url, data=json.dumps(payload).encode("utf-8"), headers=merged, timeout=timeout)
    return json.loads(text)


# ---------------------------------------------------------------------------
# 各渠道实现
# ---------------------------------------------------------------------------


def search_tavily(query: str, limit: int, timeout: float, api_key: str) -> list[WebResult]:
    data = _post_json(
        "https://api.tavily.com/search",
        {"api_key": api_key, "query": query, "max_results": limit, "search_depth": "basic"},
        {},
        timeout,
    )
    return [
        WebResult(clean(item.get("title", "")), item.get("url", ""), clean(item.get("content", "")), "tavily")
        for item in data.get("results", [])
    ]


def search_serper(query: str, limit: int, timeout: float, api_key: str) -> list[WebResult]:
    data = _post_json(
        "https://google.serper.dev/search",
        {"q": query, "num": limit, "gl": "cn", "hl": "zh-cn"},
        {"X-API-KEY": api_key},
        timeout,
    )
    return [
        WebResult(clean(item.get("title", "")), item.get("link", ""), clean(item.get("snippet", "")), "serper")
        for item in data.get("organic", [])
    ]


def search_bocha(query: str, limit: int, timeout: float, api_key: str) -> list[WebResult]:
    data = _post_json(
        "https://api.bochaai.com/v1/web-search",
        {"query": query, "count": limit, "summary": True, "freshness": "noLimit"},
        {"Authorization": f"Bearer {api_key}"},
        timeout,
    )
    pages = (((data.get("data") or {}).get("webPages") or {}).get("value")) or []
    results = []
    for item in pages:
        results.append(
            WebResult(
                clean(item.get("name", "")),
                item.get("url", ""),
                clean(item.get("summary") or item.get("snippet") or ""),
                "bocha",
            )
        )
    return results


def search_bing(query: str, limit: int, timeout: float, api_key: str = "") -> list[WebResult]:
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query, "ensearch": "0"})
    page = _request(url, timeout=timeout)
    results: list[WebResult] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        snippet = re.search(r'<p[^>]*>(.*?)</p>', block, re.S) or re.search(
            r'class="b_caption[^"]*"[^>]*>(.*?)</div>', block, re.S
        )
        results.append(
            WebResult(
                clean(link.group(2)),
                html_lib.unescape(link.group(1)),
                clean(snippet.group(1)) if snippet else "",
                "bing",
            )
        )
        if len(results) >= limit:
            break
    return results


def search_duckduckgo(query: str, limit: int, timeout: float, api_key: str = "") -> list[WebResult]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    page = _request(url, timeout=timeout)
    results: list[WebResult] = []
    blocks = re.findall(r'<div class="result results_links.*?</div>\s*</div>', page, re.S)
    if not blocks:
        blocks = re.findall(r'<div class="result[^"]*".*?(?=<div class="result|</body>)', page, re.S)
    for block in blocks:
        link = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        href = html_lib.unescape(link.group(1))
        # DDG 会包装成 /l/?uddg=xxx
        if "uddg=" in href:
            query_string = urllib.parse.urlparse(href).query
            href = urllib.parse.parse_qs(query_string).get("uddg", [href])[0]
        results.append(
            WebResult(
                clean(link.group(2)),
                href,
                clean(snippet.group(1)) if snippet else "",
                "duckduckgo",
            )
        )
        if len(results) >= limit:
            break
    return results


_PROVIDERS = {
    "tavily": search_tavily,
    "serper": search_serper,
    "bocha": search_bocha,
    "bing": search_bing,
    "duckduckgo": search_duckduckgo,
}


class WebSearch:
    """按配置依次尝试搜索渠道。"""

    def __init__(self, cfg) -> None:
        self.enabled = bool(cfg.get("search.enabled", True))
        self.mode = str(cfg.get("search.mode", "auto") or "auto")
        self.providers = [str(p).lower() for p in (cfg.get("search.providers") or [])]
        self.max_results = int(cfg.get("search.max_results", 6) or 6)
        self.timeout = float(cfg.get("search.timeout", 10) or 10)
        self.keys = {
            "tavily": str(cfg.get("search.tavily_api_key", "") or ""),
            "serper": str(cfg.get("search.serper_api_key", "") or ""),
            "bocha": str(cfg.get("search.bocha_api_key", "") or ""),
        }

    def available(self) -> bool:
        return self.enabled and bool(self.providers)

    def search(self, query: str) -> list[WebResult]:
        query = (query or "").strip()
        if not query or not self.available():
            return []
        for name in self.providers:
            handler = _PROVIDERS.get(name)
            if not handler:
                log.warning("未知的搜索渠道：%s", name)
                continue
            key = self.keys.get(name, "")
            if name in ("tavily", "serper", "bocha") and not key:
                log.debug("搜索渠道 %s 缺少 api key，跳过", name)
                continue
            try:
                results = handler(query, self.max_results, self.timeout, key)
            except urllib.error.HTTPError as exc:
                log.warning("搜索渠道 %s 返回 HTTP %s", name, exc.code)
                continue
            except Exception as exc:
                log.warning("搜索渠道 %s 失败：%s", name, str(exc)[:150])
                continue
            results = [r for r in results if r.title or r.snippet]
            if results:
                log.info("联网搜索命中 %s 条（%s）：%s", len(results), name, query)
                return results[: self.max_results]
        log.warning("所有搜索渠道都没有返回结果：%s", query)
        return []

    @staticmethod
    def format_context(results: list[WebResult], max_chars: int = 3500) -> str:
        if not results:
            return ""
        chunks: list[str] = []
        total = 0
        for index, item in enumerate(results, start=1):
            block = f"[{index}] {item.title}\n{item.snippet}\n来源：{item.url}"
            if total + len(block) > max_chars:
                break
            chunks.append(block)
            total += len(block)
        return "\n\n".join(chunks)
