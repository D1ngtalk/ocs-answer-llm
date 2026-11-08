# -*- coding: utf-8 -*-
"""首次运行向导：config.toml 不存在时引导选择服务商、填 API Key。

目标是「下载后双击 start.bat 就能用」：

* 复制 config.example.toml（连注释一起保留），只改其中几行；
* 列出预置服务商，选择序号 + 模型名；
* 输入 API Key（不回显），可选立即做一次连通性测试，失败可以重输；
* 写回 config.toml 后由调用方继续启动服务。

所有交互都走 ``ask`` 参数，方便测试时注入脚本化输入。
"""

from __future__ import annotations

import getpass
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULT_PROVIDERS, Config, ConfigError
from .llm import LLMClient

Ask = Callable[..., str]
Echo = Callable[..., None]
Verifier = Callable[[Path, Echo], bool]

# 序号、显示名、一句话说明、申请密钥的地址
PROVIDER_CATALOG: tuple[tuple[str, str, str, str], ...] = (
    ("deepseek", "DeepSeek", "便宜、中文好，支持识图", "https://platform.deepseek.com/api_keys"),
    ("qwen", "通义千问", "识图能力最强（qwen-vl-max）", "https://bailian.console.aliyun.com/"),
    ("zhipu", "智谱 GLM", "有免费额度", "https://bigmodel.cn/usercenter/apikeys"),
    ("moonshot", "Kimi", "", "https://platform.moonshot.cn/console/api-keys"),
    ("openai", "OpenAI", "", "https://platform.openai.com/api-keys"),
    ("siliconflow", "SiliconFlow", "可白嫖部分开源模型", "https://cloud.siliconflow.cn/account/ak"),
    ("ollama", "Ollama（本地）", "需先装好 ollama，无需密钥", ""),
    ("custom", "自定义接口", "OpenAI 兼容的中转/自建服务", ""),
)

_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")


# ---------------------------------------------------------------------------
# TOML 行级编辑（保留注释与用户的自定义内容）
# ---------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    """转成 TOML 基本字符串（与 JSON 的转义规则兼容）。"""
    return json.dumps(str(value), ensure_ascii=False)


def get_toml_value(text: str, section: str, key: str) -> str | None:
    """读取某个节里 key 的原始值（未做类型转换）；找不到返回 None。"""
    current = ""
    for line in text.splitlines():
        header = _SECTION_RE.match(line)
        if header:
            current = header.group(1).strip()
            continue
        if current != section:
            continue
        match = re.match(r"^\s*" + re.escape(key) + r"\s*=\s*(.*)$", line)
        if match:
            return match.group(1).strip()
    return None


def _split_inline_comment(rest: str) -> tuple[str, str]:
    """把 ``"value"   # 注释`` 拆成 (值, 注释)，忽略引号内的 #。"""
    in_string = False
    escaped = False
    quote = ""
    for index, char in enumerate(rest):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
        elif char == "#":
            return rest[:index], rest[index:]
    return rest, ""


def set_toml_value(text: str, section: str, key: str, literal: str) -> str:
    """把 ``section`` 里 ``key`` 的值改成 ``literal``（已带引号/已格式化的字面量）。

    保持其它行原样；key 不存在就补在所属节末尾；整节不存在就追加到文件末尾。
    行尾的 ``# 注释`` 会被保留。
    """
    lines = text.splitlines()
    trailing_newline = text.endswith("\n")
    current = ""
    found_section = False
    replaced = False
    last_in_section = -1

    for index, line in enumerate(lines):
        header = _SECTION_RE.match(line)
        if header:
            current = header.group(1).strip()
            if current == section:
                found_section = True
                last_in_section = index
            continue
        if current != section:
            continue
        last_in_section = index
        if replaced:
            continue
        match = re.match(r"^(\s*)" + re.escape(key) + r"\s*=(.*)$", line)
        if match:
            value_part, comment = _split_inline_comment(match.group(2))
            new_line = f"{match.group(1)}{key} = {literal}"
            if comment:
                # 保留原来的对齐空格，让注释仍然对齐
                padding = value_part[len(value_part.rstrip()) :] or "  "
                new_line += padding + comment
            lines[index] = new_line
            replaced = True

    if not replaced and found_section:
        lines.insert(max(last_in_section + 1, 0), f"{key} = {literal}")
        replaced = True

    if not replaced:  # 整节都没有
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"[{section}]", f"{key} = {literal}"])
        trailing_newline = True

    return "\n".join(lines) + ("\n" if trailing_newline else "")


