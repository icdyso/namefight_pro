# AGENTS.md - namefight_pro 项目规约（必读）

> 「名字竞技场」：两个名字经 MD5 确定各自的属性 / 技能 / 称号，进行一场完全确定
> 的回合制对战。修改本项目前先读本文件；**第 2 章核心不变量任何时候不可破坏**。
> **工作方式：以完成任务为核心，不过度设计、不写多余的验证脚本（见 3）。**

## 1. 技术形态

- 后端：**纯 Python 标准库**，`python server.py` 直接运行，无第三方依赖、无数据库，
  一切由名字实时推导；
- 前端：**无构建**的静态页（原生 JS + 自写微型框架 `web/js/framework.js`），零依赖可离线；
- 单语言（中文），数值与文案**同条目**保存于 `config/game/*.json`。

## 2. 核心不变量（不可破坏）

### 2.1 同名同命（确定性）

1. 派生 = `f(归一化名字, 配置快照)` 的纯函数；对战 = `g(双方名字, 配置快照)` 的纯函数。
   同样的名字**永远**得到同样的属性 / 技能 / 称号 / 战报 / 胜负，与进程、机器、次数无关；
   镜像对战允许，同样确定。
2. 禁止任何非种子随机源（random / 时间 / 网络 / 全局可变状态）；一切随机走
   `namefight/rng.py`（splitmix64）。斗士种子 = `md5(归一化名字)`；
   对战种子 = `md5(字典序排序后的两个归一化名字，以 seed_separator 连接)`。
3. 先后手只由（速度降序、规范化名字升序）决定，与输入顺序无关。
4. 主 PRNG 消耗顺序固定：**属性（配置顺序）-> 技能数量 -> 技能抽取 -> 称号结构 ->
   称号字段**；技能个性化用独立种子 `md5(规范化名字 + ":" + 技能id)`，顺序固定：
   熟练度 -> value -> damage -> 前缀 -> 后缀 -> 变数槽一 -> 变数槽二。
   **改变任一顺序 = breaking**。
5. 数值随机一律**三角形分布**（`next_triangular`，两均匀数取均值）；离散选择用加权
   均匀抽取。精度：后台全程浮点，**只在最终应用时取整一次**（`battle._r`）；
   非百分比属性投掷即取整；展示层百分数 2 位小数、其余整数（共鸣公式内保留两位
   有效数字，<0.1 转百分数形式，如 `*0.21%`）。
6. 量纲（引擎真实值直显）：命 20000 [10000,30000]、攻 1500 [1000,2000]、防 750
   [500,1000]、**速度 1000 [500,1500]（v1.2.1 起 ×100）**、暴击 15 [5,30]、
   闪避 10 [5,15]（百分数）；**行动槽阈值 10000**（约每 10 刻一动）。
   速度 / 行动槽相关技能参数（斩断倒退、疾影前进、叠速、称号 spd 加成）同 ×100 量纲。
7. 配置属于「输入」：改配置会改同名结果（正常行为），但需在更新文档中标注。

### 2.2 配置即唯一事实来源

- 六个文件（`config/game/`）：`system`（版本 / 名字规则）、`attributes`（投掷区间 /
  战力权重 / emoji）、`skills`（技能池（**节点图格式**）+ `stats` 词表 + 个性化 /
  共鸣 / 词缀配置）、`titles`（结构池 + 字段池）、`battle`（常数 + `statuses`
  状态定义 + `battle_log` 模板 + `playback` 回放）、`ui`（界面文案）。
- 代码中禁止硬编码面向用户的文案（例外：编辑器管理页 `web/js/editor.js`）。
- 战报以「模板 id + 参数」结构化存储；技能 / 属性等参数以 `{"ref","id"}` 传递；
  每条战报附带 `rich` 富文本段与双方状态快照（前端渲染依据）。
- 扩展（v2.0.0 起均为纯配置操作，不改引擎）：**新技能** = `skills.json` 加条目，
  `effect` 为节点图 `{nodes, edges}`（触发钩子 / 条件（分支 gate: pass/fail）/
  原子 / 结构 loop（循环）的类型与参数规格须在 `namefight/effects.py` 注册表中，
  `GET /api/schema` 可查）+ `stats` 补 `op_*` 等词表；**新状态** =
  `battle.json` 的 `statuses` 加条目（策略字段 stack/expire/interval/lethal +
  params + mods 被动修饰（`namefight/statuses.py` 的 MOD_KINDS）+ effects
  效果图（5 个状态钩子，`$参数` 引用施加参数））；新称号字段 =
  `titles.json` 对应池加条目（name/desc/bonus，**bonus 最多三种属性、可负**）。

### 2.3 技术约束

- 后端仅标准库；前端零依赖零构建；JSON 读写显式 `encoding="utf-8"`；
  前端渲染用户输入一律文本节点，禁止 innerHTML 注入。

## 3. 更新流程（尽量精简）

1. **以完成任务为核心**：直接实现，不过度思考、不做多余的验证脚本与临时文件。
2. 更新文档：`docs/updates/YYYY-MM-DD-vX.Y.Z.md` 一份，内容可从简
   （变更 + 是否 breaking + 验证结论；模板见 `_TEMPLATE.md`）。
3. **测试只在必要时**：仅当改动引擎 / 派生 / PRNG 等底层时运行
   `python -m unittest discover -s tests`（确定性测试失败为最高优先级事故）；
   纯配置数值 / 文案 / 前端改动可跳过，不写临时验证脚本。
4. 涉及规则 / 数值 / 配置结构的变更同步 `docs/GAME_SPEC.md`（头部版本号）；
   行为不变的纯重构可免。
5. 版本号唯一维护于 `config/game/system.json`：功能 +次版本、修复 +修订号、
   会改同名结果的变更需明确 breaking 标注。
