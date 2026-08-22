# AGENTS.md — namefight_pro 项目规约（必读）

> 本文件固化「名字竞技场」的底层需求、核心不变量与开发流程。任何开发者 / Agent
> 在修改本项目之前必须先阅读本文件；**任何更新都不得违背第 2 章的核心不变量**；
> 第 3 章流程规则同样适用于每次更新。若需求发生变化，应先修订本文件再动代码。

## 1. 项目定位

「名字竞技场」（Name Fight Arena）：

- 用户输入两个名字；
- 每个名字经 **MD5** 确定该斗士的全部属性、技能、称号、元素、稀有度；
- 两名斗士进行一场**完全确定**的回合制对战，输出逐条战报与胜负。

技术形态：

- 后端：**纯 Python（仅标准库）**，`python server.py` 直接运行，无任何第三方依赖；
- 前端：**无构建步骤**的静态 Web UI（原生 JS + 自编写微型框架，见 `web/js/framework.js`），
  不引入 npm / 打包器 / CDN，保证离线可运行；
- 无数据库：一切由名字实时推导。

## 2. 核心不变量（任何时候都不可破坏）

### 2.1 同名同命（确定性）

1. 斗士的一切派生数据 = `f(归一化后的名字, 当前配置快照)` 的**纯函数**。
   名字与配置不变，则属性 / 技能 / 称号 / 元素 / 稀有度**永远不变**，
   与运行次数、进程、机器无关。
2. 对战过程与结果 = `g(双方名字, 当前配置快照)` 的**纯函数**。
   **同样两个名字（无论输入顺序、无论何时查询）永远得到同一场对战、同一份战报、同一胜负。**
3. 禁止在对战与派生逻辑中使用任何非种子随机源：`random` 模块、时间、网络、
   全局可变状态等一律禁止。所有"随机"必须来自 `namefight/rng.py` 的确定性
   PRNG（splitmix64）：
   - 斗士种子 = `md5(归一化名字)`；
   - 对战种子 = `md5(按字典序排序后的两个归一化名字，以配置的分隔符连接)`。
4. 对战内部次序（先后手）只由（速度降序、规范化名字升序）决定，与输入顺序无关。
5. 派生时主 PRNG 的消耗顺序固定为：
   **稀有度 -> 元素 -> 属性（按配置顺序）-> 技能数量 -> 技能抽取 -> 称号结构 -> 称号字段（按结构字段顺序）**。
   改变此顺序属于 breaking change（见 3.4）。
   称号字段附带小额属性加成（`game/titles.json` 各字段 `bonus`），在稀有度倍率
   之后查表应用，不消耗随机数。
   技能个性化（触发概率/数值/词缀/变量共鸣随名字扰动）使用**独立种子**
   `md5(规范化名字 + ":" + 技能id)`，与主派生流互不影响；个性化消耗顺序固定：
   chance -> value -> damage -> 前缀(是否 -> 抽取) -> 后缀(是否 -> 抽取)
   -> 共鸣(是否 -> 来源 -> 模式 -> 变量 -> 倍率)。
   扰动、词缀与共鸣区间见 `config/game/skills.json` 的 `md5_variance`、
   `name_modifiers` 与 `variable_link`；全部公式与细则见 `docs/GAME_SPEC.md`。
6. 镜像对战（两个相同名字）允许，结果同样确定。
7. 配置文件内容属于「输入」的一部分：修改配置可能改变同名结果，属正常行为，
   但必须在更新文档中显著标注（见 3.4）。

### 2.2 功能与文字解耦（可配置性）

1. **数值 / 规则**全部位于 `config/game/*.json`：属性区间、技能效果与参数、
   称号权重、元素克制矩阵、稀有度加成、战斗常数、名字归一化规则。
2. **文案**全部位于 `config/locales/<lang>/*.json`（每语言十个文件）：UI 文案、
   属性显示名、技能名与描述、技能参数标签（stats）、称号字段（titles）、
   元素、稀有度、buff 文案（buffs）、技能词缀（modifiers）、战斗日志模板（battle_log）。
3. 代码中**禁止硬编码任何面向用户的文案**；技能描述只写风味文本，不重复数值
   （数值的唯一事实来源是 `config/game`）。
