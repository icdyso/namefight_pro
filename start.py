"""一键启动器（纯标准库）：自动挑空闲端口 -> 启动服务器 -> 打开浏览器。

用法：
    python start.py [--host 127.0.0.1] [--port 8123] [--no-browser] [server.py 的其余参数]

- 端口默认 8123，被占用时自动向后顺延（至多试 20 个），避免「端口已被占用」；
- 启动约 0.8 秒后用系统默认浏览器打开主页；编辑器地址会打印在控制台
  （http://<host>:<port>/editor.html）；
- Ctrl+C 退出。Windows 下双击「启动.bat」等效本脚本。
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

from namefight import server


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
    parser = argparse.ArgumentParser(description="名字竞技场一键启动")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8123, help="起始端口，默认 8123（被占用自动顺延）")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不自动打开浏览器")
    args, extra = parser.parse_known_args()

    port = pick_free_port(args.host, args.port)
    url = "http://%s:%s/" % (args.host, port)
    print("主页: %s" % url)
    print("可视化编辑器: http://%s:%s/editor.html" % (args.host, port))
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 复用 server.main 的完整启动流程（配置加载 / 热重载 / Ctrl+C 退出）
    sys.exit(server.main(["--host", args.host, "--port", str(port)] + extra))


if __name__ == "__main__":
    main()
