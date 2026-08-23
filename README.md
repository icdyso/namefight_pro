# 名字竞技场 Name Fight Arena

输入两个名字，MD5 决定它们的一切——属性（正态投掷）、技能、称号与共鸣，以及一场完全确定的战斗。
**相同的名字，永恒的命运。**

- 后端：纯 Python（仅标准库），`python server.py` 直接运行
- 前端：无构建、零依赖的静态 Web UI（原生 JS + 自编写微型框架）
- 无数据库：一切由名字实时推导
- 单语言（中文）：数值与文案合并在 `config/game/*.json` 同文件保存

## 快速开始

```bash
python server.py            # 默认 http://127.0.0.1:8000
python server.py --port 8000 --host 0.0.0.0
```

浏览器打开后：输入红蓝双方名字 → 「生成属性」查看斗士卡牌 → 「开始对战」观看逐条回放的战斗实录。

## 它是如何工作的

1. **归一化**：名字去除首尾空白并（默认）忽略大小写（规则见 `config/game/system.json`）；
2. **派生**：`md5(归一化名字)` 作为 splitmix64 PRNG 的种子，按固定顺序投掷六维属性
   （高斯分布，非百分比为 ×100 整数量纲：命 20000 / 攻防速 1500）与 2–3 个技能、组合式称号；
3. **技能个性化**：每个技能的触发概率与数值再以 `md5(名字:技能id)` 为种子做确定性扰动
   （熟练度按区间缩放触发率；区间见 `skills.json` 的 `md5_variance`）——同名技能在不同名字手中强弱不同；
4. **名称词缀**：技能有概率获得前缀/后缀（如「疾风·重击·破军」），词缀名称与修正值同条目保存，
   修正值按名字个性化高斯缩放；
5. **变量共鸣**：每个技能两个槽位各以 25% 概率成为共鸣变数，**模式（己方/敌方/差值/并值）与
   变量均由 MD5 决定**，触发时按当前值归一化系数修正技能自身参数；公式括号紧跟对应数值。
   全部公式见 `docs/GAME_SPEC.md`；
6. **称号生成与加成**：按权重抽取结构与各字段（名称/描述/属性加成同条目，如「候补」攻击-100），
   拼接成如「孤高术士·候补」的称号；
7. **对战**：以 **tick** 推进——每 tick 双方行动槽累加有效速度，满阈值（10000）即可行动；
   防御为**倒数百分比免伤**：`免伤率 = DEF / (DEF + defense_constant)`，不再直接扣减；
   伤害含暴击/闪避/技能触发/共鸣附伤/毒/眩晕/反伤/追击；**全部计算结果取整**（多步浮点只在
   最终应用时取整一次）；行动刻耗尽按剩余生命比例判定；
8. **逐条回放与富文本战报**：每条战报停顿一段可配置的时间（`battle.json` 的
   `playback.message_delay_ms`）；阵营名红/蓝加粗、技能名各自配色加粗、伤害红/治疗绿；
   「XXX 使用了 XXXX」行后附该技能的个性化描述；HUD 实时刷新六维真实引擎值
   （变化时高亮）与行动槽数值（如 `440/10000`）；悬停技能或 buff 可查看详细说明；
9. **确定性契约**：名字与配置不变 ⇒ 派生数据与对战结果永远不变，与进程、机器、请求次数、
   输入顺序无关（有测试守护，见 `tests/test_determinism.py`）。

## 定制指南（数值与文案同文件保存）

| 想改什么 | 改哪里 |
| --- | --- |
| 属性区间、战力权重、显示名/emoji | `config/game/attributes.json` |
| 技能效果、名称、风味描述 | `config/game/skills.json` → `skills`（效果类型须为引擎已支持的 25 种） |
| 技能参数标签模板 / 共鸣句式 | `config/game/skills.json` → `stats` |
| 技能个性化扰动区间 | `config/game/skills.json` → `md5_variance` |
| 变量共鸣（概率/变量池/倍率/可共鸣类型） | `config/game/skills.json` → `variable_link` |
| 称号结构、字段池（名称/描述/加成） | `config/game/titles.json` |
| 战斗常数（暴击倍率、浮动、免伤常数、行动槽阈值、tick 上限…） | `config/game/battle.json` |
| 战报模板 / buff 文案 / 回放停顿时长 | `config/game/battle.json` → `battle_log` / `buffs` / `playback` |
| 界面文案 | `config/game/ui.json` |
| 名字归一化规则、版本号 | `config/game/system.json` |

- **新增技能**：在 `game/skills.json` 的 `skills` 加条目（含 `name`/`description` 与效果参数）+ `stats` 补 `nat_<type>` 模板；
- **新增称号字段**：`game/titles.json` 对应池加条目（含 `name`/`desc`/`bonus`）；
- 修改配置后重启进程生效。**注意：修改数值配置会改变同名对战结果**，请按 `AGENTS.md` 的更新流程记录。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 平衡性检验

```bash
python tools/balance_check.py [名字数] [对局数]
```

固定种子蒙特卡洛，检查各技能持有者胜率是否落在 45%–55%（当前基线 46.5%–54.6%）。

## 项目结构与规约

见 [`AGENTS.md`](AGENTS.md)——核心不变量（同名同命、数值与文案同文件）与更新流程（每次更新必须写 `docs/updates/` 文档并提交 git）都以它为准。

## API 摘要

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查与版本 |
| `GET /api/text` | 前端 UI 全部文案 + 回放配置 |
| `GET /api/fighter?name=X` | 斗士完整数据（含 MD5 摘要） |
| `POST /api/battle` | `{"a":"...","b":"..."}` → 双方数据 + 逐条战报（含富文本段与快照）+ 胜负 |

## 更新历史

见 [`docs/updates/`](docs/updates/)。