def ensure_toml_value(text: str, section: str, key: str, literal: str) -> str:
    """仅当 key 不存在时才写入，避免覆盖用户改过的 base_url。"""
    if get_toml_value(text, section, key) is not None:
        return text
    return set_toml_value(text, section, key, literal)


def configured_api_key(text: str, provider: str) -> str:
    """从配置文本里取某个服务商已保存的 api_key（解析失败时返回空串）。"""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ""
    node: Any = raw
    for part in ("llm", "providers", provider):
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
    if not isinstance(node, dict):
        return ""
    return str(node.get("api_key") or "")


# ---------------------------------------------------------------------------
# 交互辅助
# ---------------------------------------------------------------------------


def _stdin_is_console() -> bool:
    """判断 stdin 是否是真的控制台（能否安全地交互）。

    Windows 上 ``isatty()`` 对 NUL 设备也返回 True，用来判断会误判；
    ``GetConsoleMode`` 只对真正的控制台输入句柄成功。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)
            if handle in (0, -1):
                return False
            mode = ctypes.c_uint32()
            return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        except Exception:  # pragma: no cover - 极老的 Windows
            return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def _default_ask(prompt: str, *, secret: bool = False) -> str:
    if secret:
        try:
            if _stdin_is_console():
                return getpass.getpass(prompt)
        except Exception:  # getpass 在个别终端会抛异常，退回普通输入
            pass
    try:
        return input(prompt)
    except EOFError:
        return ""


def _ask_yes_no(ask: Ask, prompt: str, *, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    raw = ask(f"{prompt} {hint}：").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1", "true", "ok", "好")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_template(config_path: Path, template_path: str | Path | None) -> Path | None:
    if template_path:
        p = Path(template_path)
        return p if p.exists() else None
    for candidate in (
        config_path.parent / "config.example.toml",
        Path(__file__).resolve().parent.parent / "config.example.toml",
    ):
        if candidate.exists():
            return candidate
    return None


def _fallback_template() -> str:
    """连模板都没有时，用内置默认值拼一份最小配置。"""
    lines = [
        "# 没有找到 config.example.toml，这是自动生成的最小配置",
        "",
        "[server]",
        'host = "127.0.0.1"',
        "port = 8000",
        'log_level = "INFO"',
        'log_file = "data/logs/ocs.log"',
        "",
        "[llm]",
        'provider = "deepseek"',
        "timeout = 40",
        "temperature = 0.1",
        "max_tokens = 1600",
        "json_mode = true",
        'thinking = "auto"',
        'thinking_effort = "low"',
        "",
    ]
    for name, item in DEFAULT_PROVIDERS.items():
        lines.append(f"[llm.providers.{name}]")
        lines.append(f'base_url = "{item.get("base_url", "")}"')
        lines.append('api_key = ""')
        lines.append(f'model = "{item.get("model", "")}"')
        if item.get("vision_model"):
            lines.append(f'vision_model = "{item["vision_model"]}"')
        lines.append("")
    lines += [
        "[vision]",
        'provider = ""',
        'model = ""',
        "max_images = 8",
        "max_image_bytes = 8388608",
        "label_images = true",
        "timeout = 20",
        "",
        "[search]",
        "enabled = true",
        'mode = "auto"',
        'providers = ["bing", "duckduckgo"]',
        "max_results = 6",
        "timeout = 10",
        "",
        "[bank]",
        'dir = "data/bank"',
        'file = "default.md"',
        "fuzzy = true",
        "fuzzy_threshold = 0.92",
        "verify_options = true",
        "option_threshold = 0.72",
        "update_existing = true",
        "",
        "[behavior]",
        "total_timeout = 55",
        "retry_on_low_confidence = true",
        "low_confidence = 0.45",
        "save_to_bank = true",
        "max_question_chars = 2000",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 服务商信息
# ---------------------------------------------------------------------------


def _template_providers(text: str) -> dict[str, dict[str, Any]]:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raw = {}
    providers = (raw.get("llm") or {}).get("providers") or {}
    return {str(k): dict(v or {}) for k, v in providers.items()}


def _provider_default(name: str, template_providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    base = dict(DEFAULT_PROVIDERS.get(name, {}))
    base.update({k: v for k, v in template_providers.get(name, {}).items() if v not in ("", None)})
    return base


def _display_width(text: str) -> int:
    """按终端显示宽度算长度：中文/全角算 2 列。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _print_menu(echo: Echo, defaults: dict[str, dict[str, Any]], default_index: int = 1) -> None:
    echo(f"请选择模型服务商（输入序号，直接回车使用 {default_index}）：")
    echo("")
    for index, (name, label, note, _url) in enumerate(PROVIDER_CATALOG, start=1):
        model = str(defaults.get(name, {}).get("model") or ("自定义" if name == "custom" else ""))
        mark = "  ← 推荐" if name == "deepseek" else ""
        line = f"  {index}) {_pad(label, 14)} {_pad(model, 26)}"
        if note:
            line += f" {note}"
        echo(line + mark)
    echo("")


