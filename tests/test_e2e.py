# -*- coding: utf-8 -*-
"""端到端测试：用假的 OpenAI 兼容服务替代真实模型，验证完整链路。"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocs_answerer.bank import Bank, options_similarity  # noqa: E402
from ocs_answerer.config import Config  # noqa: E402
from ocs_answerer.normalize import normalize_question, parse_options  # noqa: E402
from ocs_answerer.server import create_server  # noqa: E402
from ocs_answerer.solver import SearchRequest, Solver  # noqa: E402


class MockLLM(BaseHTTPRequestHandler):
    """根据题干关键词返回预设答案，用来模拟真实大模型。"""

    protocol_version = "HTTP/1.1"
    calls: list[dict] = []

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        MockLLM.calls.append(payload)
        # 只看最后一条用户消息里的「题目：」段落，避免被 prompt 里的说明文字误导
        content = payload["messages"][-1]["content"]
        if isinstance(content, list):
            text = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        else:
            text = str(content)
        matched = re.search(r"题目：\n(.*?)(?:\n\n|\Z)", text, re.S)
        question = matched.group(1) if matched else text

        if "多选" in question:
            answer = {"answer": ["A", "C"], "answers": [], "confidence": 0.9, "need_search": False, "reason": "多选"}
        elif "判断" in question:
            answer = {"answer": ["A"], "answers": [], "confidence": 0.9, "need_search": False, "reason": "判断"}
        elif "填空" in question:
            answer = {"answer": [], "answers": ["北京", "上海"], "confidence": 0.9, "need_search": False}
        elif "无选项" in question:
            answer = {"answer": [], "answers": ["答案文本"], "confidence": 0.9, "need_search": False}
        elif "不在选项" in question:
            answer = {"answer": ["Z"], "answers": [], "confidence": 0.9, "need_search": False}
        else:
            answer = {"answer": ["B"], "answers": [], "confidence": 0.95, "need_search": False, "reason": "世界和平"}

        body = json.dumps(
            {
                "id": "mock",
                "model": payload.get("model", "mock"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_mock() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockLLM)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    start_mock.server = server  # type: ignore[attr-defined]
    return server.server_address[1]


CONFIG_TEMPLATE = """
[server]
host = "127.0.0.1"
port = {port}
public_url = "http://127.0.0.1:{port}"

[llm]
provider = "mock"
timeout = 10

[llm.providers.mock]
base_url = "http://127.0.0.1:{llm_port}/v1"
api_key = "test"
model = "mock-model"

[search]
enabled = false
providers = []