6. 完成后 git commit（Conventional Commits）并 push；远端不可达则提交本地并在
   更新文档注明。禁止提交 `__pycache__` 等运行时产物。

## 4. 目录结构

```
namefight_pro/
├── server.py                 # 启动入口
├── namefight/                # 后端：rng / config / fighter / battle / text / power / server
│                             #   + effects（钩子/条件/op 注册表与图编译）/ statuses（状态 kind 系统）
├── config/game/              # 六个配置 JSON（数值 + 文案同条目，单语言）
├── web/                      # 前端：index + power + editor 三页 + css + js(app/framework/power/editor)
├── tests/                    # unittest（test_determinism 核心不变量 / test_config 完整性与图校验）
├── tools/balance_check.py    # 技能平衡蒙特卡洛（固定种子）
└── docs/                     # GAME_SPEC.md 规则手册 + updates/ 更新文档 + title_candidates.md 称号候选库
```

## 5. 运行与 API

- 启动：`python server.py [--host 127.0.0.1] [--port 8000]`；可视化编辑器：`/editor.html`。
- API：`GET /api/health`、`GET /api/text`、`GET /api/fighter?name=`、
  `POST /api/battle`、`POST /api/battle/fast`、`POST /api/power`（真战力）、
  `GET /api/schema`（引擎自描述，编辑器表单驱动源）、
  `GET /api/config`、`POST /api/config/preview`、`POST /api/config/save`（编辑器保存 + 热重载）。
- 错误统一 `{"error": "<code>"}` + 4xx/5xx。

## 6. 设计备忘（现行规则速查）

- **技能图模型（v3.1.0 最小原子）**：技能逻辑 = 节点图 `{nodes, edges}`，四类
  节点——trigger（9 钩子）、condition（9 种，出边 gate: pass/fail 构成**分支**；
  compare 为通用「比较值与值」，14 种值源 × 4 种运算）、
  op（**13 个最小原子**：strike / hit_mod / taken_mod / grant_immune / stat_mod /
  hp_mod / gauge_mod / apply_status / cleanse / record / skip_action /
  marker / status_ctl）、struct（**loop 循环**：chain 衰减续链 / count 固定
  次数）；注册表与参数规格在 `namefight/effects.py`，执行顺序 = 技能派生顺序 ×
  触发节点数组顺序 × 边数组顺序（pass 组先于 fail 组）× loop 轮次
  （**改变即 breaking**）。
- **状态系统（v3.0.0）**：运行时容器 `_Combatant.st`（通用字段 params/stacks/
  expires/layers/next/actions/records/total/links）+ `markers`；定义数据化于
  `battle.json` 的 `statuses`——策略字段（stack/expire/interval/reset_on_miss/
  lethal）+ params（施加可覆盖、可个性化可共鸣）+ **mods 被动修饰表**（8 种
  kind，攻防聚合点按施加顺序聚合）+ **effects 效果图**（5 个状态钩子，毒发 /
  流血 / 回春 / 眩晕 / 蓄力释放 / 吸血均为图上原子组合，`$参数` 引用施加参数）；
  新建状态无需改引擎。
- **tick 模型**：每刻双方 gauge += 自身有效速度，达阈值（10000）行动一次并扣回；
  同刻多人按（gauge 余量降序、内部序）行动。
- **伤害**：`raw = 有效ATK × 三角浮动 × 暴击倍率 × 技能倍率`，
  `免伤率 = 有效DEF / (有效DEF + 2500) × (1 − 穿透)`，`dmg = max(100, raw × (1 − 免伤))`。
- **行动顺序**：斩断打断 -> 流血 -> 行动开始技能（血契/回春/净化）-> 眩晕 ->
  背水一战 -> 攻击（蓄力释放优先）；行动后：大器晚成叠速叠攻、嗜血递减、血契转化。
  毒每刻结算；超时按剩余生命比例判定，完全相同则平局。
- **熟练度** 0~100 缩放触发率（`mastery_on` 指定作用字段，可为数组，条件型缩放
  效果值）；**共鸣**双槽位各 25%（可共鸣资格由 op 参数规格的 `link` 标记声明），
  模式 own10 : enemy3 : difference2 : sum1，公式
  「基数 + 变量式*合并系数」直显真实值；live 文本占位符 = `\u0001 + 槽位序号`
  （对应 `link_calc` 下标），前端按序号替换。
- **称号（v1.2.1）**：结构固定为「前缀+主体」（structures 仅 `prefix_core`，
  连接符空串直拼）；字段自带 desc 与 bonus（最多三种属性、可负、与属性同量纲）。
  候选词条库：`docs/title_candidates.md`（500 前缀 + 500 主体，筛选后手动并入）。
- **战报角色名**一律为「【称号】名字」（`_Combatant.name`）；受击剩余生命不以
  文本呈现，由前端在角色名头顶渲染**无数字简易血条**（白=当前、红=本次掉血、
  绿=本次回血、灰=空）。
- 暴击上限 100%、闪避上限 80%；真实伤害（雷罚）无视防御但吃伤害减免并触发受击反应；
  不屈意志概率 ×0.5 乘算衰减。
- **真战力（v1.3.0）**：`/power.html` 独立页 + `POST /api/power`，与
  `power_check.enemies`（默认 10000）个固定编号敌人（名字 "1".."N"）各打一场
  极速对战（不生成快照、不记录战报，`run_battle(record=False)`，随机数消耗
  与常规对战一致），**胜场数即真战力**，与现行面板战力并行显示；
  正常对战不触发；敌人斗士按配置快照缓存于 `namefight/power.py`。
- 回放：逐条停顿 `message_delay_ms`，每 `action_pause_every` 次行动停
  `action_pause_ms`；支持单回合递进模式与简易显示模式。
