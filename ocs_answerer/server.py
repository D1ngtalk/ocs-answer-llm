# -*- coding: utf-8 -*-
"""本地 HTTP 服务：对外暴露 OCS 题库协议需要的接口。"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .bank import Bank
from .config import Config, ConfigError
from .solver import Solver, SearchRequest

log = logging.getLogger("ocs.server")

IMAGE_HANDLER_JS = """return (env) => {
  // OCS 5.x「图片题优化」会直接把题目图片转成 base64 传进来（env.images）；
  // 老版本没有这个字段，则退回 DOM 抓取图片地址，由本地服务下载。
  if (env.images && env.images.length) return env.images;
  const selectors = ['.question img', '.subject img', '.topic img', '.issue img', '.stem img', '.que img', '.title img', '.exam-question img', '.q-body img', '.question-content img'];
  const urls = [];
  for (const sel of selectors) {
    for (const img of document.querySelectorAll(sel)) {
      if (!img.src) continue;
      if (img.naturalWidth && img.naturalWidth < 100) continue;
      if (!urls.includes(img.src)) urls.push(img.src);
    }
  }
  return urls.slice(0, 3);
}"""

# OCS 5.x 的图片题优化要求 data 中必须实际引用 ${images} / ${suggestion_title} /
# ${suggestion_options}（或 handler 里读取 env.images 等），否则会提示「暂不支持图片」。
# 这两个 handler 同时兼顾了老版本：没有 suggestion_* 字段时回退到原始 title / options。
TITLE_FIELD_JS = "return (env) => (env.suggestion_title || env.title || '')"
OPTIONS_FIELD_JS = "return (env) => (env.suggestion_options || env.options || '')"

RESPONSE_HANDLER_JS = (
    "return (res) => (res && res.code === 1 && res.answer) "
    "? [res.question, res.answer] : undefined"
)

# 状态页上「复制配置」按钮的脚本（独立成常量，避免 f-string 里的大括号转义）
COPY_SCRIPT_HTML = """<script>
  (function () {
    var btn = document.getElementById('copyBtn');
    var pre = document.getElementById('cfg');
    if (!btn || !pre) return;
    btn.addEventListener('click', function () {
      var text = pre.textContent;
      function done() {
        btn.textContent = '已复制';
        btn.classList.add('done');
        setTimeout(function () {
          btn.textContent = '复制配置';
          btn.classList.remove('done');
        }, 1800);
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { fallback(); });
      } else {
        fallback();
      }
      function fallback() {
        var range = document.createRange();
        range.selectNodeContents(pre);
        var selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        try { document.execCommand('copy'); } catch (e) {}
        done();
      }
    });
  })();
