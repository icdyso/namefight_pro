"""Toy 静态包构建：把对战页 + JS 引擎 + 原样配置 JSON 打成一个可直接发布的目录。

用法：
    python tools/build_toy.py            # 输出到 dist/toy/
    python tools/build_toy.py --zip      # 同时产出 dist/namefight-toy.zip

产物（B站 Toy 平台纯静态托管，页面跑在 /toy/<slug>/ 子路径）：
- index.html / power.html：源页面注入引擎脚本与本地 API 引导（编辑器不随包）
- css/ + js/：与源前端相同文件（相对路径，子路径托管安全）
- js/engine/：Python 引擎的 JS 移植（web/js/engine/，差分验证见 port_check.mjs）
- config/game/*.json：六份配置**原样随包**（与编辑器同源同格式，运行时 fetch；
  注意 file:// 直开不可用——fetch 本地 JSON 受 CORS 限制，需经 http 访问）

发布前请跑 toy_doctor 预检与预览确认（见 docs/updates 对应条目）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
CONFIG = ROOT / "config" / "game"

ENGINE_FILES = [
    "md5.js", "pyfmt.js", "rng.js", "text.js", "expr.js",
    "effects.js", "statuses.js", "config.js", "fighter.js", "battle.js", "api.js",
]
CONFIG_KEYS = ["system", "attributes", "skills", "titles", "battle", "ui"]
PAGES = ["index.html", "power.html"]          # 编辑器需后端，不进静态包

INJECT_MARK = '  <script src="./js/framework.js"></script>'


def current_version() -> str:
    """当前配置版本号（system.json，启动器展示用）。"""
    with (CONFIG / "system.json").open("r", encoding="utf-8") as f:
        return str(json.load(f)["version"])


def build(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "css").mkdir(parents=True)
    (out_dir / "js" / "engine").mkdir(parents=True)
    (out_dir / "config" / "game").mkdir(parents=True)

    # 配置原样随包（与编辑器同源同格式；运行时 fetch 加载，见 engine/api.js）
    for key in CONFIG_KEYS:
        shutil.copy2(CONFIG / (key + ".json"), out_dir / "config" / "game" / (key + ".json"))
    version = current_version()

    # 静态资源
    shutil.copy2(WEB / "css" / "style.css", out_dir / "css" / "style.css")
    for name in ("framework.js", "app.js", "power.js"):
        shutil.copy2(WEB / "js" / name, out_dir / "js" / name)
    for name in ENGINE_FILES:
        shutil.copy2(WEB / "js" / "engine" / name, out_dir / "js" / "engine" / name)

    # 页面注入：引擎脚本 -> 本地 API 引导（异步加载包内配置，framework 挂 NF.localApi）
    inject = ["  <!-- Toy 静态包构建注入：本地引擎 + 原样配置 JSON -->"]
    inject += ['  <script src="./js/engine/%s"></script>' % f for f in ENGINE_FILES]
    inject += ["  <script>window.NF_ENGINE_API = NFE.loadConfig();</script>"]
    snippet = "\n".join(inject)
    for page in PAGES:
        html = (WEB / page).read_text(encoding="utf-8")
        if INJECT_MARK not in html:
            raise SystemExit("页面 %s 缺少注入锚点（framework.js script 标签）" % page)
        (out_dir / page).write_text(html.replace(INJECT_MARK, snippet + "\n" + INJECT_MARK),
                                    encoding="utf-8")

    total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    print("Toy 静态包已构建: %s（v%s，%d 个文件，%.1f KB；配置 JSON 原样随包）"
          % (out_dir, version, len([p for p in out_dir.rglob("*") if p.is_file()]),
             total / 1024))
    print("本地预览: python start_toy.py（或 python -m http.server -d %s）" % out_dir)


def make_zip(out_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(out_dir))
    print("ZIP 已产出: %s（%.1f KB）" % (zip_path, zip_path.stat().st_size / 1024))


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 B站 Toy 静态发布包")
    parser.add_argument("--out", default=str(ROOT / "dist" / "toy"), help="输出目录")
    parser.add_argument("--zip", action="store_true", help="同时打 zip（toy create 可直接传目录）")
    args = parser.parse_args()
    out_dir = Path(args.out)
    build(out_dir)
    if args.zip:
        make_zip(out_dir, out_dir.parent / "namefight-toy.zip")


if __name__ == "__main__":
    main()
