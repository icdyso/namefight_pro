"""HTTP 服务：静态资源 + JSON API。纯标准库实现（见 AGENTS.md 2.3）。"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .battle import battle_to_api, run_battle
from .config import ConfigError, load_game_config, load_locale
from .fighter import InvalidName, derive_fighter, fighter_to_api

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WEB_ROOT = _REPO_ROOT / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class _Handled(Exception):
    """已向客户端发送错误响应，跳过后续处理。"""


class AppState:
    """启动时一次性加载的配置快照（修改配置需重启进程）。"""

    def __init__(self, config_root: Path):
        self.game = load_game_config(config_root)
        self.locales = {}
        for lang in self.game.system.available_locales:
            self.locales[lang] = load_locale(config_root, lang)
        if self.game.system.default_locale not in self.locales:
            raise ConfigError("默认语言不存在: %s" % self.game.system.default_locale)


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NameFightArena/" + state.game.system.version

        def log_message(self, fmt, *args):  # 静默访问日志
            pass

        # ---------- 响应工具 ----------

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, code, status=400):
            self._send_json({"error": code}, status)

        def _send_static(self, path: str):
            if path in ("/", "/index.html"):
                target = _WEB_ROOT / "index.html"
            else:
                target = (_WEB_ROOT / path.lstrip("/")).resolve()
                web_root = _WEB_ROOT.resolve()
                if web_root != target and web_root not in target.parents:
                    self._send_error_json("not_found", 404)
                    return
            if not target.is_file():
                self._send_error_json("not_found", 404)
                return
            ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- 路由 ----------

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            try:
                if path.startswith("/api/"):
                    self._api_get(path, parse_qs(parsed.query))
                else:
                    self._send_static(path)
            except _Handled:
                pass
            except InvalidName as e:
                self._send_error_json(e.code, 400)
            except Exception as e:  # noqa: BLE001 - API 兜底，避免线程崩溃
                self._send_error_json("internal_error", 500)
                print("[error] GET %s: %r" % (path, e), file=sys.stderr)

        def do_POST(self):
            path = unquote(urlsplit(self.path).path)
            if path != "/api/battle":
                self._send_error_json("not_found", 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                self._send_error_json("bad_request", 400)
                return
            try:
                self._api_battle(payload)
            except _Handled:
                pass
            except InvalidName as e:
                self._send_error_json(e.code, 400)
            except Exception as e:  # noqa: BLE001
                self._send_error_json("internal_error", 500)
                print("[error] POST /api/battle: %r" % (e,), file=sys.stderr)

        # ---------- API ----------

        def _require_lang(self, lang):
            if lang not in state.locales:
                self._send_error_json("unknown_locale", 400)
                raise _Handled()
            return lang

        def _api_get(self, path, query):
            if path == "/api/health":
                self._send_json({"status": "ok", "version": state.game.system.version})
            elif path == "/api/text":
                lang = self._require_lang((query.get("lang") or [state.game.system.default_locale])[0])
                self._send_json({
                    "lang": lang,
                    "langs": list(state.game.system.available_locales),
                    "version": state.game.system.version,
                    "ui": state.locales[lang].ui,
                })
            elif path == "/api/fighter":
                lang = self._require_lang((query.get("lang") or [state.game.system.default_locale])[0])
                name = (query.get("name") or [""])[0]
                fighter = derive_fighter(name, state.game)
                self._send_json(fighter_to_api(fighter, state.game, state.locales[lang]))
            else:
                self._send_error_json("not_found", 404)

        def _api_battle(self, payload):
            if not isinstance(payload, dict):
                self._send_error_json("bad_request", 400)
                raise _Handled()
            lang = self._require_lang(payload.get("lang") or state.game.system.default_locale)
            a = payload.get("a")
            b = payload.get("b")
            if not isinstance(a, str) or not isinstance(b, str):
                self._send_error_json("empty_name", 400)
                raise _Handled()
            fighter_a = derive_fighter(a, state.game)
            fighter_b = derive_fighter(b, state.game)
            outcome = run_battle(fighter_a, fighter_b, state.game)
            locale = state.locales[lang]
            fighters_api = [
                fighter_to_api(fighter_a, state.game, locale),
                fighter_to_api(fighter_b, state.game, locale),
            ]
            self._send_json(battle_to_api(outcome, fighters_api, locale))

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="名字竞技场 Name Fight Arena")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--config", default=str(_REPO_ROOT / "config"), help="配置目录")
    args = parser.parse_args(argv)

    try:
        state = AppState(Path(args.config))
    except ConfigError as e:
        print("配置加载失败: %s" % e, file=sys.stderr)
        sys.exit(2)

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print("名字竞技场 v%s 已启动: http://%s:%s" % (state.game.system.version, args.host, args.port))
    print("可用语言: %s（Ctrl+C 退出）" % ", ".join(state.game.system.available_locales))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
