# -*- coding: utf-8 -*-
"""配置加载：读取 config.toml，并与内置默认配置深度合并。"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """配置文件缺失或内容不合法。"""


# ---------------------------------------------------------------------------
# 内置默认配置
# ---------------------------------------------------------------------------

DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "vision_model": "",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "model": "qwen-plus",
        "vision_model": "qwen-vl-max",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-flash",
        "vision_model": "glm-4v-flash",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "",
        "model": "moonshot-v1-8k",
        "vision_model": "moonshot-v1-8k-vision-preview",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "vision_model": "Qwen/Qwen2-VL-72B-Instruct",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "model": "qwen2.5:7b",
        "vision_model": "llava",
    },
}

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "public_url": "",  # 留空则自动使用 http://host:port
        "log_level": "INFO",
        "log_file": "data/logs/ocs.log",
    },
    "llm": {
        "provider": "deepseek",
        "timeout": 40,
        "max_retries": 2,
        "temperature": 0.1,
        "max_tokens": 1200,
        "json_mode": True,
        "providers": DEFAULT_PROVIDERS,
    },
    "vision": {
        "provider": "",  # 留空表示沿用 llm.provider
        "model": "",  # 留空表示使用该 provider 的 vision_model
        "max_images": 5,
        "max_image_bytes": 8 * 1024 * 1024,
        "timeout": 20,
    },
    "search": {
        "enabled": True,
        "mode": "auto",  # auto | always | never
        "providers": ["bing", "duckduckgo"],
        "max_results": 6,
        "timeout": 10,
        "tavily_api_key": "",
        "serper_api_key": "",
        "bocha_api_key": "",
    },
    "bank": {
        "dir": "data/bank",
        "file": "default.md",
        "fuzzy": True,
        "fuzzy_threshold": 0.92,
        "verify_options": True,
        "option_threshold": 0.72,
        "update_existing": True,
    },
    "behavior": {
        "total_timeout": 55,  # 单次请求的总预算（OCS 侧超时为 60 秒）
        "retry_on_low_confidence": True,
        "low_confidence": 0.45,
        "save_to_bank": True,
        "max_question_chars": 2000,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 override 合并进 base 的深拷贝，dict 递归合并，其它类型直接覆盖。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Provider:
    """一个大模型服务商的连接参数。"""

    name: str
    base_url: str
    api_key: str
    model: str
    vision_model: str = ""
    json_mode: bool = True
    timeout: float = 40.0
    temperature: float = 0.1
    max_tokens: int = 1200
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)


class Config:
    """合并后的配置对象，提供点号取值与 provider 解析。"""

    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self.data = data
        self.path = path

    # -- 加载 ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(
                f"找不到配置文件：{p}\n请先复制 config.example.toml 为 config.toml 并填写 api_key。"
            )
        try:
            with p.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - 仅做提示
            raise ConfigError(f"配置文件格式错误（TOML）：{exc}") from exc
        return cls(_deep_merge(DEFAULTS, raw), p.resolve())

    def reload(self) -> None:
        if self.path:
            other = Config.load(self.path)
            self.data = other.data

    # -- 通用取值 -----------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # -- 常用片段 -----------------------------------------------------------

    @property
    def server(self) -> dict[str, Any]:
        return self.data["server"]

    @property
    def bank_dir(self) -> Path:
        p = Path(self.get("bank.dir", "data/bank"))
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def bank_file(self) -> Path:
        p = Path(self.get("bank.file", "default.md"))
        return p if p.is_absolute() else (self.bank_dir / p)

    @property
    def base_dir(self) -> Path:
        if self.path:
            return self.path.parent
        return Path.cwd()

    @property
    def public_url(self) -> str:
        explicit = str(self.get("server.public_url", "") or "").strip()
        if explicit:
            return explicit.rstrip("/")
        host = self.get("server.host", "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.get('server.port', 8000)}"

    # -- provider -----------------------------------------------------------

    def provider(self, name: str | None = None) -> Provider:
        name = (name or self.get("llm.provider", "deepseek")).strip()
        providers: dict[str, Any] = self.get("llm.providers", {}) or {}
        if name not in providers:
            known = "、".join(sorted(providers)) or "（无）"
            raise ConfigError(f"未找到名为 {name!r} 的模型服务商，已配置的有：{known}")
        raw = dict(providers[name] or {})
        raw["name"] = name
        # [llm] 下的公共参数作为兜底
        for key in ("timeout", "temperature", "max_tokens", "json_mode"):
            if raw.get(key) is None or raw.get(key) == "":
                raw[key] = self.get(f"llm.{key}")
        provider = Provider(
            name=name,
            base_url=str(raw.get("base_url", "")).strip(),
            api_key=str(raw.get("api_key", "")).strip(),
            model=str(raw.get("model", "")).strip(),
            vision_model=str(raw.get("vision_model", "") or "").strip(),
            json_mode=bool(raw.get("json_mode", True)),
            timeout=float(raw.get("timeout") or 40),
            temperature=float(raw.get("temperature") or 0.1),
            max_tokens=int(raw.get("max_tokens") or 1200),
            extra_headers=dict(raw.get("extra_headers") or {}),
            extra_body=dict(raw.get("extra_body") or {}),
        )
        if not provider.base_url:
            raise ConfigError(f"[llm.providers.{name}] 缺少 base_url")
        if not provider.model:
            raise ConfigError(f"[llm.providers.{name}] 缺少 model")
        return provider

    def vision_provider(self) -> Provider | None:
        """返回识图用的 provider；未配置视觉模型时返回 None。"""
        name = str(self.get("vision.provider", "") or "").strip()
        provider = self.provider(name or None)
        model = str(self.get("vision.model", "") or "").strip()
        if model:
            provider.model = model
            return provider
        if provider.vision_model:
            provider.model = provider.vision_model
            return provider
        return None

    def check_ready(self) -> list[str]:
        """检查必填项，返回错误信息列表（空列表表示可用）。"""
        problems: list[str] = []
        try:
            provider = self.provider()
        except ConfigError as exc:
            return [str(exc)]
        if not provider.api_key and "127.0.0.1" not in provider.base_url and "localhost" not in provider.base_url:
            problems.append(
                f"模型服务商 [{provider.name}] 的 api_key 为空，请在 config.toml 中填写。"
            )
        try:
            vision = self.vision_provider()
        except ConfigError as exc:
            problems.append(str(exc))
            vision = None
        if (
            vision is not None
            and vision.name != provider.name
            and not vision.api_key
            and "127.0.0.1" not in vision.base_url
            and "localhost" not in vision.base_url
        ):
            problems.append(f"识图服务商 [{vision.name}] 的 api_key 为空，图片题将无法作答。")
        if self.get("search.enabled") and not self.get("search.providers"):
            problems.append("search.enabled 为 true 但 search.providers 为空。")
        return problems