</script>"""


def build_ocs_config(base_url: str) -> list[dict[str, Any]]:
    """生成可以直接粘贴到 OCS「题库配置」里的 JSON。

    采用 OCS 5.x 的图片题协议（v2）：标题/选项走 suggestion_* 字段，
    图片走 env.images，这样脚本不会提示「当前题库配置暂不支持图片、公式等题目处理」。
    """
    return [
        {
            "name": "本地AI题库",
            "homepage": base_url.rstrip("/"),
            "url": f"{base_url.rstrip('/')}/api/search",
            "method": "post",
            "contentType": "json",
            "type": "GM_xmlhttpRequest",
            "headers": {"Content-Type": "application/json"},
            "data": {
                "title": {"handler": TITLE_FIELD_JS},
                "options": {"handler": OPTIONS_FIELD_JS},
                "type": "${type}",
                "images": {"handler": IMAGE_HANDLER_JS},
            },
            "handler": RESPONSE_HANDLER_JS,
        }
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = f"OCSAnswerLLM/{__version__}"
    protocol_version = "HTTP/1.1"

    # 由 create_server 注入
    cfg: Config
    bank: Bank
    solver: Solver

    # -- 基础工具 -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # 覆盖默认 stderr 输出
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _base_url(self) -> str:
        """用浏览器实际访问的地址生成题库配置。

        从 127.0.0.1 打开就是 127.0.0.1，从局域网 IP 打开就是那个 IP，
        换设备访问时不用再手动改配置里的地址。
        """
        host = (self.headers.get("Host") or "").strip()
        # 只接受 host / host:port / [ipv6]:port，防止 Host 头里塞奇怪的东西
        if not host or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]{1,80}", host):
            return self.cfg.public_url
        scheme = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
        if scheme not in ("http", "https"):
            scheme = "http"
        return f"{scheme}://{host}"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _html(self, html: str, status: int = 200) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _read_params(self) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(self.path)
        params: dict[str, Any] = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            raw = self.rfile.read(length)
            content_type = (self.headers.get("Content-Type") or "").lower()
            text = raw.decode("utf-8", "replace")
            if "application/json" in content_type or text.strip().startswith("{"):
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        params.update(data)
                except json.JSONDecodeError:
                    log.warning("请求体不是合法 JSON：%s", text[:200])
            else:
                params.update(dict(urllib.parse.parse_qsl(text, keep_blank_values=True)))
        return params

    # -- 路由 ---------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path in ("/", "/index.html"):
                self._html(self._status_page())
            elif path in ("/health", "/api/health", "/ping"):
                self._json({"ok": True, "version": __version__, "uptime": int(time.time() - START_TIME)})
            elif path in ("/api/search", "/search", "/api/answer", "/answer"):
                self._handle_search()
            elif path in ("/api/ocs-config", "/ocs-config", "/api/config"):
                self._json(build_ocs_config(self._base_url()))
            elif path in ("/api/stats", "/stats"):
                self._json(self.solver.stats_snapshot())
            elif path in ("/api/reload", "/reload"):
                self._handle_reload()
            elif path == "/api/bank":
                self._handle_bank()
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json({"code": 0, "msg": f"未知路径：{path}"}, 404)
        except ConfigError as exc:
            self._json({"code": 0, "msg": str(exc)}, 500)
        except Exception as exc:  # pragma: no cover - 兜底
            log.exception("处理请求失败：%s %s", method, path)
            self._json({"code": 0, "msg": f"服务内部错误：{exc}"}, 500)
        finally:
            log.debug("%s %s %.0fms", method, path, (time.monotonic() - started) * 1000)

    # -- 业务 ---------------------------------------------------------------

    def _handle_search(self) -> None:
        params = self._read_params()
        request = SearchRequest.from_params(params)
        if not request.title and not request.images:
            self._json({"code": 0, "msg": "缺少 title 参数"})
            return
        if request.qtype not in ("single", "multiple", "judgement", "completion"):
            request.qtype = ""
        answer = self.solver.solve(request)
        self._json(answer.to_dict())

    def _handle_reload(self) -> None:
        try:
            self.cfg.reload()
            self.solver.reload()
        except ConfigError as exc:
            self._json({"code": 0, "msg": str(exc)})
            return
        count = self.bank.load()
        self._json({"code": 1, "msg": "已重新加载配置与题库", "entries": count})

    def _handle_bank(self) -> None:
        params = self._read_params()
        keyword = str(params.get("q") or params.get("keyword") or "")
        limit = int(params.get("limit") or 20)
        entries = self.bank.search(keyword, limit=limit)
        self._json(
            {
                "code": 1,
                "total": self.bank.stats()["entries"],
                "items": [
                    {
                        "question": e.question,
                        "answer": e.answer_labels,
                        "answer_text": e.answer_text,
                        "type": e.qtype,
                        "source": e.source,
                        "updated": e.updated,
                    }
                    for e in entries
                ],
            }
        )

    # -- 状态页 -------------------------------------------------------------

    def _status_page(self) -> str:
        try:
            provider = self.cfg.provider()
            provider_text = f"{provider.name} / {provider.model}"
        except ConfigError:
            provider_text = "未配置"
        stats = self.solver.stats_snapshot()
        bank_entries = stats["bank"]["entries"]
        base_url = self._base_url()
        config_json = json.dumps(build_ocs_config(base_url), ensure_ascii=False, indent=2)
        config_html = (
            config_json.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        search_state = "开启" if self.solver.search.available() else "关闭"
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OCS 本地 AI 题库</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px 20px 60px; background: #f6f7f9; color: #1f2328;
         font: 14px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }}
  .wrap {{ max-width: 880px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; letter-spacing: .3px; }}
  .sub {{ color: #6b7280; margin: 0 0 24px; font-size: 13px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; }}
  .card .k {{ color: #6b7280; font-size: 12px; }}
  .card .v {{ font-size: 18px; font-weight: 600; margin-top: 2px; word-break: break-all; }}
  section {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }}
  h2 {{ font-size: 14px; margin: 0 0 10px; color: #111827; }}
  pre {{ background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px;
         overflow: auto; font-size: 12px; line-height: 1.55; max-height: 380px; margin: 0; }}
  .row {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }}
  .row h2 {{ margin: 0; }}
  button {{ border: 1px solid #d0d7de; background: #fff; color: #1f2328; border-radius: 7px;
            padding: 5px 12px; font-size: 12.5px; cursor: pointer; }}
  button:hover {{ background: #f3f4f6; }}
  button.done {{ border-color: #1a7f37; color: #1a7f37; }}
  p.hint {{ color: #6b7280; font-size: 12.5px; margin: 10px 0 0; }}
  code {{ background: #eef1f4; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  td {{ padding: 4px 0; color: #374151; }}
  td:first-child {{ color: #6b7280; width: 42%; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>OCS 本地 AI 题库</h1>
  <p class="sub">v{__version__} · {base_url} · 服务运行中</p>

  <div class="grid">
    <div class="card"><div class="k">模型</div><div class="v" style="font-size:14px">{provider_text}</div></div>
    <div class="card"><div class="k">题库条目</div><div class="v">{bank_entries}</div></div>
    <div class="card"><div class="k">本次请求 / 缓存命中</div><div class="v">{stats['total']} / {stats['cache_hits']}</div></div>
    <div class="card"><div class="k">联网搜索</div><div class="v" style="font-size:14px">{search_state}</div></div>
  </div>

  <section>
    <div class="row">
      <h2>粘贴到 OCS「题库配置」</h2>
      <button id="copyBtn" type="button">复制配置</button>
    </div>
    <pre id="cfg">{config_html}</pre>
    <p class="hint">已使用 <code>GM_xmlhttpRequest</code> 绕过跨域与混合内容限制；若脚本提示域名未授权，
    请在脚本管理器里把 <code>@connect</code> 设为宽松模式，或使用全域名通用版脚本。</p>
  </section>

  <section>
    <h2>接口</h2>
    <table>
      <tr><td>答题</td><td><code>POST /api/search</code> · 参数 title / type / options / image</td></tr>
      <tr><td>题库配置</td><td><code>GET /api/ocs-config</code></td></tr>
      <tr><td>运行统计</td><td><code>GET /api/stats</code></td></tr>
      <tr><td>重载配置与题库</td><td><code>GET /api/reload</code></td></tr>
      <tr><td>健康检查</td><td><code>GET /health</code></td></tr>
    </table>
  </section>
</div>
{COPY_SCRIPT_HTML}
</body>
</html>"""


START_TIME = time.time()


def create_server(cfg: Config, bank: Bank, solver: Solver, host: str, port: int) -> ThreadingHTTPServer:
    handler_cls = type("BoundHandler", (Handler,), {"cfg": cfg, "bank": bank, "solver": solver})
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    return server


def serve_forever(cfg: Config, bank: Bank, solver: Solver) -> None:
    host = str(cfg.get("server.host", "127.0.0.1"))
    port = int(cfg.get("server.port", 8000))
    server = create_server(cfg, bank, solver, host, port)
    url = cfg.public_url
    log.info("=" * 56)
    log.info("OCS 本地题库已启动：%s", url)
    log.info("题库条目：%s 条", bank.stats()["entries"])
    log.info("状态页：%s/", url)
    log.info("OCS 题库配置：%s/api/ocs-config", url)
    log.info("按 Ctrl+C 退出")
    log.info("=" * 56)
    stop = threading.Event()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        log.info("正在关闭服务…")
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
