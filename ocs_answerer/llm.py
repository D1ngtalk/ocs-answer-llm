# -*- coding: utf-8 -*-
"""极简 OpenAI 兼容协议客户端（只用标准库 urllib）。

支持 DeepSeek / 通义千问 / 智谱 / 月之暗面 / OpenAI / SiliconFlow / Ollama
等一切兼容 ``/chat/completions`` 的服务。
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import Provider

log = logging.getLogger("ocs.llm")

_USER_AGENT = "ocs-answer-llm/1.0 (+https://docs.ocsjs.com)"


class LLMError(RuntimeError):
    """调用大模型失败。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def build_endpoint(base_url: str) -> str:
    """把用户填的 base_url 规整成 /chat/completions 地址。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("base_url 为空")
    if url.endswith("/chat/completions"):
        return url
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    if path in ("", "/"):
        return url + "/v1/chat/completions"
    return url + "/chat/completions"


def _is_local(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", _USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)

    # 本地服务（Ollama 等）不走系统代理，避免被代理劫持
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if _is_local(url)
        else urllib.request.build_opener()
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:800]
        except Exception:  # pragma: no cover - 读错误体失败无所谓
            pass
        retryable = exc.code in (408, 409, 429, 500, 502, 503, 504)
        raise LLMError(
            f"HTTP {exc.code} {exc.reason}：{_friendly_error(detail)}",
            status=exc.code,
            retryable=retryable,
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise LLMError(f"请求超时或网络不可达：{exc}", retryable=True) from exc
    except OSError as exc:  # pragma: no cover - 连接被重置等
        raise LLMError(f"网络错误：{exc}", retryable=True) from exc

    if not body.strip():
        raise LLMError("接口返回了空响应", retryable=True)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMError(f"接口返回的不是 JSON：{body[:300]}") from exc


def _friendly_error(detail: str) -> str:
    """把各家五花八门的报错压缩成一行。"""
    if not detail:
        return "（无错误信息）"
    try:
        data = json.loads(detail)
    except json.JSONDecodeError:
        return detail.replace("\n", " ")[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    if isinstance(error, str):
        return error[:300]
    if isinstance(data, dict):
        for key in ("message", "msg", "detail"):
            if data.get(key):
                return str(data[key])[:300]
    return detail.replace("\n", " ")[:300]


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象，容忍 markdown 代码块和前后废话。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回了空内容")

    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S | re.I)
    if fence:
        candidate = fence.group(1).strip()
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
        except json.JSONDecodeError:
            pass

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    except json.JSONDecodeError:
        pass

    # 括号配对扫描
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    fragment = text[start:index + 1]
                    try:
                        value = json.loads(fragment)
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        break
        break
    raise LLMError(f"模型没有返回合法 JSON：{text[:300]}")


class LLMClient:
    """一个 provider 对应一个客户端实例。"""

    def __init__(self, provider: Provider, *, json_mode: bool | None = None) -> None:
        self.provider = provider
        self.endpoint = build_endpoint(provider.base_url)
        self.json_mode = provider.json_mode if json_mode is None else json_mode

    # -- 基础调用 -----------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
        deadline: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.provider.model,
            "messages": messages,
            "temperature": self.provider.temperature if temperature is None else temperature,
            "max_tokens": self.provider.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        use_json = self.json_mode if json_mode is None else json_mode
        if use_json:
            payload["response_format"] = {"type": "json_object"}
        if self.provider.extra_body:
            payload.update(self.provider.extra_body)

        headers = {"Authorization": f"Bearer {self.provider.api_key}"} if self.provider.api_key else {}
        headers.update(self.provider.extra_headers)

        attempt = 0
        last_error: LLMError | None = None
        while attempt <= retries:
            attempt += 1
            call_timeout = float(timeout or self.provider.timeout)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 2:
                    break
                call_timeout = max(2.0, min(call_timeout, remaining))
            try:
                data = _post_json(self.endpoint, payload, headers, call_timeout)
                return self._extract_content(data)
            except LLMError as exc:
                last_error = exc
                # 有些服务不支持 response_format，去掉后重试一次
                if "response_format" in payload and exc.status in (400, 422, 404):
                    log.warning("服务不支持 JSON 模式，已自动降级：%s", exc)
                    payload.pop("response_format", None)
                    use_json = False
                    attempt -= 1
                    retries = max(retries, 1)
                    continue
                if not exc.retryable or attempt > retries:
                    raise
                wait = min(2 ** attempt * 0.8, 6.0)
                if deadline is not None and time.monotonic() + wait >= deadline - 2:
                    raise
                log.warning("模型调用失败（第 %s 次），%.1fs 后重试：%s", attempt, wait, exc)
                time.sleep(wait)
        raise last_error or LLMError("模型调用超时，请调低 timeout 或换更快的模型")

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not choices:
            message = _friendly_error(json.dumps(data, ensure_ascii=False))
            raise LLMError(f"接口没有返回 choices：{message}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            content = "".join(parts)
        content = (content or "").strip()
        if not content:
            # 部分推理模型只填了 reasoning_content
            content = str(message.get("reasoning_content") or "").strip()
        if not content:
            raise LLMError("模型返回了空内容", retryable=True)
        return content

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        text = self.chat(
            messages,
            json_mode=True,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            retries=retries,
            deadline=deadline,
        )
        return extract_json(text)

    # -- 健康检查 -----------------------------------------------------------

    def ping(self, timeout: float = 30) -> str:
        """发一条极短的请求验证 key 是否可用，返回模型回复。"""
        text = self.chat(
            [{"role": "user", "content": "只回复两个字：可用"}],
            json_mode=False,
            timeout=timeout,
            max_tokens=16,
            retries=0,
        )
        return text.strip()