4. 战斗日志以「模板 id + 参数」结构化存储；技能 / 称号 / 元素等参数以
   `{"ref": 注册名, "id": 条目id}` 形式传递，渲染时才查当前语言的显示名，
   保证切换语言不改变战报结构。
5. 扩展方式：
   - 新增语言 = 新增 `config/locales/<lang>/` 目录（九个文件齐全）；
   - 新增技能 = `config/game/skills.json` 增加条目（效果类型必须是引擎
     `SUPPORTED_EFFECTS` 中已支持的）+ 各语言补充文案与 stats 标签；
   - 新增称号字段 = `game/titles.json` 对应池（prefixes/cores/suffixes）加条目
     + 各语言补 name/desc；
   - 新增称号结构 = `game/titles.json` 的 structures 加条目（字段名限定
     prefix / core / core2 / suffix，连接符数量为字段数减一）；
   - 新增元素 / 稀有度同理。元素仅为身份标识，不参与任何数值计算。

### 2.3 技术约束

- 后端仅用 Python 标准库；前端零依赖、零构建。
- 所有 JSON 读写显式 `encoding="utf-8"`；HTTP 响应一律 UTF-8。
- 前端渲染用户输入一律使用文本节点，禁止 innerHTML 注入用户内容。

## 3. 更新流程（每次更新必须执行）

1. **更新文档**：每次更新必须在 `docs/updates/` 新建 `YYYY-MM-DD-vX.Y.Z.md`
   （模板见 `docs/updates/_TEMPLATE.md`），包含：变更内容、动机、影响面、
   对确定性的影响（是否 breaking）、验证方式与结果。
2. **Git 提交**：更新完成后必须 git commit；若配置了远端且可达则 push 到
   GitHub；远端不可达 / 未配置时提交到本地 git，并在更新文档中注明。
3. 提交信息使用 Conventional Commits（feat / fix / docs / refactor / test / config）。
4. **版本号**：功能新增 → 次版本号 +1；修复 → 修订号 +1；会改变同名结果的
   数值 / 规则 / 算法变更 → 主版本号 +1 或明确 breaking 标注。
   版本号唯一维护于 `config/game/system.json` 的 `version` 字段。
5. **测试**：任何更新前后都必须运行 `python -m unittest discover -s tests -v`
   且全部通过；确定性测试（`tests/test_determinism.py`）失败视为最高优先级事故。
6. **规则手册同步**：任何涉及规则、数值、配置结构或文案结构的更新，都必须同步更新
   `docs/GAME_SPEC.md`（流程图、细则、数值表、JSON 指南），并在该文档头部维护
   当前版本号；纯代码重构且行为不变时可免。
7. 禁止提交运行时产物（`__pycache__` 等，见 `.gitignore`）。

## 4. 目录结构

```
namefight_pro/
├── AGENTS.md                 # 本文件：需求与规约
├── README.md                 # 使用与定制说明
├── server.py                 # 启动入口
├── namefight/                # 后端核心包（纯标准库）
│   ├── rng.py                # splitmix64 确定性 PRNG
│   ├── config.py             # 配置加载与校验
│   ├── fighter.py            # 名字 -> MD5 -> 斗士派生
│   ├── battle.py             # 确定性对战引擎
│   ├── text.py               # 模板渲染（文案与结构解耦）
│   └── server.py             # HTTP 服务（静态资源 + JSON API）
├── config/
│   ├── game/                 # 数值与规则（与语言无关）
│   │   ├── system.json       # 版本、语言列表、名字归一化规则
│   │   ├── attributes.json   # 属性区间与战力权重
│   │   ├── elements.json     # 元素池（仅身份标识，无克制）
│   │   ├── rarities.json     # 稀有度权重与属性倍率
│   │   ├── skills.json       # 技能池 + md5_variance + variable_link + name_modifiers
│   │   ├── titles.json       # 称号：结构池（structures）+ 字段池（prefix/cores/suffixes，含 bonus）
│   │   └── battle.json       # 战斗常数（暴击倍率/浮动/行动槽阈值/max_ticks 等）
│   └── locales/              # 文案（与数值无关），每语言十个文件：
│       ├── zh/               # ui/attributes/elements/rarities/skills/titles/stats/buffs/modifiers/battle_log
│       └── en/
├── web/                      # 前端（无构建、零依赖）
│   ├── index.html
│   ├── css/style.css
│   └── js/framework.js       # 自编写微型框架（NF.h 等）
│   └── js/app.js             # 应用逻辑（文案全部来自 /api/text）
├── tests/                    # unittest 测试
│   ├── test_determinism.py   # 核心不变量：同名同命 / 顺序无关 / 跨进程一致
│   └── test_config.py        # 配置完整性、locale 覆盖、PRNG 金向量
└── docs/
    ├── GAME_SPEC.md          # 完整规则手册：流程图/细则/数值/JSON 指南（更新须同步）
    └── updates/              # 更新文档（每次更新一份）
```

