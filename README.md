# 名字竞技场 Name Fight Arena

输入两个名字，MD5 决定它们的一切——属性、技能、称号、元素、稀有度，以及一场完全确定的战斗。
**相同的名字，永恒的命运。**

- 后端：纯 Python（仅标准库），`python server.py` 直接运行
- 前端：无构建、零依赖的静态 Web UI（原生 JS + 自编写微型框架）
- 无数据库：一切由名字实时推导

## 快速开始

```bash
python server.py            # 默认 http://127.0.0.1:8000
python server.py --port 8000 --host 0.0.0.0
```

浏览器打开后：输入红蓝双方名字 → 「生成属性」查看斗士卡牌 → 「开始对战」观看逐条回放的战斗实录。

## 它是如何工作的

1. **归一化**：名字去除首尾空白并（默认）忽略大小写（规则见 `config/game/system.json`）；
2. **派生**：`md5(归一化名字)` 作为 splitmix64 PRNG 的种子，按固定顺序抽出稀有度、元素、六维属性（HP/攻击/防御/速度/暴击/闪避）、2–3 个技能与称号；
3. **对战**：双方规范化名字排序后连接取 MD5 作为对战种子；回合制战斗（普攻/暴击/闪避/技能触发/毒/眩晕/反伤/追击），回合耗尽按剩余生命比例判定；
4. **确定性契约**：名字与配置不变 ⇒ 派生数据与对战结果永远不变，与进程、机器、请求次数、输入顺序无关（有测试守护，见 `tests/test_determinism.py`）。

## 定制指南（功能与文字完全解耦）

| 想改什么 | 改哪里 |
| --- | --- |
| 属性区间、战力权重 | `config/game/attributes.json` |
| 技能效果与触发概率 | `config/game/skills.json`（效果类型须为引擎已支持的 12 种之一） |
| 称号池及权重 | `config/game/titles.json` |
| 元素与克制矩阵 | `config/game/elements.json` |
| 稀有度与属性倍率 | `config/game/rarities.json` |
| 战斗常数（暴击倍率、浮动、回合上限…） | `config/game/battle.json` |
| 名字归一化规则、版本号、语言列表 | `config/game/system.json` |
| 任何面向用户的文字 | `config/locales/<lang>/*.json` |

- **新增技能**：在 `game/skills.json` 加条目（参考现有 12 种 `effect.type` 写法）+ 在每个语言的 `skills.json` 补名称与风味描述；
- **新增语言**：复制 `config/locales/zh/` 为新目录并翻译 7 个文件，再把语言代码加入 `system.json` 的 `available_locales`；
- 修改配置后重启进程生效。**注意：修改数值配置会改变同名对战结果**，请按 `AGENTS.md` 的更新流程记录。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 项目结构与规约

见 [`AGENTS.md`](AGENTS.md)——核心不变量（同名同命、功能-文案解耦）与更新流程（每次更新必须写 `docs/updates/` 文档并提交 git）都以它为准。

## API 摘要

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查与版本 |
| `GET /api/text?lang=zh` | 前端 UI 全部文案 + 可用语言 |
| `GET /api/fighter?name=X&lang=zh` | 斗士完整数据（含 MD5 摘要） |
| `POST /api/battle` | `{"a":"...","b":"...","lang":"zh"}` → 双方数据 + 逐条战报 + 胜负 |

## 更新历史

见 [`docs/updates/`](docs/updates/)。
