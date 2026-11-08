# -*- coding: utf-8 -*-
"""Markdown 题库：读写、检索（精确 + 模糊）、增量更新。

文件格式（人类可读、可手工编辑）::

    ## 中国梦是什么？

    - 类型: single
    - 选项:
      - A. 实现中华民族伟大复兴
      - B. 实现世界和平
    - 答案: A
    - 解析: 十九大的表述。
    - 来源: llm+web
    - 更新时间: 2026-11-08 11:02:13

只要 `## ` 标题是题干、`- 答案: A` 是答案，就能被正确加载。
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .normalize import (
    clean_text,
    normalize_option,
    normalize_question,
    parse_options,
)

_ONE_LINE_RE = re.compile(r"\s+")
_FIELD_RE = re.compile(r"^-\s*([^:：]{1,12})[:：]\s*(.*)$")

_FIELD_ALIASES = {
    "类型": "type",
    "题型": "type",
    "type": "type",
    "答案": "answer",
    "answer": "answer",
    "答案内容": "answer_text",
    "答案文本": "answer_text",
    "answer_text": "answer_text",
    "解析": "explain",
    "解释": "explain",
    "说明": "explain",
    "explain": "explain",
    "来源": "source",
    "source": "source",
    "更新时间": "updated",
    "时间": "updated",
    "updated": "updated",
}

HEADER = """# OCS 本地题库

> 由本地题库程序自动维护，也可以手工编辑。
> 每条记录以 `## 题干` 开头，字段写成 `- 名称: 值`，`- 选项:` 下面用 `  - A. xxx` 逐行列出。