## 5. 运行与验证

- 启动：`python server.py [--host 127.0.0.1] [--port 8000]`
- 测试：`python -m unittest discover -s tests -v`
- 冒烟：`GET /api/health`、`GET /api/fighter?name=测试`、`POST /api/battle`

## 6. API 摘要

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | `{status, version}` |
| `GET /api/text?lang=zh` | 前端所需全部 UI 文案 + 可用语言列表 |
| `GET /api/fighter?name=X&lang=zh` | 斗士完整数据（含 MD5 摘要、属性、技能、称号） |
| `POST /api/battle` | body `{"a": "...", "b": "...", "lang": "zh"}`；返回双方数据、逐条战报（结构化事件 + 已渲染文本 + 双方状态快照）与胜负 |

错误以 `{"error": "<code>"}` + 4xx/5xx 返回，错误码文案同样由 locale 提供。

## 7. 设计备忘

- **tick 战斗模型**：战斗以 tick 推进；每个 tick 双方行动槽（gauge）累加自身速度值，
  达到阈值（`battle.json` 的 `gauge_threshold`，默认 100）即可行动一次并扣回阈值。
  速度决定行动频率（速度 12 ≈ 每 9 tick 行动一次，速度 9 ≈ 每 12 tick 一次）。
  同一 tick 多人可行动时按（gauge 余量降序、内部序）执行；内部序 = 速度降序、
  规范化名字升序，与输入顺序无关；
- 伤害公式：`max(最小伤害, round(ATK × 浮动 × 暴击倍率 × 技能倍率 − DEF × 防御系数))`；
  元素不参与计算（仅身份标识）；
- 技能参数按斗士 MD5 个性化：`md5(规范化名字 + ":" + 技能id)` 为种子的确定性扰动，
  区间为 `skills.json` 的 `md5_variance`（chance/value 各自的倍率范围）；
- **变量共鸣**：可共鸣类型的技能（`variable_link.linkable_types`）有概率与一项自身
  属性（atk/def/spd/hp/crit/dodge）建立共鸣，触发时附加
  `属性值 × rate` 的伤害（淬毒类加在毒伤上）；共鸣变量与倍率均由个性化种子决定，
  并以名称后缀标记体现（如「淬毒之刃·坚」= 防御共鸣，标记文案在 locale 的
  `stats.json` `link_*` 键）；
- **称号加成**：称号各字段自带小额属性加成（可为负），在稀有度倍率后叠加，
  卡牌展示聚合结果（「称号加成：生命 +3 · 攻击 -1」）；
- 毒在拥有者的行动时机结算；眩晕消耗一次行动；tick 耗尽（max_ticks）按剩余生命
  比例判定，完全相同则平局；
- `crit` / `dodge` 属性以百分数（整数）存储，判定时除以 100；
- 称号为多字段组合：结构（core / prefix+core / core+suffix / 双core / 全结构）
  与各字段均按权重概率抽取，显示名按结构连接符拼接，描述由字段描述片段以「，」拼接；
- 战报条目结构：`{tick, template, params, state, text}`；`state` 为双方快照
  （HP/ATK/DEF/SPD/行动槽百分比/buff 列表），buff 以 id+params 存储、渲染时查
  locale 的 buffs 模板；快照按输入位置 a/b 记录。
