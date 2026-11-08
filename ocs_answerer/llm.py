# -*- coding: utf-8 -*-
"""极简 OpenAI 兼容协议客户端（只用标准库 urllib）。

支持 DeepSeek / 通义千问 / 智谱 / 月之暗面 / OpenAI / SiliconFlow / Ollama
等一切兼容 ``/chat/completions`` 的服务。
"""

from __future__ import annotations

import ast
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

# 模型把推理过程当正文输出、或输出被截断时，让它重新整理成 JSON
JSON_REPAIR_PROMPT = (
    "你上面的回复不是合法的 JSON。请忽略之前的推理与分析过程，"
    "直接把最终答案整理成一个 JSON 对象输出：以 { 开头、以 } 结尾，"
    "不要使用 Markdown 代码块，不要输出任何解释文字。"
)


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


def _loads_fragment(fragment: str) -> dict[str, Any] | None:
    """尽力把一段文本解析成 dict：标准 JSON 失败后尝试 Python 字面量与去尾逗号。"""
    try:
        value = json.loads(fragment)
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    except json.JSONDecodeError:
        pass
    # 有些模型会输出单引号 / True / None 这类 Python 字面量
    try:
        value = ast.literal_eval(fragment)
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    except (ValueError, SyntaxError):
        pass
    # 去掉对象/数组结尾多余的逗号再试一次
    cleaned = re.sub(r",\s*([}\]])", r"\1", fragment)
    if cleaned != fragment:
        try:
            value = json.loads(cleaned)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    return None


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象，容忍 markdown 代码块、<json> 标签和前后废话。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回了空内容")

    # <json>...</json>
    tagged = re.search(r"<json>\s*(.+?)\s*</json>", text, re.S | re.I)
    if tagged:
        value = _loads_fragment(tagged.group(1).strip())
        if value is not None:
            return value

    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S | re.I)
    if fence:
        value = _loads_fragment(fence.group(1).strip())
        if value is not None:
            return value

    value = _loads_fragment(text)
    if value is not None:
        return value

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
                    value = _loads_fragment(text[start:index + 1])
                    if value is not None:
                        return value
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
        fallback_reasoning: bool = False,
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
                content, reasoning, finish_reason = self._content_parts(data)
                if finish_reason == "length":
                    log.warning("模型输出被 max_tokens 截断，考虑调大 llm.max_tokens")
                if content:
                    return content
                if reasoning:
                    if fallback_reasoning:
                        # 推理模型有时只填 reasoning_content，交给上层尝试从中提取 JSON
                        log.warning("模型只返回了推理过程（%s 字），尝试从中解析", len(reasoning))
                        return reasoning
                    raise LLMError(
                        "模型只返回了推理过程，没有给出最终答案（可换非推理模型或调大 max_tokens）",
                        retryable=True,
                    )
                raise LLMError("模型返回了空内容", retryable=True)
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
    def _content_parts(data: dict[str, Any]) -> tuple[str, str, str]:
        """返回 (正文, 推理内容, 结束原因)。"""
        choices = data.get("choices")
        if not choices:
            message = _friendly_error(json.dumps(data, ensure_ascii=False))
            raise LLMError(f"接口没有返回 choices：{message}")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            content = "".join(parts)
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        return (
            str(content or "").strip(),
            str(reasoning or "").strip(),
            str(choice.get("finish_reason") or ""),
        )

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
        deadline: float | None = None,
        repair: bool = True,
    ) -> dict[str, Any]:
        text = self.chat(
            messages,
            json_mode=True,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            retries=retries,
            deadline=deadline,
            fallback_reasoning=True,
        )
        try:
            return extract_json(text)
        except LLMError as exc:
            if not repair:
                raise
            if deadline is not None and deadline - time.monotonic() < 8:
                log.warning("剩余时间不足，跳过 JSON 修复：%s", exc)
                raise
            log.warning("模型没有返回合法 JSON，修复一轮：%s", text[:120].replace("\n", " "))
            repair_messages = list(messages) + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": JSON_REPAIR_PROMPT},
            ]
            fixed = self.chat(
                repair_messages,
                json_mode=True,
                timeout=timeout,
                temperature=0,
                max_tokens=max_tokens,
                retries=0,
                deadline=deadline,
                fallback_reasoning=True,
            )
            try:
                return extract_json(fixed)
            except LLMError as second:
                raise LLMError(
                    f"模型连续两次没有返回合法 JSON（多半是推理型模型把思考过程当成了正文，"
                    f"建议换非推理模型或调大 max_tokens）。最后一次输出：{fixed[:200]}"
                ) from second

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
