# -*- coding: utf-8 -*-
"""端到端测试：用假的 OpenAI 兼容服务替代真实模型，验证完整链路。"""

from __future__ import annotations

import http.client
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
        user_turns = sum(1 for message in payload["messages"] if message.get("role") == "user")

        if "纯推理" in question:
            # 第一次只吐思考链，被要求「修复」后才给 JSON
            if user_turns <= 1:
                body = self._reply(payload, content="We need answer single choice. Let me think about the options...")
                return self._send(body)
            answer = {"answer": ["B"], "answers": [], "confidence": 0.9, "need_search": False}
            return self._send(self._reply(payload, content=json.dumps(answer, ensure_ascii=False)))
        if "推理链含JSON" in question:
            # content 为空，JSON 藏在 reasoning_content 里
            body = self._reply(
                payload,
                content="",
                reasoning_content='{"answer": ["B"], "answers": [], "confidence": 0.88, "need_search": false}',
            )
            return self._send(body)

        if "多选" in question:
            answer = {"answer": ["A", "C"], "answers": [], "confidence": 0.9, "need_search": False, "reason": "多选"}
        elif "判断" in question:
            answer = {"answer": ["A"], "answers": [], "confidence": 0.9, "need_search": False, "reason": "判断"}
        elif "程序输出" in question:
            answer = {"answer": [], "answers": ["2#3#5#7#"], "confidence": 0.9, "need_search": False}
        elif "填空" in question:
            answer = {"answer": [], "answers": ["北京", "上海"], "confidence": 0.9, "need_search": False}
        elif "无选项" in question:
            answer = {"answer": [], "answers": ["答案文本"], "confidence": 0.9, "need_search": False}
        elif "不在选项" in question:
            answer = {"answer": ["Z"], "answers": [], "confidence": 0.9, "need_search": False}
        else:
            answer = {"answer": ["B"], "answers": [], "confidence": 0.95, "need_search": False, "reason": "世界和平"}

        self._send(self._reply(payload, content=json.dumps(answer, ensure_ascii=False)))

    @staticmethod
    def _reply(payload: dict, *, content: str, reasoning_content: str = "") -> bytes:
        message = {"role": "assistant", "content": content}
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        return json.dumps(
            {
                "id": "mock",
                "model": payload.get("model", "mock"),
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def _send(self, body: bytes) -> None:
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
thinking = "on"
thinking_effort = "low"

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

    def ask(self, title, options="", qtype="", images=None, **kwargs):
        request = SearchRequest(title=title, options=options, qtype=qtype, images=list(images or []))
        return self.solver.solve(request, **kwargs)

    @staticmethod
    def png_data_url(seed: int) -> str:
        import base64
        import struct
        import zlib

        def chunk(tag: bytes, data: bytes) -> bytes:
            body = struct.pack(">I", len(data)) + tag + data
            return body + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        raw = b"".join(b"\x00" + bytes([seed, 0, 0]) * 2 for _ in range(2))
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )
        return "data:image/png;base64," + base64.b64encode(png).decode()

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

    def test_answer_token_forms(self):
        """模型可能给 A#C、AC、A,C、[A,C]，也可能直接给英文选项内容。"""
        multi = parse_options("A. 甲\nB. 乙\nC. 丙\nD. 丁")
        for answer in (["A#C"], ["A,C"], ["AC"], ["A", "C"], ["A、C"]):
            self.assertEqual(
                self.solver._resolve({"answer": answer}, multi, "multiple"),
                ("A#C", "A. 甲#C. 丙"),
                f"答案形态 {answer} 解析错误",
            )

        single = parse_options("void\n\ndouble\nint\nchar")
        self.assertEqual(self.solver._resolve({"answer": ["int"]}, single, "single"), ("C", "int"))
        self.assertEqual(self.solver._resolve({"answer": ["INT"]}, single, "single"), ("C", "int"))
        self.assertEqual(self.solver._resolve({"answer": ["C"]}, single, "single"), ("C", "int"))

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

    def test_type_isolation(self):
        """同一题干、同一选项，多选题不能命中单选的缓存。"""
        options = "A. 甲\nB. 乙\nC. 丙"
        first = self.ask("这道多选和单选共用题干", options, "single")
        self.assertEqual(first.code, 1, first.msg)
        self.assertEqual(first.labels, "A")

        multi = self.ask("这道多选和单选共用题干", options, "multiple")
        self.assertFalse(multi.cached, "多选题必须重新作答，不能复用单选缓存")
        self.assertEqual(multi.labels, "A#C")

        again = self.ask("这道多选和单选共用题干", options, "single")
        self.assertTrue(again.cached)
        self.assertEqual(again.labels, "A")

        text = (self.bank_dir / "default.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## 这道多选和单选共用题干"), 2)
        self.assertIn("- 类型: single", text)
        self.assertIn("- 类型: multiple", text)

    def test_image_only_question_key(self):
        """纯图片题的占位符标题不能变成通用缓存键。"""
        solver = self.solver
        self.assertEqual(solver._prepare_question(SearchRequest(title=" [图片1] ")), "")
        self.assertEqual(solver._prepare_question(SearchRequest(title="[图片1][图片2]")), "")
        self.assertEqual(
            solver._prepare_question(SearchRequest(title="如图[图片1]所示，选择正确答案")),
            "如图[图片1]所示，选择正确答案",
        )

    def test_reasoning_only_then_repair(self):
        """模型把思考链当正文输出时，应自动修复一轮。"""
        answer = self.ask("这道纯推理题", "A. 甲\nB. 乙\nC. 丙", "single")
        self.assertEqual(answer.code, 1, answer.msg)
        self.assertEqual(answer.answer, "B. 乙")
        self.assertGreaterEqual(len(MockLLM.calls), 2, "应触发 JSON 修复轮")

    def test_json_inside_reasoning_content(self):
        """content 为空、JSON 藏在 reasoning_content 里时也要能取到。"""
        answer = self.ask("这道推理链含JSON的题", "A. 甲\nB. 乙\nC. 丙", "single")
        self.assertEqual(answer.code, 1, answer.msg)
        self.assertEqual(answer.answer, "B. 乙")
        self.assertEqual(len(MockLLM.calls), 1, "JSON 就在推理内容里，无需再修复")

    def test_image_options_use_letters(self):
        """选项是图片时回传字母，否则 OCS 侧（选项文本是图片 URL）匹配不上。"""
        single = self.ask("图片选项题", "A. 甲\nB. [图片2]\nC. 丙", "single")
        self.assertEqual(single.answer, "B")
        self.assertEqual(single.labels, "B")

        multi = self.ask("图片选项多选", "A. [图片1]\nB. 乙\nC. [图片3]", "multiple")
        self.assertEqual(multi.answer, "A#C")

    def test_blank_option_lines_are_dropped(self):
        """某个选项末尾带换行时，OCS 拼出来的选项串里会多一个空行，不能当成第 5 个选项。"""
        options = parse_options("void\n\ndouble\nint\nchar")
        self.assertEqual([o.raw for o in options], ["void", "double", "int", "char"])
        self.assertEqual([o.label for o in options], ["A", "B", "C", "D"])
        self.assertEqual(options[2].text, "int")

    def test_empty_option_is_not_image_mode(self):
        """空选项是解析噪音，不能因此把整题当成图片题回传字母。"""
        from ocs_answerer.solver import Solver

        options = parse_options("void\n\ndouble\nint\nchar")
        self.assertFalse(Solver._has_image_options(options))

    def test_stale_entry_with_empty_option_ignored(self):
        """选项有空项的旧条目会答错（字母错位），必须跳过。"""
        path = self.bank_dir / "broken.md"
        path.write_text(
            "## 在C语言程序中，若对函数类型未加显式说明，则函数的隐含类型为\n\n"
            "- 类型: single\n"
            "- 选项:\n"
            "  - void\n"
            "  - \n"
            "  - double\n"
            "  - int\n"
            "  - char\n"
            "- 答案: D\n",
            encoding="utf-8",
        )
        bank = Bank(self.cfg.bank_dir, self.cfg.bank_file)
        bank.load()
        entry, _ = bank.lookup(
            "在C语言程序中，若对函数类型未加显式说明，则函数的隐含类型为",
            ["void", "double", "int", "char"],
            qtype="single",
        )
        self.assertIsNone(entry, "有空选项的旧条目不能命中")
        stale = bank.stale_entries()
        self.assertEqual(len(stale), 1)
        self.assertTrue(stale[0].options[1] == "")

    def test_image_questions_do_not_collide(self):
        """题干是「方程 [图片1] 确定的隐函数 [图片2]」这类套话时，图片不同就是不同的题。"""
        title = "方程 [图片1] 确定的隐函数 [图片2]"
        options = "A. [图片3]\nB. [图片4]\nC. [图片5]\nD. [图片6]"

        first = self.ask(title, options, "single", images=[self.png_data_url(1)])
        self.assertEqual(first.code, 1, first.msg)
        self.assertFalse(first.cached)

        other = self.ask(title, options, "single", images=[self.png_data_url(2)])
        self.assertFalse(other.cached, "换了图片必须当成新题")
        self.assertEqual(len(MockLLM.calls), 2)

        again = self.ask(title, options, "single", images=[self.png_data_url(1)])
        self.assertTrue(again.cached, "同一组图片应命中")

        text = (self.bank_dir / "default.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("- 图片指纹:"), 2)

    def test_image_only_question_cached(self):
        options = "A. [图片2]\nB. [图片3]"
        question = " [图片1] "
        first = self.ask(question, options, "single", images=[self.png_data_url(7)])
        self.assertFalse(first.cached)
        second = self.ask(question, options, "single", images=[self.png_data_url(7)])
        self.assertTrue(second.cached)
        self.assertEqual(first.answer, second.answer)

    def test_legacy_image_entry_ignored(self):
        """没有图片指纹的旧图片题条目不能被命中（否则会串题）。"""
        path = self.bank_dir / "legacy.md"
        path.write_text(
            "## 方程 [图片1] 确定的隐函数 [图片2]\n\n"
            "- 类型: single\n"
            "- 选项:\n"
            "  - [图片3]\n"
            "  - [图片4]\n"
            "- 答案: A\n",
            encoding="utf-8",
        )
        bank = Bank(self.cfg.bank_dir, self.cfg.bank_file)
        bank.load()
        entry, _ = bank.lookup(
            "方程 [图片1] 确定的隐函数 [图片2]", ["[图片3]", "[图片4]"], qtype="single"
        )
        self.assertIsNone(entry, "无指纹的旧条目应被忽略")

        # 补上指纹后就能命中
        path.write_text(
            "## 方程 [图片1] 确定的隐函数 [图片2]\n\n"
            "- 类型: single\n"
            "- 选项:\n"
            "  - [图片3]\n"
            "  - [图片4]\n"
            "- 答案: A\n"
            "- 图片指纹: abc123abc123abc1\n",
            encoding="utf-8",
        )
        bank.load()
        entry2, score = bank.lookup(
            "方程 [图片1] 确定的隐函数 [图片2]",
            ["[图片3]", "[图片4]"],
            qtype="single",
            image_hash="abc123abc123abc1deadbeef",
        )
        self.assertIsNotNone(entry2)
        self.assertEqual(entry2.answer_text, "[图片3]")

    def test_thinking_params_sent(self):
        """思考模式参数要按配置发出去，并且思考模式下不再发 temperature。"""
        self.ask("思考参数测试", "A. 甲\nB. 乙", "single")
        payload = MockLLM.calls[-1]
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("temperature", payload, "思考模式不支持 temperature，不应发送")

    def test_ocs_config_uses_request_host(self):
        """题库配置里的地址要跟着浏览器访问的地址走，换设备访问不用手改 IP。"""
        server = create_server(self.cfg, self.bank, self.solver, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

        def fetch(path: str, host: str) -> str:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", path, headers={"Host": host})
            body = conn.getresponse().read().decode("utf-8")
            conn.close()
            return body

        try:
            lan = f"192.168.1.50:{port}"
            data = json.loads(fetch("/api/ocs-config", lan))
            self.assertEqual(data[0]["url"], f"http://{lan}/api/search")
            self.assertEqual(data[0]["homepage"], f"http://{lan}")

            html = fetch("/", lan)
            self.assertIn(lan, html)
            self.assertNotIn(f"127.0.0.1:{port}", html, "页面里不应再写死回环地址")

            # Host 头被塞了奇怪内容时退回配置里的地址，不原样回显
            weird = json.loads(fetch("/api/ocs-config", 'evil.example"><script>'))
            self.assertNotIn("evil.example", weird[0]["url"])
        finally:
            server.shutdown()
            server.server_close()

    def test_completion_answer_with_separator(self):
        """填空答案本身含 # 时改发 JSON 数组，OCS 才不会把它切成空格分隔。"""
        first = self.ask("这道程序输出题", "", "completion")
        self.assertEqual(first.code, 1, first.msg)
        self.assertEqual(first.answer, '["2#3#5#7#"]')
        self.assertEqual(first.labels, "2#3#5#7#")

        # 重启后仍要命中，而且返回同样的形式
        bank2 = Bank(self.cfg.bank_dir, self.cfg.bank_file)
        bank2.load()
        again = Solver(self.cfg, bank2).solve(
            SearchRequest(title="这道程序输出题", options="", qtype="completion")
        )
        self.assertTrue(again.cached)
        self.assertEqual(again.answer, '["2#3#5#7#"]')

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
            data = configs[0]["data"]
            # 必须引用图片题优化的字段，否则 OCS 会提示「暂不支持图片、公式」
            self.assertIn("suggestion_title", data["title"]["handler"])
            self.assertIn("suggestion_options", data["options"]["handler"])
            self.assertIn("env.images", data["images"]["handler"])
        finally:
            server.shutdown()
            server.server_close()


class ThinkingConfigTest(unittest.TestCase):
    """思考模式开关与强度（DeepSeek 文档 https://api-docs.deepseek.com/zh-cn/guides/thinking_mode）。"""

    @staticmethod
    def provider(**kwargs):
        from ocs_answerer.config import Provider

        base = dict(name="x", base_url="https://api.deepseek.com", api_key="k", model="deepseek-flash")
        base.update(kwargs)
        return Provider(**base)

    def test_auto_deepseek_defaults_to_low(self):
        from ocs_answerer.llm import LLMClient

        body = LLMClient(self.provider(thinking="auto", thinking_effort="low")).thinking_payload()
        self.assertEqual(body, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})

    def test_auto_leaves_other_providers_alone(self):
        from ocs_answerer.llm import LLMClient

        body = LLMClient(self.provider(base_url="https://api.openai.com/v1")).thinking_payload()
        self.assertEqual(body, {}, "非 DeepSeek 服务商不能收到 thinking 字段")

    def test_force_on_for_relay(self):
        from ocs_answerer.llm import LLMClient

        body = LLMClient(
            self.provider(base_url="https://relay.example.com/v1", thinking="on", thinking_effort="max")
        ).thinking_payload()
        self.assertEqual(body, {"thinking": {"type": "enabled"}, "reasoning_effort": "max"})

    def test_off(self):
        from ocs_answerer.llm import LLMClient

        body = LLMClient(self.provider(thinking="off", thinking_effort="low")).thinking_payload()
        self.assertEqual(body, {"thinking": {"type": "disabled"}})

    def test_ping_disables_thinking(self):
        from ocs_answerer.llm import LLMClient

        body = LLMClient(self.provider(thinking="auto")).thinking_payload(False)
        self.assertEqual(body, {"thinking": {"type": "disabled"}})

    def test_bool_and_alias_values(self):
        from ocs_answerer.config import _thinking_mode

        self.assertEqual(_thinking_mode(True), "on")
        self.assertEqual(_thinking_mode(False), "off")
        self.assertEqual(_thinking_mode("ENABLED"), "on")
        self.assertEqual(_thinking_mode("disabled"), "off")
        self.assertEqual(_thinking_mode("", "auto"), "auto")
        self.assertEqual(_thinking_mode(None), "auto")


class JsonExtractTest(unittest.TestCase):
    """模型输出千奇百怪，JSON 提取要尽量宽容。"""

    def test_variants(self):
        from ocs_answerer.llm import extract_json

        self.assertEqual(extract_json('{"answer":["A"]}')["answer"], ["A"])
        self.assertEqual(extract_json('```json\n{"answer":["B"]}\n```')["answer"], ["B"])
        self.assertEqual(extract_json('说明文字\n<json>{"answer":["C"]}</json>')["answer"], ["C"])
        self.assertEqual(extract_json("我觉得是 {'answer': ['D'], 'confidence': 0.8}")["answer"], ["D"])
        self.assertEqual(extract_json('{"answer": ["A"],}')["answer"], ["A"])
        self.assertEqual(
            extract_json('先分析一下。\n最终结果：\n{"answer": ["B"], "answers": []}\n以上。')["answer"], ["B"]
        )

    def test_reject_plain_text(self):
        from ocs_answerer.llm import LLMError, extract_json

        with self.assertRaises(LLMError):
            extract_json("We need answer single choice. Need parse images.")


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