[bank]
dir = "{bank_dir}"
file = "default.md"
"""


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm_port = start_mock()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bank_dir = cls.root / "bank"
        cls.bank_dir.mkdir()
        cls.config_path = cls.root / "config.toml"
        cls.config_path.write_text(
            CONFIG_TEMPLATE.format(port=0, llm_port=cls.llm_port, bank_dir=str(cls.bank_dir).replace("\\", "/")),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls):
        start_mock.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        for path in self.bank_dir.glob("*.md"):
            path.unlink()
        MockLLM.calls = []
        self.cfg = Config.load(self.config_path)
        self.bank = Bank(self.cfg.bank_dir, self.cfg.bank_file)
        self.bank.load()
        self.solver = Solver(self.cfg, self.bank)

    def ask(self, title, options="", qtype="", **kwargs):
        request = SearchRequest(title=title, options=options, qtype=qtype)
        return self.solver.solve(request, **kwargs)

    # -- 用例 ---------------------------------------------------------------

    def test_single_choice(self):
        answer = self.ask("中国梦是什么？", "A. 实现中华民族伟大复兴\nB. 世界和平\nC. 人民幸福", "single")
        self.assertEqual(answer.code, 1, answer.msg)
        self.assertEqual(answer.answer, "B. 世界和平")
        self.assertEqual(answer.labels, "B")
        self.assertFalse(answer.cached)

    def test_cache_hit_and_markdown(self):
        options = "A. 甲\nB. 乙\nC. 丙"
        first = self.ask("测试题目一", options, "single")
        self.assertEqual(first.code, 1)
        self.assertEqual(first.source, "llm")

        second = self.ask("【单选题】测试题目一", options, "single")
        self.assertTrue(second.cached, "加题型标记后仍应命中缓存")
        self.assertEqual(second.answer, first.answer)

        text = (self.bank_dir / "default.md").read_text(encoding="utf-8")
        self.assertIn("## 测试题目一", text)
        self.assertIn("- 答案: B", text)

        # 重新加载后依然命中
        bank2 = Bank(self.cfg.bank_dir, self.cfg.bank_file)
        self.assertEqual(bank2.load(), 1)
        solver2 = Solver(self.cfg, bank2)
        third = solver2.solve(SearchRequest(title="测试题目一", options=options, qtype="single"))
        self.assertTrue(third.cached)

    def test_multiple_choice(self):
        answer = self.ask("这是一道多选", "A. 甲\nB. 乙\nC. 丙\nD. 丁", "multiple")
        self.assertEqual(answer.code, 1, answer.msg)
        self.assertEqual(answer.labels, "A#C")
        self.assertEqual(answer.answer, "A. 甲#C. 丙")

    def test_judgement(self):
        answer = self.ask("这是判断题", "A. 正确\nB. 错误", "judgement")
        self.assertEqual(answer.code, 1, answer.msg)
        self.assertEqual(answer.answer, "A. 正确")

    def test_judgement_word_answer(self):
        """模型直接回「对/错」而不是字母时也要能匹配。"""
        options = parse_options("A. 对\nB. 错")
        self.assertEqual(self.solver._resolve({"answer": ["正确"]}, options, "judgement")[0], "A")
        self.assertEqual(self.solver._resolve({"answer": ["错误"]}, options, "judgement")[0], "B")
        self.assertEqual(self.solver._resolve({"answer": ["不对"]}, options, "judgement")[0], "B")
        options2 = parse_options("A. 错误\nB. 正确")
        self.assertEqual(self.solver._resolve({"answer": ["√"]}, options2, "judgement")[0], "B")

    def test_completion(self):
        answer = self.ask("这是填空题", "", "completion")
        self.assertEqual(answer.code, 1, answer.msg)
        self.assertEqual(answer.answer, "北京#上海")

    def test_no_options(self):
        answer = self.ask("无选项的简答题", "", "")
        self.assertEqual(answer.code, 1, answer.msg)
        self.assertEqual(answer.answer, "答案文本")

    def test_option_change_invalidates_cache(self):
        first = self.ask("相同的题干", "A. 甲\nB. 乙", "single")
        self.assertEqual(first.code, 1)
        # 同一题干但选项完全不同，应重新问模型而不是复用
        second = self.ask("相同的题干", "A. 天\nB. 地\nC. 人\nD. 和", "single")
        self.assertFalse(second.cached)
        self.assertEqual(len(MockLLM.calls), 2)  # 每题各调用一次

    def test_fuzzy_match(self):
        first = self.ask("计算机网络体系结构中，OSI参考模型共有几层？", "A. 4\nB. 5\nC. 6\nD. 7", "single")
        self.assertEqual(first.code, 1)
        near = self.ask("计算机网络体系结构中 OSI 参考模型共有几层", "A. 4\nB. 5\nC. 6\nD. 7", "single")
        self.assertTrue(near.cached)

    def test_invalid_letter_retry(self):
        before = len(MockLLM.calls)
        answer = self.ask("不在选项里的题", "A. 甲\nB. 乙", "single")
        self.assertGreaterEqual(len(MockLLM.calls) - before, 2, "答案不在选项内时应重试一次")
        self.assertEqual(answer.code, 0)

    def test_manual_markdown(self):
        """手工写的题库文件也要能直接读。"""
        path = self.bank_dir / "手动.md"
        path.write_text(
            "# 我的题库\n\n"
            "## 【单选题】1+1=？\n"
            "- 答案：B\n"
            "- 选项：\n"
            "  - A. 1\n"
            "  - B. 2\n"
            "  - C. 3\n\n"
            "## 地球是圆的\n"
            "- 类型: judgement\n"
            "- 选项:\n"
            "  - A. 对\n"
            "  - B. 错\n"
            "- 答案: A\n",
            encoding="utf-8",
        )
        bank = Bank(self.cfg.bank_dir, self.cfg.bank_file)
        self.assertEqual(bank.load(), 2)

        entry, score = bank.lookup("1+1=?", ["A. 1", "B. 2", "C. 3"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry.answer_labels, "B")
        self.assertEqual(entry.answer_text, "B. 2")

        entry2, _ = bank.lookup("地球是圆的", ["A. 对", "B. 错"])
        self.assertIsNotNone(entry2)
        self.assertEqual(entry2.answer_text, "A. 对")

    def test_http_server(self):
        server = create_server(self.cfg, self.bank, self.solver, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        try:
            payload = json.dumps(
                {"title": "服务测试题", "type": "single", "options": "A. 甲\nB. 乙\nC. 丙"}
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/search",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read())
            self.assertEqual(data["code"], 1, data)
            self.assertEqual(data["answer"], "B. 乙")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ocs-config", timeout=10) as response:
                configs = json.loads(response.read())
            self.assertEqual(configs[0]["method"], "post")
            self.assertIn("handler", configs[0])
            self.assertEqual(configs[0]["type"], "GM_xmlhttpRequest")
        finally:
            server.shutdown()
            server.server_close()


class NormalizeTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_question("【单选题】1. 中国梦是什么？"), "中国梦是什么")
        self.assertEqual(normalize_question("第 3 题  关于 TCP/IP 的说法"), "关于tcp/ip的说法")
        self.assertEqual(
            normalize_question("<p>下列哪个是<strong>正确</strong>的？</p>"), "下列哪个是正确的"
        )

    def test_parse_options(self):
        options = parse_options("A. 甲\nB、乙\nC) 丙\nD．丁")
        self.assertEqual([o.label for o in options], ["A", "B", "C", "D"])
        self.assertEqual([o.text for o in options], ["甲", "乙", "丙", "丁"])

    def test_parse_inline_options(self):
        options = parse_options("A. 甲 B. 乙 C. 丙")
        self.assertEqual(len(options), 3)

    def test_parse_without_labels(self):
        options = parse_options("对\n错")
        self.assertEqual([o.label for o in options], ["A", "B"])
        self.assertEqual(options[0].raw, "对")

    def test_option_similarity(self):
        self.assertGreater(options_similarity(["A. 甲", "B. 乙"], ["A. 甲", "B. 乙"]), 0.99)
        self.assertLess(options_similarity(["A. 甲", "B. 乙"], ["A. 天", "B. 地"]), 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