def _ask_choice(
    ask: Ask, echo: Echo, defaults: dict[str, dict[str, Any]], default_name: str = "deepseek"
) -> tuple[str, str, str]:
    """返回 (provider 名, 显示名, 申请密钥地址)。"""
    total = len(PROVIDER_CATALOG)
    names = [name for name, *_ in PROVIDER_CATALOG]
    default_index = names.index(default_name) + 1 if default_name in names else 1
    _print_menu(echo, defaults, default_index)
    while True:
        raw = ask(f"服务商序号 [{default_index}]：").strip()
        if not raw:
            name, label, _note, url = PROVIDER_CATALOG[default_index - 1]
            return name, label, url
        if raw.isdigit() and 1 <= int(raw) <= total:
            name, label, _note, url = PROVIDER_CATALOG[int(raw) - 1]
            return name, label, url
        echo(f"  请输入 1-{total} 之间的序号。")


def _ask_model(ask: Ask, echo: Echo, name: str, default: str) -> str:
    if name == "custom" or not default:
        while True:
            model = ask("模型名称（如 deepseek-chat，必填）：").strip()
            if model:
                return model
            echo("  模型名称不能为空。")
    model = ask(f"模型名称 [{default}]：").strip()
    return model or default


def _ask_base_url(ask: Ask, echo: Echo, default: str) -> str:
    while True:
        echo("  填入兼容 OpenAI 的地址，例如 https://api.example.com/v1")
        url = ask(f"接口地址 [{default or '必填'}]：").strip()
        if not url and default:
            return default
        if url.startswith(("http://", "https://")):
            return url.rstrip("/")
        echo("  地址需要以 http:// 或 https:// 开头。")


# ---------------------------------------------------------------------------
# 连通性测试
# ---------------------------------------------------------------------------


