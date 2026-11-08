# -*- coding: utf-8 -*-
"""命令行入口。

    python main.py                 启动服务
    python main.py ask "题目"       本地测一道题
    python main.py config          打印 OCS 题库配置
    python main.py check           自检（配置、模型连通性、题库目录）
    python main.py bank            查看题库
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ocs_answerer.bank import Bank
from ocs_answerer.config import Config, ConfigError
from ocs_answerer.log import setup_logging
from ocs_answerer.server import build_ocs_config, serve_forever
from ocs_answerer.solver import SearchRequest, Solver


def _bootstrap(args) -> tuple[Config, Bank, Solver]:
    cfg = Config.load(args.config)
    log_file = str(cfg.get("server.log_file", "") or "")
    if log_file and not Path(log_file).is_absolute():
        log_file = str(cfg.base_dir / log_file)
    setup_logging(str(cfg.get("server.log_level", "INFO")), log_file or None)
    bank = Bank(
        cfg.bank_dir,
        cfg.bank_file,
        fuzzy=bool(cfg.get("bank.fuzzy", True)),
        fuzzy_threshold=float(cfg.get("bank.fuzzy_threshold", 0.92)),
        verify_options=bool(cfg.get("bank.verify_options", True)),
        option_threshold=float(cfg.get("bank.option_threshold", 0.72)),
        update_existing=bool(cfg.get("bank.update_existing", True)),
    )
    count = bank.load()
    solver = Solver(cfg, bank)
    return cfg, bank, solver


def cmd_serve(args) -> int:
    cfg, bank, solver = _bootstrap(args)
    if args.host:
        cfg.data["server"]["host"] = args.host
    if args.port:
        cfg.data["server"]["port"] = args.port
    problems = cfg.check_ready()
    if problems:
        print("配置有问题：")
        for item in problems:
            print(f"  - {item}")
        return 1
    serve_forever(cfg, bank, solver)
    return 0


def cmd_ask(args) -> int:
    cfg, bank, solver = _bootstrap(args)
    request = SearchRequest(
        title=args.title,
        options=args.options or "",
        qtype=args.type or "",
        images=[img for img in (args.image or [])],
    )
    answer = solver.solve(request, use_cache=not args.no_cache)
    data = answer.to_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if answer.code == 1 else 1
    print("-" * 56)
    print(f"题目：{request.title[:200] or '（图片题）'}")
    if request.options:
        print(f"选项：\n{request.options}")
    print("-" * 56)
    if answer.code != 1:
        print(f"失败：{answer.msg or '未知原因'}")
        return 1
    print(f"答案：{answer.answer}")
    print(f"字母：{answer.labels}")
    print(f"来源：{answer.source}{'（题库缓存）' if answer.cached else ''}"
          f"{'（已联网）' if answer.search_used else ''}")
    print(f"耗时：{answer.elapsed_ms / 1000:.1f}s")
    if answer.explain:
        print(f"理由：{answer.explain}")
    return 0


def cmd_config(args) -> int:
    cfg = Config.load(args.config)
    data = build_ocs_config(cfg.public_url)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已写入 {args.output}")
    print(text)
    return 0


def cmd_check(args) -> int:
    cfg, bank, solver = _bootstrap(args)
    ok = True
    print("=" * 56)
    print(f"配置文件：{cfg.path}")
    problems = cfg.check_ready()
    if problems:
        ok = False
        for item in problems:
            print(f"[配置] [!!] {item}")
    else:
        provider = cfg.provider()
        print(f"[配置] [OK] 服务商 {provider.name}，模型 {provider.model}，密钥 {'已填写' if provider.api_key else '未填写'}")
        vision = cfg.vision_provider()
        if vision:
            print(f"[配置] [OK] 识图模型 {vision.name} / {vision.model}")
        else:
            print("[配置] [--] 未配置识图模型，图片题会退化为纯文本作答")

    print(f"[题库] [OK] {bank.stats()['entries']} 条，目录 {cfg.bank_dir}")
    try:
        cfg.bank_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.bank_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("[题库] [OK] 目录可写")
    except OSError as exc:
        ok = False
        print(f"[题库] [!!] 目录不可写：{exc}")

    print(f"[搜索] {'[OK] 已开启：' + '、'.join(solver.search.providers) if solver.search.available() else '[--] 未开启'}")

    try:
        reply = solver.llm.ping(timeout=30)
        print(f"[模型] [OK] 连通，回复：{reply[:30]}")
    except Exception as exc:
        ok = False
        print(f"[模型] [!!] 调用失败：{exc}")

    print(f"[服务] 监听 {cfg.public_url}")
    print("=" * 56)
    return 0 if ok else 1


def cmd_bank(args) -> int:
    cfg, bank, _ = _bootstrap(args)
    entries = bank.search(args.query or "", limit=args.limit)
    print(f"题库共 {bank.stats()['entries']} 条，显示 {len(entries)} 条：")
    for entry in entries:
        print("-" * 56)
        print(f"题目：{entry.question[:120]}")
        print(f"答案：{entry.answer_labels}  →  {entry.answer_text[:100]}")
        print(f"来源：{entry.source}  更新：{entry.updated}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocs-answer-llm",
        description="本地运行的 OCS AI 题库（大模型 + 识图 + 联网 + Markdown 缓存）",
    )
    parser.add_argument("-c", "--config", default="config.toml", help="配置文件路径（默认 config.toml）")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP 服务（默认命令）")
    serve.add_argument("--host", default="", help="监听地址")
    serve.add_argument("--port", type=int, default=0, help="监听端口")
    serve.set_defaults(func=cmd_serve)

    ask = sub.add_parser("ask", help="命令行直接问一道题")
    ask.add_argument("title", help="题干")
    ask.add_argument("-o", "--options", default="", help="选项，多行用 \\n 分隔")
    ask.add_argument("-t", "--type", default="", help="题目类型：single/multiple/judgement/completion")
    ask.add_argument("-i", "--image", action="append", default=[], help="图片地址或本地路径，可重复")
    ask.add_argument("--no-cache", action="store_true", help="忽略题库缓存，强制问模型")
    ask.add_argument("--json", action="store_true", help="以 JSON 输出")
    ask.set_defaults(func=cmd_ask)

    config = sub.add_parser("config", help="输出 OCS 题库配置 JSON")
    config.add_argument("-O", "--output", default="", help="同时写入文件")
    config.set_defaults(func=cmd_config)

    check = sub.add_parser("check", help="自检配置与连通性")
    check.set_defaults(func=cmd_check)

    bank = sub.add_parser("bank", help="查看题库")
    bank.add_argument("-q", "--query", default="", help="按关键词过滤")
    bank.add_argument("-n", "--limit", type=int, default=20, help="显示条数")
    bank.set_defaults(func=cmd_bank)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 某些环境不支持
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # 直接 `python main.py` 时按 serve 处理，同时保留 -c 等全局参数
        args.command = "serve"
        args.func = cmd_serve
        args.host = ""
        args.port = 0
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
