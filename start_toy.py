"""Toy 静态版一键启动器（纯标准库）：构建静态包 -> 静态服务 -> 打开浏览器。

用法：
    python start_toy.py [--host 127.0.0.1] [--port 8124] [--no-browser] [--no-rebuild]

- 每次启动默认重新构建 dist/toy（把 config/game 与 web 的最新改动打进包，
  构建约一秒）；--no-rebuild 跳过，直接服务现有包；
- 端口默认 8124，被占用时自动向后顺延（至多试 20 个）；
- 启动约 0.8 秒后打开主页；页面走 JS 静态引擎（NF.localApi），无任何后端；
- Ctrl+C 退出。Windows 下双击「启动Toy.bat」等效本脚本。
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import build_toy  # noqa: E402  （tools/build_toy.py：构建逻辑复用）


def pick_free_port(host: str, start: int, tries: int = 20) -> int:
    """从 start 起找一个可绑定端口（被占用则 +1 顺延）。"""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue          # 被占用：试下一个
            return port
    raise SystemExit("端口 %s~%s 均被占用，请用 --port 指定其他端口"
                     % (start, start + tries - 1))


def main() -> None:
    parser = argparse.ArgumentParser(description="名字竞技场 · Toy 静态版一键启动")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8124, help="起始端口，默认 8124（被占用自动顺延）")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不自动打开浏览器")
    parser.add_argument("--no-rebuild", action="store_true", help="跳过构建，直接服务现有 dist/toy")
    parser.add_argument("--out", default=str(ROOT / "dist" / "toy"), help="静态包目录")
    args = parser.parse_args()

    if not args.no_rebuild:
        build_toy.build(Path(args.out))
    if not (Path(args.out) / "index.html").is_file():
        raise SystemExit("静态包不存在：%s（先构建，或去掉 --no-rebuild）" % args.out)

    port = pick_free_port(args.host, args.port)
    handler = partial(SimpleHTTPRequestHandler, directory=str(Path(args.out)))
    httpd = ThreadingHTTPServer((args.host, port), handler)

    url = "http://%s:%s/index.html" % (args.host, port)
    print("Toy 静态版 v%s 已启动: %s" % (build_toy.current_version(), url), flush=True)
    print("真战力页: http://%s:%s/power.html" % (args.host, port), flush=True)
    print("Ctrl+C 退出", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
