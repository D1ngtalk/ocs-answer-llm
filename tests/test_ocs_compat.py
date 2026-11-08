# -*- coding: utf-8 -*-
"""兼容性测试：把 OCS 的答案匹配算法移植成 Python，验证本程序输出的答案格式。

对应源码（4.0 分支）：
* packages/core/src/core/worker/utils.ts            —— splitAnswer
* packages/core/src/core/utils/string.ts             —— removeRedundant / normalizeString / answerNormalizedMatch
* packages/core/src/core/worker/resolvers/*.ts       —— single / multiple / judgement / completion

只要这里的用例通过，答案就能被 OCS 正确识别。
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocs_answerer.normalize import parse_options  # noqa: E402

CJK = "\u2e80-\u9fff"
KEEP_RE = re.compile(rf"[^\{CJK}A-Za-z0-9①②③④⑤⑥⑦⑧⑨]*")


def clear_string(text: str) -> str:
    return KEEP_RE.sub("", text.strip().lower())


def remove_redundant(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(rf"^[A-Z][^A-Za-z0-9{CJK}]+([A-Za-z0-9{CJK}]+)", r"\1", text)
    text = re.sub(rf"^[A-Z]([{CJK}][A-Za-z0-9{CJK}]*)", r"\1", text)
    return text


def normalize_string(text: str) -> str:
    text = re.sub(r"[，。！？；：\"'、（）【】《》\s]", "", text)
    text = re.sub(r"[,.\-!?;:'\"()\[\]<>]", "", text)
    text = re.sub(r"[ａ-ｚＡ-Ｚ０-９]", lambda m: chr(ord(m.group(0)) - 0xFEE0), text)
    return clear_string(text.replace("％", "%"))


def answer_normalized_match(answers: list[str], options: list[str]) -> list[str]:
    _answers = [normalize_string(remove_redundant(a)) for a in answers]
    _options = [normalize_string(remove_redundant(o)) for o in options]
    scored = []
    for index, option in enumerate(_options):
        level = 0
        if option:
            for answer in _answers:
                if answer == option:
                    level = 2
                    break
                if option in answer:
                    level = 1
        return_level = level
        scored.append((return_level, index))
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: -item[0])
    return [options[index] for _, index in scored]


def answer_similar(answers: list[str], options: list[str]) -> list[float]:
    _answers = [clear_string(remove_redundant(a)) for a in answers]
    ratings = []
    for option in options:
        text = clear_string(remove_redundant(option))
        if not text:
            ratings.append(0.0)
            continue
        best = 0.0
        for answer in _answers:
            if not answer:
                continue
            # string-similarity 用的是 Dice 系数（二元组）
            best = max(best, _dice(text, answer))
        ratings.append(best)
    return ratings


def _dice(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 0.0
    bigrams_a = [a[i:i + 2] for i in range(len(a) - 1)]
    bigrams_b = [b[i:i + 2] for i in range(len(b) - 1)]
    pool = list(bigrams_b)
    hits = 0
    for gram in bigrams_a:
        if gram in pool:
            pool.remove(gram)
            hits += 1
    return 2 * hits / (len(bigrams_a) + len(bigrams_b))


def split_answer(answer: str, separators=("===", "#", "---", "###", "|", ";", "；")) -> list[str]:
    """严格对齐 5.0 的 splitAnswer：先试 JSON 数组，失败才按分隔符切。"""
    answer = answer.strip()
    if not answer:
        return []
    try:
        data = json.loads(answer)
    except json.JSONDecodeError:
        for sep in separators:
            if sep in answer:
                return [part.strip() for part in answer.split(sep) if part.strip()]
        return [answer]
    if isinstance(data, list):
        return [str(item).strip() for item in data if str(item).strip()]
    # JSON 解析成功但不是数组（如纯数字），不会走分隔符分支
    return [answer]


def resolve_single(answers: list[str], options: list[str]) -> str | None:
    all_answer: list[str] = []
    for answer in answers:
        all_answer.extend(split_answer(answer))
    option_strings = [remove_redundant(option) for option in options]

    matched = answer_normalized_match(all_answer, option_strings)
    if matched:
        for index, option in enumerate(option_strings):
            if option == matched[0]:
                return options[index]

    ratings = answer_similar(all_answer, option_strings)
    best_index, best_rating = -1, 0.0
    for index, rating in enumerate(ratings):
        if rating > best_rating:
            best_index, best_rating = index, rating
    if best_index != -1 and best_rating > 0.6:
        return options[best_index]

    for answer in all_answer:
        plain = re.sub(r"[,，.。．、 #]", "", answer).strip()
        if re.fullmatch(r"[A-Za-z]", plain):
            index = ord(plain.upper()) - 65
            if 0 <= index < len(options):
                return options[index]
    return None


def resolve_multiple(answers: list[str], options: list[str]) -> list[str] | None:
    option_strings = [remove_redundant(option) for option in options]
    for answer in answers:
        parts = split_answer(answer)
        matched = answer_normalized_match(parts, option_strings)
        if matched:
            result = []
            for option in matched:
                index = option_strings.index(option)
                result.append(options[index])
            return result

    plain: list[str] = []
    for answer in answers:
        compact = re.sub(r"[,，.。．、 #]", "", answer).strip()
        if re.fullmatch(r"[A-Za-z]{2,}", compact):
            for char in compact:
                index = ord(char.upper()) - 65
                if 0 <= index < len(options):
                    plain.append(options[index])
    return plain or None


CORRECT_WORDS = ["是", "对", "正确", "确定", "√", "对的", "是的", "正确的", "true", "t", "yes", "1"]
INCORRECT_WORDS = ["非", "否", "错", "错误", "×", "x", "错的", "不对", "不正确的", "不正确", "不是", "false", "f", "no", "0"]


def _judge_equal(target: str, word: str) -> bool:
    return clear_string(remove_redundant(word).replace("√", "").replace("×", "")) == clear_string(
        remove_redundant(target).replace("√", "").replace("×", "")
    )


def resolve_judgement(answers: list[str], options: list[str]) -> str | None:
    shows_correct = any(_judge_equal(answer, word) for answer in answers for word in CORRECT_WORDS)
    shows_incorrect = any(_judge_equal(answer, word) for answer in answers for word in INCORRECT_WORDS)
    if not shows_correct and not shows_incorrect:
        return None
    for option in options:
        option_correct = any(_judge_equal(option, word) for word in CORRECT_WORDS)
        option_incorrect = any(_judge_equal(option, word) for word in INCORRECT_WORDS)
        if shows_correct and option_correct:
            return option
        if shows_incorrect and option_incorrect:
            return option
    return None


def resolve_completion(answers: list[str], blank_count: int) -> list[str] | None:
    """一个题库返回的答案列表 → 每个空要填的内容。"""
    ans = [item for item in answers if item]
    if len(ans) == 1:
        ans = split_answer(ans[0])
    if ans and (len(ans) == blank_count or blank_count == 1):
        if len(ans) == blank_count:
            return ans
        if blank_count == 1:
            return [" ".join(ans)]
    return None


class OCSCompatTest(unittest.TestCase):
    """拿本程序真实产生的答案，喂给 OCS 的匹配算法。"""

    def test_single_choice(self):
        options = parse_options("A. 甲\nB. 世界和平\nC. 丙")
        answer = "B. 世界和平"
        self.assertEqual(resolve_single([answer], [o.raw for o in options]), "B. 世界和平")

    def test_single_without_label_in_page(self):
        """页面 innerText 不带字母前缀时也要能匹配。"""
        options = parse_options("甲\n世界和平\n丙")
        self.assertEqual(resolve_single(["B. 世界和平"], [o.raw for o in options]), "世界和平")

    def test_single_text_only(self):
        options = parse_options("A. 甲\nB. 世界和平\nC. 丙")
        self.assertEqual(resolve_single(["世界和平"], [o.raw for o in options]), "B. 世界和平")

    def test_multiple_choice(self):
        options = parse_options("A. 甲\nB. 乙\nC. 丙\nD. 丁")
        answer = "A. 甲#C. 丙"
        picked = resolve_multiple([answer], [o.raw for o in options])
        self.assertEqual(picked, ["A. 甲", "C. 丙"])

    def test_multiple_plain_letters(self):
        options = parse_options("A. 甲\nB. 乙\nC. 丙")
        self.assertEqual(resolve_multiple(["AC"], [o.raw for o in options]), ["A. 甲", "C. 丙"])

    def test_judgement(self):
        options = parse_options("A. 正确\nB. 错误")
        self.assertEqual(resolve_judgement(["A. 正确"], [o.raw for o in options]), "A. 正确")
        self.assertEqual(resolve_judgement(["B. 错误"], [o.raw for o in options]), "B. 错误")

    def test_judgement_plain_words(self):
        options = parse_options("A. 对\nB. 错")
        self.assertEqual(resolve_judgement(["A. 对"], [o.raw for o in options]), "A. 对")
        # 只有「对/错」文本、没有字母时也能匹配
        self.assertEqual(resolve_judgement(["对"], [o.raw for o in options]), "A. 对")
        self.assertEqual(resolve_judgement(["错误"], [o.raw for o in options]), "B. 错")

    def test_completion(self):
        self.assertEqual(resolve_completion(["北京#上海"], 2), ["北京", "上海"])
        self.assertEqual(resolve_completion(["北京"], 1), ["北京"])

    def test_image_options_plain_letter(self):
        """选项是图片时，OCS 侧的选项文本是图片 URL，只能靠纯字母兜底命中。"""
        option_texts = [
            "A. https://cdn.example.com/1.png",
            "B. https://cdn.example.com/2.png",
            "C. https://cdn.example.com/3.png",
        ]
        self.assertEqual(resolve_single(["B"], option_texts), option_texts[1])
        self.assertEqual(resolve_multiple(["A#C"], option_texts), [option_texts[0], option_texts[2]])

    def test_completion_answer_with_separator(self):
        """答案本身含 # 时，本程序会换成 JSON 数组，OCS 解析后原样保留。"""
        raw = '["2#3#5#7#"]'
        self.assertEqual(resolve_completion([raw], 1), ["2#3#5#7#"])
        # 旧的直接拼接会被切碎成用空格连接
        self.assertEqual(resolve_completion(["2#3#5#7#"], 1), ["2 3 5 7"])

    def test_completion_multiple_blanks(self):
        self.assertEqual(resolve_completion(['["甲","乙"]'], 2), ["甲", "乙"])
        self.assertEqual(resolve_completion(["甲#乙"], 2), ["甲", "乙"])

    def test_math_option_not_confused(self):
        """选项内容带数字时，不能把 A/B/C 字母和选项内容搞混。"""
        options = parse_options("A. 2\nB. 3\nC. 4")
        self.assertEqual(resolve_single(["C. 4"], [o.raw for o in options]), "C. 4")
        self.assertEqual(resolve_single(["4"], [o.raw for o in options]), "C. 4")


