# -*- coding: utf-8 -*-
"""题面归一化与选项解析。

归一化的目的有两个：
1. 生成稳定的缓存 key（同一个题目在不同页面上的标点、空格、【单选题】前缀不同，
   但语义相同）；
2. 给模糊匹配提供统一的文本。
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

# HTML 标签（OCS 的 innerText 一般不带标签，但部分平台会把 <p> 之类带进来）
_TAG_RE = re.compile(r"<[^>]{1,300}>")

# 【单选题】【多选】[判断题]（填空题）
_TYPE_TAG_RE = re.compile(
    r"[【\[（(]\s*(?:单项选择|多项选择|单选|多选|判断|填空|简答|问答|不定项选择|不定项|材料|阅读|听力)"
    r"\s*题?\s*[】\]）)]"
)

# 题号前缀：第1题 / 1. / (1) / 一、
_NUM_PREFIX_RE = re.compile(
    r"^\s*(?:第\s*\d{1,3}\s*题|\d{1,3}\s*[、.．)）]|[（(]\s*\d{1,3}\s*[)）]|[一二三四五六七八九十]{1,3}\s*[、.．])\s*"
)

_WS_RE = re.compile(r"[\s\u3000\xa0]+")

# 只保留：中英文、数字、下划线、以及少量有语义的符号
_KEEP_RE = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff+\-=*/^%<>√×@·]")

# 选项前缀：A. / A、 / A) / (A) / A．
_OPTION_LABEL_RE = re.compile(
    r"^\s*(?:[（(]\s*([A-Za-zＡ-Ｚａ-ｚ])\s*[)）]|([A-Za-zＡ-Ｚａ-ｚ])\s*[、.．:：)）])\s*"
)

# 单行多选项的切分（"A. 甲 B. 乙 C. 丙"）
_INLINE_SPLIT_RE = re.compile(r"\s+(?=[A-Ha-h]\s*[、.．)）])")

TYPE_NAMES = {
    "single": "单选题",
    "multiple": "多选题",
    "judgement": "判断题",
    "completion": "填空题",
}


def clean_text(text: str, nfkc: bool = True) -> str:
    """去掉 HTML 标签、反转义实体；nfkc=True 时把全角字符转成半角。"""
    if text is None:
        return ""
    text = str(text)
    if "&" in text:
        text = html.unescape(text)
    if "<" in text and ">" in text:
        text = _TAG_RE.sub(" ", text)
    if nfkc:
        text = unicodedata.normalize("NFKC", text)
    return text.replace("\u200b", "").replace("\ufeff", "")


def normalize_question(text: str, max_chars: int = 4000) -> str:
    """题目归一化：用于生成缓存 key 与模糊匹配。"""
    text = clean_text(text)
    if not text:
        return ""
    # 去掉【单选题】这类题型标记（可能嵌套多层）
    for _ in range(3):
        stripped = _TYPE_TAG_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _NUM_PREFIX_RE.sub("", text)
    text = text.lower()
    text = _WS_RE.sub("", text)
    text = _KEEP_RE.sub("", text)
    return text[:max_chars]


def strip_option_label(option: str) -> str:
    """去掉选项的字母前缀，例如 "A. 中国梦" -> "中国梦"。"""
    option = clean_text(option).strip()
    match = _OPTION_LABEL_RE.match(option)
    if match:
        return option[match.end():].strip()
    return option


def normalize_option(option: str) -> str:
    return _KEEP_RE.sub("", _WS_RE.sub("", strip_option_label(option).lower()))


@dataclass
class Option:
    """一个选项。raw 是 OCS 传过来的原始文本，回传答案时必须原样返回。"""

    label: str
    text: str
    raw: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "text": self.text, "raw": self.raw}


def parse_options(options_text: str) -> list[Option]:
    """把 OCS 传来的 "A. 甲\\nB. 乙" 解析成选项列表。

    兼容三种情况：
    * 每行一个选项；
    * 全部挤在一行（用空格分隔）；
    * 完全没有字母前缀（此时按 A/B/C 顺序补编号）。
    """
    if not options_text:
        return []
    text = clean_text(options_text).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # 去掉首尾空行，但保留中间的空行：图片选项可能没有文字，
    # 直接丢弃会让后面的选项整体错位，字母答案就会答错。
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    lines = [line.strip() for line in lines]
    if not lines:
        return []

    # 单行里塞了多个选项时尝试切分
    if len(lines) == 1 and len(_INLINE_SPLIT_RE.findall(lines[0])) >= 2:
        parts = [p.strip() for p in _INLINE_SPLIT_RE.split(lines[0]) if p.strip()]
        if len(parts) > 1:
            lines = parts

    options: list[Option] = []
    for index, line in enumerate(lines):
        match = _OPTION_LABEL_RE.match(line)
        if match:
            label = (match.group(1) or match.group(2) or "").upper()
            label = unicodedata.normalize("NFKC", label)
            body = line[match.end():].strip()
        else:
            label = chr(ord("A") + index) if index < 26 else str(index + 1)
            body = line
        options.append(Option(label=label, text=body, raw=line))
    return options


def format_options_for_prompt(options: list[Option]) -> str:
    return "\n".join(f"{opt.label}. {opt.text}" for opt in options)