"""


@dataclass
class BankEntry:
    key: str
    question: str
    qtype: str = "single"
    options: list[str] = field(default_factory=list)
    answer_labels: str = ""
    answer_text: str = ""
    explain: str = ""
    source: str = "llm"
    updated: str = ""
    file: str = ""

    def option_objects(self):
        return parse_options("\n".join(self.options))

    def to_markdown(self) -> str:
        lines: list[str] = [f"## {_one_line(self.question)}", ""]
        lines.append(f"- 类型: {self.qtype or 'single'}")
        if self.options:
            lines.append("- 选项:")
            for opt in self.options:
                lines.append(f"  - {_one_line(opt)}")
        lines.append(f"- 答案: {self.answer_labels or self.answer_text}")
        derived = self.derive_answer_text()
        if self.answer_text and self.answer_text != derived:
            lines.append(f"- 答案内容: {_one_line(self.answer_text)}")
        if self.explain:
            lines.append(f"- 解析: {_one_line(self.explain)}")
        lines.append(f"- 来源: {self.source}")
        lines.append(f"- 更新时间: {self.updated or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines) + "\n"

    def derive_answer_text(self) -> str:
        """根据字母答案和选项还原出「回传 OCS 的答案字符串」。"""
        if not self.answer_labels:
            return self.answer_text
        options = self.option_objects()
        if not options:
            return self.answer_text
        if not re.fullmatch(r"[A-Za-z#\s]+", self.answer_labels):
            # 不是字母答案（例如填空题文本），直接返回
            return self.answer_labels
        by_label = {opt.label: opt.raw for opt in options}
        parts: list[str] = []
        for label in re.split(r"[#\s,，]+", self.answer_labels.strip()):
            if not label:
                continue
            parts.append(by_label.get(label.upper(), label))
        return "#".join(parts) if parts else self.answer_text


def _one_line(text: str) -> str:
    # 展示用文本保留原始全角标点，只把换行压成空格
    text = _ONE_LINE_RE.sub(" ", clean_text(text or "", nfkc=False)).strip()
    return text


def options_similarity(left: list[str], right: list[str]) -> float:
    """比较两组选项的相似度（与顺序无关），取值 0~1。"""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    a = "|".join(sorted(normalize_option(x) for x in left if normalize_option(x)))
    b = "|".join(sorted(normalize_option(x) for x in right if normalize_option(x)))
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def strip_letters(answer: str) -> str:
    return re.sub(r"[\s,，.。．、#|;;]+", "", answer or "").upper()


class Bank:
    """题库集合。所有 .md 文件共用一个索引。"""

    def __init__(
        self,
        directory: str | Path,
        default_file: str | Path,
        *,
        fuzzy: bool = True,
        fuzzy_threshold: float = 0.92,
        verify_options: bool = True,
        option_threshold: float = 0.72,
        update_existing: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.default_file = Path(default_file)
        self.fuzzy = fuzzy
        self.fuzzy_threshold = fuzzy_threshold
        self.verify_options = verify_options
        self.option_threshold = option_threshold
        self.update_existing = update_existing

        self._lock = threading.RLock()
        self._entries: list[BankEntry] = []
        self._index: dict[str, list[BankEntry]] = {}
        self._by_file: dict[Path, list[BankEntry]] = {}

    # -- 加载 ---------------------------------------------------------------

    def load(self) -> int:
        with self._lock:
            self._entries = []
            self._index = {}
            self._by_file = {}
            if not self.directory.exists():
                self.directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.directory.glob("*.md")):
                entries = self._parse_file(path)
                if not entries:
                    continue
                self._by_file[path] = entries
                for entry in entries:
                    self._entries.append(entry)
                    self._index.setdefault(entry.key, []).append(entry)
            return len(self._entries)

    def _parse_file(self, path: Path) -> list[BankEntry]:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        entries: list[BankEntry] = []
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in content.splitlines():
            if line.startswith("## "):
                if current:
                    blocks.append(current)
                current = [line]
            elif current:
                current.append(line)
        if current:
            blocks.append(current)

        for block in blocks:
            entry = self._parse_block(block, path)
            if entry:
                entries.append(entry)
        return entries

    def _parse_block(self, block: list[str], path: Path) -> BankEntry | None:
        question = block[0][3:].strip()
        if not question:
            return None
        fields: dict[str, str] = {}
        options: list[str] = []
        section = ""
        for raw_line in block[1:]:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("- "):
                match = _FIELD_RE.match(line)
                if match:
                    raw_name = match.group(1).strip()
                    name = _FIELD_ALIASES.get(raw_name.lower(), "")
                    value = match.group(2).strip()
                    if name == "type":
                        section = ""
                        fields["type"] = value
                    elif name == "answer":
                        section = ""
                        fields["answer"] = value
                    elif name == "answer_text":
                        section = ""
                        fields["answer_text"] = value
                    elif name == "explain":
                        section = ""
                        fields["explain"] = value
                    elif name == "source":
                        section = ""
                        fields["source"] = value
                    elif name == "updated":
                        section = ""
                        fields["updated"] = value
                    elif raw_name in ("选项", "options", "Options"):
                        section = "options"
                        if value:
                            options.append(value)
                    elif section == "options":
                        # "A. 甲的答案: xxx" 这种选项内容也带冒号，按选项处理
                        options.append(line[2:].strip())
                    else:
                        section = ""
                    continue
                if section == "options":
                    options.append(line[2:].strip())
                continue
            if section == "options":
                # 兼容没有 "- " 的缩进选项
                options.append(line)

        entry = BankEntry(
            key=normalize_question(question),
            question=question,
            qtype=fields.get("type", "single") or "single",
            options=options,
            answer_labels=fields.get("answer", ""),
            answer_text=fields.get("answer_text", ""),
            explain=fields.get("explain", ""),
            source=fields.get("source", "bank"),
            updated=fields.get("updated", ""),
            file=str(path),
        )
        if not entry.answer_labels and entry.answer_text:
            entry.answer_labels = entry.answer_text
        if not entry.key:
            return None
        if not entry.answer_text:
            entry.answer_text = entry.derive_answer_text()
        return entry

    # -- 查询 ---------------------------------------------------------------

    def lookup(
        self, question: str, options: list[str] | str, *, image_hash: str = ""
    ) -> tuple[BankEntry | None, float]:
        """返回 (命中的条目, 匹配分)。未命中时返回 (None, 0)。"""
        key = normalize_question(question)
        if not key and image_hash:
            key = normalize_question(f"图片题 {image_hash[:12]}")
        if not key:
            return None, 0.0

        option_list = options if isinstance(options, list) else _split_options(options)
        with self._lock:
            best: BankEntry | None = None
            best_score = 0.0
            for entry in reversed(self._index.get(key, [])):
                score = self._score(entry, option_list)
                if score > best_score:
                    best, best_score = entry, score
            if best is not None and best_score >= self.option_threshold:
                return best, best_score

            if not self.fuzzy or len(key) < 6:
                return None, 0.0

            for entry in reversed(self._entries):
                if entry.key == key:
                    continue
                if not self._length_ok(entry.key, key):
                    continue
                matcher = SequenceMatcher(None, key, entry.key)
                if matcher.quick_ratio() < self.fuzzy_threshold:
                    continue
                ratio = matcher.ratio()
                if ratio < self.fuzzy_threshold:
                    continue
                score = self._score(entry, option_list) * ratio
                if score > best_score:
                    best, best_score = entry, score
            if best is not None and best_score >= self.option_threshold:
                return best, best_score
        return None, 0.0

    def _score(self, entry: BankEntry, option_list: list[str]) -> float:
        if not self.verify_options:
            return 1.0
        return options_similarity(entry.options, option_list)

    @staticmethod
    def _length_ok(a: str, b: str) -> bool:
        longer = max(len(a), len(b))
        if longer == 0:
            return False
        return abs(len(a) - len(b)) / longer <= 0.3

    # -- 写入 ---------------------------------------------------------------

    def save(self, entry: BankEntry) -> str:
        """写入题库，返回 'created' / 'updated' / 'skipped'。"""
        if not entry.key:
            entry.key = normalize_question(entry.question)
        if not entry.key:
            return "skipped"
        entry.updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            target: BankEntry | None = None
            for existing in self._index.get(entry.key, []):
                if options_similarity(existing.options, entry.options) >= 0.9:
                    target = existing
                    break

            if target is not None and self.update_existing:
                target.qtype = entry.qtype
                target.options = entry.options or target.options
                target.answer_labels = entry.answer_labels
                target.answer_text = entry.answer_text
                target.explain = entry.explain or target.explain
                target.source = entry.source
                target.updated = entry.updated
                self._rewrite_file(Path(target.file or self.default_file))
                return "updated"

            entry.file = str(self.default_file)
            self.directory.mkdir(parents=True, exist_ok=True)
            new_file = not self.default_file.exists()
            with self.default_file.open("a", encoding="utf-8", newline="\n") as fh:
                if new_file:
                    fh.write(HEADER)
                else:
                    fh.write("\n")
                fh.write(entry.to_markdown())
            self._entries.append(entry)
            self._index.setdefault(entry.key, []).append(entry)
            self._by_file.setdefault(self.default_file, []).append(entry)
            return "created"

    def _rewrite_file(self, path: Path) -> None:
        entries = self._by_file.get(path)
        if entries is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        body = HEADER + "\n".join(entry.to_markdown() for entry in entries)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    # -- 统计 ---------------------------------------------------------------

    def stats(self) -> dict[str, object]:
        with self._lock:
            files = sorted(self._by_file)
            return {
                "entries": len(self._entries),
                "files": [
                    {"path": str(p), "entries": len(self._by_file[p])} for p in files
                ],
                "directory": str(self.directory),
            }

    def search(self, keyword: str, limit: int = 20) -> list[BankEntry]:
        keyword = normalize_question(keyword)
        with self._lock:
            if not keyword:
                return list(reversed(self._entries))[:limit]
            hits = [e for e in self._entries if keyword in e.key]
            return list(reversed(hits))[:limit]


def _split_options(options: str | list[str]) -> list[str]:
    if isinstance(options, list):
        return options
    return [line for line in str(options or "").splitlines() if line.strip()]