class ImageOptimizeCompatTest(unittest.TestCase):
    """对应 OCS 5.x 的 isAnswererWrapperSupportImageOptimize()。

    只有 POST + 配置里实际引用图片题字段，脚本才不会弹
    「当前题库配置暂不支持图片、公式等题目处理」。
    """

    PLACEHOLDERS = ("${images}", "${suggestion_title}", "${suggestion_options}")
    ENV_REF = re.compile(r"(^|[^\w$])(images|suggestion_title|suggestion_options)(?![\w$])")

    @classmethod
    def supports(cls, wrapper: dict) -> bool:
        if str(wrapper.get("method") or "get").lower() != "post":
            return False
        for value in (wrapper.get("data") or {}).values():
            if isinstance(value, str):
                if any(placeholder in value for placeholder in cls.PLACEHOLDERS):
                    return True
            elif isinstance(value, dict) and isinstance(value.get("handler"), str):
                if cls.ENV_REF.search(value["handler"]):
                    return True
        return False

    def test_generated_config_is_recognized(self):
        from ocs_answerer.server import build_ocs_config

        wrapper = build_ocs_config("http://127.0.0.1:8000")[0]
        self.assertTrue(self.supports(wrapper), "生成的配置应被 OCS 判定为支持图片题优化")

    def test_old_config_is_not_recognized(self):
        """1.0 版本的配置（只读 title/options、DOM 抓图）确实会被判定为不支持。"""
        old = {
            "method": "post",
            "data": {
                "title": "${title}",
                "type": "${type}",
                "options": "${options}",
                "image": {"handler": "return (env) => { const urls = []; return urls.join(','); }"},
            },
        }
        self.assertFalse(self.supports(old))

    def test_get_method_is_not_supported(self):
        from ocs_answerer.server import build_ocs_config

        wrapper = build_ocs_config("http://127.0.0.1:8000")[0]
        wrapper["method"] = "get"
        self.assertFalse(self.supports(wrapper))


if __name__ == "__main__":
    unittest.main(verbosity=2)
