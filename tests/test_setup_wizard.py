# -*- coding: utf-8 -*-
"""首次运行向导的测试：TOML 行级编辑 + 交互流程。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocs_answerer.config import Config, ConfigError
from ocs_answerer.setup_wizard import (
    configured_api_key,
    get_toml_value,
    run_wizard,
    set_toml_value,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "config.example.toml"


def scripted(answers: list[str]):
    """返回一个按顺序回答的 ask，并记录问过的提示。"""
    queue = list(answers)
    prompts: list[str] = []

    def ask(prompt: str, *, secret: bool = False) -> str:
        prompts.append(prompt)
        if not queue:
            raise AssertionError(f"没有更多预设回答了，卡在：{prompt}")
        return queue.pop(0)

    ask.prompts = prompts  # type: ignore[attr-defined]
    return ask


def _close_log_file_handlers() -> None:
    """关掉日志文件句柄，否则 Windows 上临时目录删不掉。"""
    import logging

    logger = logging.getLogger("ocs")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()


class TomlEditTest(unittest.TestCase):
    def test_replace_existing_key(self):
        text = '[llm]\nprovider = "deepseek"   # 注释保留\n'
        out = set_toml_value(text, "llm", "provider", '"qwen"')
        self.assertIn('provider = "qwen"   # 注释保留', out)

    def test_insert_missing_key_into_section(self):
        text = '[llm]\nprovider = "deepseek"\n\n[vision]\nprovider = ""\n'
        out = set_toml_value(text, "llm", "thinking", '"on"')
        self.assertLess(out.index('thinking = "on"'), out.index("[vision]"))
        self.assertEqual(get_toml_value(out, "llm", "thinking"), '"on"')

    def test_append_missing_section(self):
        out = set_toml_value("[server]\nport = 8000\n", "llm.providers.custom", "model", '"m"')
        self.assertIn("[llm.providers.custom]", out)
        self.assertEqual(get_toml_value(out, "llm.providers.custom", "model"), '"m"')

    def test_configured_api_key(self):
        text = '[llm.providers.qwen]\napi_key = "sk-test"\n'
        self.assertEqual(configured_api_key(text, "qwen"), "sk-test")
        self.assertEqual(configured_api_key(text, "deepseek"), "")
        self.assertEqual(configured_api_key("not toml = [", "qwen"), "")


class WizardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "config.toml"
        self.output: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_wizard(self, answers: list[str], **kwargs):
        ask = scripted(answers)
        kwargs.setdefault("template_path", TEMPLATE)
        ready = run_wizard(
            self.path,
            ask=ask,
            echo=self.output.append,
            **kwargs,
        )
        return ready, ask

    def test_defaults_when_pressing_enter(self):
        ready, _ = self.run_wizard(["", "", "sk-abcdef123456"], verify=False)
        self.assertTrue(ready)

        cfg = Config.load(self.path)
        provider = cfg.provider()
        self.assertEqual(cfg.get("llm.provider"), "deepseek")
        self.assertEqual(provider.model, "deepseek-flash")
        self.assertEqual(provider.api_key, "sk-abcdef123456")

        text = self.path.read_text(encoding="utf-8")
        self.assertIn("#  OCS 本地 AI 题库 · 配置文件", text, "模板注释应保留")
        self.assertIn('provider = "deepseek"', text)

    def test_choose_other_provider_and_model(self):
        ready, _ = self.run_wizard(["2", "qwen-max", "sk-qwen-key"], verify=False)
        self.assertTrue(ready)

        provider = Config.load(self.path).provider()
        self.assertEqual(provider.name, "qwen")
        self.assertEqual(provider.model, "qwen-max")
        self.assertEqual(provider.api_key, "sk-qwen-key")
        self.assertEqual(provider.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")

    def test_invalid_choice_reprompts(self):
        ready, ask = self.run_wizard(["99", "0", "1", "", "sk-x"], verify=False)
        self.assertTrue(ready)
        self.assertEqual(sum("服务商序号" in p for p in ask.prompts), 3)
        self.assertEqual(Config.load(self.path).get("llm.provider"), "deepseek")

    def test_skip_key_warns_and_returns_false(self):
        ready, _ = self.run_wizard(["1", "", ""], verify=False)
        self.assertFalse(ready)
        self.assertTrue(self.path.exists(), "即使没填密钥也要把配置写出来")
        self.assertTrue(any("没有填写 API Key" in line for line in self.output))
        self.assertEqual(Config.load(self.path).provider().api_key, "")

    def test_ollama_needs_no_key(self):
        ready, ask = self.run_wizard(["7", ""], verify=False)
        self.assertTrue(ready)
        self.assertFalse(any("API Key" in prompt for prompt in ask.prompts))
        provider = Config.load(self.path).provider()
        self.assertEqual(provider.name, "ollama")
        self.assertEqual(provider.api_key, "ollama")

    def test_custom_provider(self):
        ready, _ = self.run_wizard(
            ["8", "https://api.example.com/v1", "my-model", "sk-custom"], verify=False
        )
        self.assertTrue(ready)
        provider = Config.load(self.path).provider()
        self.assertEqual(provider.name, "custom")
        self.assertEqual(provider.base_url, "https://api.example.com/v1")
        self.assertEqual(provider.model, "my-model")
        self.assertEqual(provider.api_key, "sk-custom")

    def test_retry_key_after_failed_verify(self):
        calls: list[Path] = []

        def verifier(path: Path, echo) -> bool:
            calls.append(path)
            if len(calls) == 1:
                echo("      [!!] 401 Unauthorized")
                return False
            return True

        ready, _ = self.run_wizard(
            ["1", "", "bad-key", "y", "good-key"], verify=verifier
        )
        self.assertTrue(ready)
        self.assertEqual(len(calls), 2)
        self.assertEqual(Config.load(self.path).provider().api_key, "good-key")

    def test_give_up_after_three_failures(self):
        ready, _ = self.run_wizard(
            ["1", "", "k1", "y", "k2", "y", "k3"], verify=lambda path, echo: False
        )
        self.assertTrue(ready, "配置已写入，向导不应报错退出")
        self.assertTrue(any("多次测试未通过" in line for line in self.output))

    def test_reply_no_stops_retrying(self):
        ready, ask = self.run_wizard(["1", "", "k1", "n"], verify=lambda path, echo: False)
        self.assertTrue(ready)
        self.assertTrue(any("已跳过" in line for line in self.output))
        self.assertEqual(len(ask.prompts), 4, "回答 n 之后不应再问密钥")

    def test_force_keeps_unrelated_settings(self):
        self.path.write_text(
            "[server]\nport = 8123\n\n[llm]\nprovider = \"zhipu\"\n\n"
            "[llm.providers.zhipu]\nbase_url = \"https://custom.example/v4\"\n"
            "api_key = \"old-key\"\nmodel = \"glm-4-flash\"\n",
            encoding="utf-8",
        )
        ready, _ = self.run_wizard(["3", "", "new-key"], verify=False, force=True)
        self.assertTrue(ready)

        cfg = Config.load(self.path)
        self.assertEqual(cfg.get("server.port"), 8123, "其它节不能被改动")
        provider = cfg.provider()
        self.assertEqual(provider.name, "zhipu")
        self.assertEqual(provider.base_url, "https://custom.example/v4", "不应覆盖用户改过的 base_url")
        self.assertEqual(provider.api_key, "new-key")

    def test_non_interactive_raises(self):
        with mock.patch("ocs_answerer.setup_wizard._stdin_is_console", return_value=False):
            with self.assertRaises(ConfigError):
                run_wizard(self.path, template_path=TEMPLATE, echo=self.output.append)

    def test_force_defaults_to_current_provider(self):
        self.path.write_text(
            '[llm]\nprovider = "zhipu"\n\n[llm.providers.zhipu]\n'
            'base_url = "https://open.bigmodel.cn/api/paas/v4"\napi_key = ""\n'
            'model = "glm-4-flash"\n',
            encoding="utf-8",
        )
        ready, ask = self.run_wizard(["", "", "sk-new"], verify=False, force=True)
        self.assertTrue(ready)
        self.assertIn("[3]", ask.prompts[0], "默认选中配置里现有的服务商")
        self.assertEqual(Config.load(self.path).provider().name, "zhipu")

    def test_template_copied_before_prompts(self):
        """向导中途被 Ctrl+C 打断，config.toml 也已经存在（就是模板副本）。"""

        def boom(prompt: str, *, secret: bool = False) -> str:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run_wizard(self.path, template_path=TEMPLATE, ask=boom, echo=self.output.append)

        self.assertTrue(self.path.exists())
        self.assertEqual(Config.load(self.path).get("llm.provider"), "deepseek")
        self.assertIn("#  OCS 本地 AI 题库 · 配置文件", self.path.read_text(encoding="utf-8"))

    def test_skip_when_exists_without_force(self):
        self.path.write_text("[llm]\nprovider = \"qwen\"\n", encoding="utf-8")
        with mock.patch("ocs_answerer.setup_wizard._stdin_is_console", return_value=False):
            self.assertTrue(run_wizard(self.path, template_path=TEMPLATE))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[llm]\nprovider = \"qwen\"\n")

    def test_fallback_template_when_example_missing(self):
        ready, _ = self.run_wizard(
            ["1", "", "sk-x"], template_path=self.dir / "nope.toml", verify=False
        )
        self.assertTrue(ready)
        cfg = Config.load(self.path)
        self.assertEqual(cfg.get("server.port"), 8000)
        self.assertEqual(cfg.provider().api_key, "sk-x")


class MainIntegrationTest(unittest.TestCase):
    """main.py 在没有 config.toml 时应自动进入向导，然后继续执行原命令。"""

    def test_missing_config_triggers_wizard(self):
        import main as entry

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Windows 上日志文件被占用会导致临时目录删不掉，先关掉文件 handler
        self.addCleanup(_close_log_file_handlers)

        config_path = Path(tmp.name) / "config.toml"
        answers = ["1", "", "sk-integration"]

        def fake_ask(prompt: str, *, secret: bool = False) -> str:
            if not answers:
                raise AssertionError(f"意外多问了一次：{prompt}")
            return answers.pop(0)

        with mock.patch("ocs_answerer.setup_wizard._default_ask", fake_ask), mock.patch(
            "ocs_answerer.setup_wizard._stdin_is_console", return_value=True
        ), mock.patch(
            "ocs_answerer.setup_wizard._default_verify", lambda path, echo: True
        ):
            code = entry.main(["-c", str(config_path), "bank"])

        self.assertEqual(code, 0)
        self.assertTrue(config_path.exists())
        provider = Config.load(config_path).provider()
        self.assertEqual(provider.name, "deepseek")
        self.assertEqual(provider.api_key, "sk-integration")

    def test_serve_runs_after_wizard(self):
        """python main.py（无参数）走完向导后应该接着启动服务。"""
        import main as entry

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(_close_log_file_handlers)
        config_path = Path(tmp.name) / "config.toml"
        answers = ["2", "", "sk-serve"]
        started = []

        def fake_ask(prompt: str, *, secret: bool = False) -> str:
            return answers.pop(0)

        with mock.patch("ocs_answerer.setup_wizard._default_ask", fake_ask), mock.patch(
            "ocs_answerer.setup_wizard._stdin_is_console", return_value=True
        ), mock.patch(
            "ocs_answerer.setup_wizard._default_verify", lambda path, echo: True
        ), mock.patch(
            "main.serve_forever", lambda cfg, bank, solver: started.append(cfg.get("llm.provider"))
        ):
            code = entry.main(["-c", str(config_path)])

        self.assertEqual(code, 0)
        self.assertEqual(started, ["qwen"], "向导选的服务商要传给之后的启动流程")
        self.assertEqual(Config.load(config_path).provider().model, "qwen-plus")

    def test_serve_prompts_when_key_missing(self):
        """配置在、但 key 是空的：启动时直接进向导补填，而不是报错退出。"""
        import main as entry

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(_close_log_file_handlers)
        config_path = Path(tmp.name) / "config.toml"
        config_path.write_text(
            '[llm]\nprovider = "deepseek"\n\n[llm.providers.deepseek]\n'
            'base_url = "https://api.deepseek.com/"\napi_key = ""\n'
            'model = "deepseek-flash"\n',
            encoding="utf-8",
        )
        started = []
        answers = ["", "", "sk-filled"]

        with mock.patch(
            "ocs_answerer.setup_wizard._default_ask", lambda prompt, secret=False: answers.pop(0)
        ), mock.patch(
            "ocs_answerer.setup_wizard._stdin_is_console", return_value=True
        ), mock.patch(
            "ocs_answerer.setup_wizard._default_verify", lambda path, echo: True
        ), mock.patch(
            "main.serve_forever", lambda cfg, bank, solver: started.append(cfg.provider().api_key)
        ):
            code = entry.main(["-c", str(config_path)])

        self.assertEqual(code, 0)
        self.assertEqual(started, ["sk-filled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
