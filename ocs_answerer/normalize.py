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


# ---------------------------------------------------------------------------
# 复刻 OCS 的文本归一化，用来预判 OCS 的多选「领先度消歧」
# ---------------------------------------------------------------------------

# OCS normalizeString：先去掉中英文标点与空白，全角转半角，再只保留中英文/数字
_OCS_PUNCT_CN_RE = re.compile(r"[，。！？；：\"'、（）【】《》\s]")
_OCS_PUNCT_EN_RE = re.compile(r"[,.\-!?;:'\"()\[\]<>]")
_OCS_FULLWIDTH_RE = re.compile(r"[ａ-ｚＡ-Ｚ０-９]")
_OCS_KEEP_RE = re.compile(r"[^\u2e80-\u9fffa-z0-9①-⑨]")


def ocs_similarity_key(text: str) -> str:
    """对齐 OCS 5.x 的 ``normalizeString(removeRedundant(x))``。

    只用于预判 OCS 的匹配行为，不参与题库 key 计算。
    """
    text = strip_option_label(text)
    text = _OCS_PUNCT_CN_RE.sub("", text)
    text = _OCS_PUNCT_EN_RE.sub("", text)
    text = _OCS_FULLWIDTH_RE.sub(lambda m: chr(ord(m.group(0)) - 0xFEE0), text)
    text = text.lower().replace("％", "%")
    return _OCS_KEEP_RE.sub("", text)


def dice_similarity(left: str, right: str) -> float:
    """string-similarity 用的 Dice 系数（二元组），与 OCS 依赖的实现一致。"""
    if left == right:
        return 1.0
    if len(left) < 2 or len(right) < 2:
        return 0.0
    bigrams: dict[str, int] = {}
    for index in range(len(left) - 1):
        gram = left[index:index + 2]
        bigrams[gram] = bigrams.get(gram, 0) + 1
    hits = 0
    for index in range(len(right) - 1):
        gram = right[index:index + 2]
        count = bigrams.get(gram, 0)
        if count:
            bigrams[gram] = count - 1
            hits += 1
    return 2.0 * hits / (len(left) + len(right) - 2)


def ocs_similar_options(texts: list[str], threshold: float = 0.6) -> bool:
    """任意两个选项是否「过于相似」，会被 OCS 5.x 的领先度消歧删掉一个。

    源码：packages/core/src/core/worker/resolvers/multiple.ts 的
    ``disambiguateSimilarOptions(options, ratings, threshold = 0.6)``。
    多选时它会把文本相似度大于阈值的两个选项当作「一字之差的干扰项」，
    只保留匹配度更高的那个 —— 如果两个都是正确答案，就会漏选。
    """
    keys = [ocs_similarity_key(text) for text in texts]
    keys = [key for key in keys if key]
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            if dice_similarity(left, right) > threshold:
                return True
    return False


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
    # 丢掉空行：OCS 用 "\n" 拼接选项文本，某个选项末尾带换行时会出现空行，
    # 保留它会把后面的选项整体顶偏一位，字母答案就全错了。
    lines = [line.strip() for line in text.split("\n") if line.strip()]
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