def _default_verify(path: Path, echo: Echo) -> bool:
    try:
        cfg = Config.load(path)
        provider = cfg.provider()
        reply = LLMClient(provider).ping(timeout=30)
    except Exception as exc:  # noqa: BLE001 - 这里的异常都要变成人话提示
        echo(f"      [!!] 调用失败：{str(exc)[:200]}")
        echo("           常见原因：密钥填错、余额不足、网络不通、模型名写错。")
        return False
    echo(f"      [OK] 调用成功：{reply[:30] or '（空回复）'}")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_wizard(
    config_path: str | Path = "config.toml",
    *,
    template_path: str | Path | None = None,
    ask: Ask | None = None,
    echo: Echo | None = None,
    verify: bool | Verifier = True,
    force: bool = False,
) -> bool:
    """引导生成 config.toml，返回是否已就绪。

    已经存在且 ``force`` 为 False 时直接返回 True，不做任何事。
    """
    path = Path(config_path)
    echo = echo or (lambda line="": print(line))
    interactive = ask is not None
    ask = ask or _default_ask

    if path.exists() and not force:
        return True

    if not interactive and not _stdin_is_console():
        template_name = Path(template_path).name if template_path else "config.example.toml"
        if path.exists():
            raise ConfigError(
                "当前不是交互式终端，无法运行配置向导。\n"
                f"请在终端里运行 python main.py setup，或直接编辑 {path}。"
            )
        raise ConfigError(
            f"找不到配置文件：{path}\n"
            "当前不是交互式终端，无法引导初始化。\n"
            f"请复制 {template_name} 为 {path.name} 并填写 api_key，\n"
            "或在本机终端运行：python main.py setup"
        )

    created = not path.exists()
    if created:
        template = _find_template(path, template_path)
        if template is not None:
            text = _read_text(template)
        else:
            echo("[提示] 没有找到 config.example.toml，改用内置默认模板。")
            text = _fallback_template()
        # 先把模板落盘：中途 Ctrl+C 也不会白跑一趟，config.toml 已经在那儿了，
        # 下次启动会因为 api_key 为空再进一次向导。
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"无法写入配置文件 {path}：{exc}") from exc
    else:
        text = _read_text(path)

    echo("")
    echo("=" * 64)
    echo("  初始化配置向导")
    echo("=" * 64)
    if created:
        echo(f"没有找到 {path.name}，已根据模板生成一份，接下来按提示选模型、填密钥即可。")
    else:
        echo(f"将更新 {path.name} 里的服务商、模型与 API Key。")
    echo("")

    defaults = {name: _provider_default(name, _template_providers(text)) for name, *_ in PROVIDER_CATALOG}
    current = (get_toml_value(text, "llm", "provider") or "").strip().strip('"').strip("'")
    name, label, key_url = _ask_choice(ask, echo, defaults, default_name=current or "deepseek")

    default_model = str(defaults.get(name, {}).get("model") or "")
    base_url = str(defaults.get(name, {}).get("base_url") or "")
    if name == "custom":
        base_url = _ask_base_url(ask, echo, "")
    model = _ask_model(ask, echo, name, default_model)

    local_endpoint = bool(base_url) and ("127.0.0.1" in base_url or "localhost" in base_url)
    api_key = ""
    if name == "ollama":
        api_key = str(defaults.get(name, {}).get("api_key") or "ollama")
        echo("  Ollama 跑在本机，不需要密钥。")
    elif local_endpoint:
        echo("  检测到本地地址，API Key 可以留空。")
    if not api_key and not local_endpoint:
        existing_key = configured_api_key(text, name)
        echo("")
        if key_url:
            echo(f"  {label} 的密钥申请地址：{key_url}")
        if existing_key:
            echo(f"  当前已保存密钥（{len(existing_key)} 位），直接回车保持不变。")
        else:
            echo("  粘贴后按回车；输入时不回显。留空可稍后再填。")
        api_key = ask(f"请输入 {label} 的 API Key：", secret=True).strip()

    # --- 写入配置 ----------------------------------------------------------
    provider_section = f"llm.providers.{name}"
    text = set_toml_value(text, "llm", "provider", _toml_str(name))
    if base_url:
        text = ensure_toml_value(text, provider_section, "base_url", _toml_str(base_url))
    if name == "custom":
        text = set_toml_value(text, provider_section, "base_url", _toml_str(base_url))
    text = set_toml_value(text, provider_section, "model", _toml_str(model))
    if name == "custom":
        text = ensure_toml_value(text, provider_section, "vision_model", _toml_str(""))
    if api_key:
        text = set_toml_value(text, provider_section, "api_key", _toml_str(api_key))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法写入配置文件 {path}：{exc}") from exc

    echo("")
    echo("-" * 64)
    echo(f"[1/3] 配置已写入 {path}")
    echo(f"[2/3] 服务商：{label}    模型：{model}")

    saved_key = configured_api_key(text, name)
    key_ready = bool(saved_key) or local_endpoint
    if not key_ready:
        echo("[3/3] [!!] 没有填写 API Key，配置已保存但暂时无法使用。")
        echo("       稍后运行 python main.py setup 补填即可。")
        echo("-" * 64)
        return False

    if verify is False:
        echo("[3/3] 已跳过连通性测试。")
        echo("-" * 64)
        return True

    verifier: Verifier = _default_verify if verify is True else verify
    for attempt in range(1, 4):
        echo("[3/3] 正在测试连通性…" if attempt == 1 else "      再测一次…")
        if verifier(path, echo):
            echo("-" * 64)
            return True
        if attempt >= 3:
            echo("      配置已保存，但多次测试未通过。可以先运行 python main.py check 复查。")
            echo("-" * 64)
            return True
        if not _ask_yes_no(ask, "      是否重新输入 API Key 再试一次？", default=True):
            echo("      已跳过。稍后可以运行 python main.py check 复查。")
            echo("-" * 64)
            return True
        retry_key = ask(f"请重新输入 {label} 的 API Key：", secret=True).strip()
        if not retry_key:
            echo("      未输入新密钥，已跳过测试。")
            echo("-" * 64)
            return True
        text = set_toml_value(_read_text(path), provider_section, "api_key", _toml_str(retry_key))
        path.write_text(text, encoding="utf-8")
        echo(f"      已更新密钥（{len(retry_key)} 位）")

    return True
