# 名字竞技场 · 完整规则与配置手册（GAME_SPEC）

> 本文档是游戏的**完整规则说明书**：流程图、全部细则与数值、每个 JSON 配置文件的
> 意义与调整指南。按 AGENTS.md 第 3.6 条，**每次更新涉及规则/数值/配置结构时必须
> 同步更新本文档**。当前版本：**v0.5.0**（与 `config/game/system.json` 的 `version` 一致）。

---

## 1. 总览

输入两个名字 → 名字 MD5 确定斗士的一切（元素/技能/称号/共鸣）→ 以 tick
推进的确定性回合对战。核心契约：**名字 + 配置不变 ⇒ 派生与对战结果永远不变**
（与进程、机器、请求次数、输入顺序无关）。

v0.5.0 起的设计原则：**属性与稀有度不再随机**（属性为固定基础值，稀有度系统
已删除），斗士间的强度差异完全来自技能组合、称号加成、技能个性化与共鸣；
**所有数值类随机一律服从高斯分布**（见 2.3）。

```mermaid
flowchart LR
    A[名字输入] --> B[归一化]
    B --> C[md5 -> splitmix64 PRNG]
    C --> D[斗士派生<br/>元素/技能/称号]
    D --> E[技能个性化<br/>高斯扰动/词缀/共鸣]
    E --> F{开始对战?}
    F -->|是| G[对战种子 = md5 排序后双方名字]
    G --> H[tick 战斗循环]
    H --> I[结构化战报 + 状态快照]
    I --> J[前端逐刻回放 + HUD]
```

---

## 2. 随机体系

### 2.1 名字归一化

| 规则 | 当前值（`game/system.json` → `name`） |
| --- | --- |
| 去除首尾空白 `trim` | `true` |
| 大小写折叠 `case_sensitive` | `false`（`Alice` ≡ `alice`） |
| 最小/最大长度 | 1 / 32 |

归一化后为空 → 400 `empty_name`；超长 → 400 `name_too_long`。内部空格保留。

### 2.2 抽样规则

- **离散选择**（选哪个元素/技能/称号结构/字段/词缀/共鸣来源与模式）：加权均匀抽取；
- **数值抽样**（技能数量、个性化扰动倍率、共鸣倍率、伤害浮动）：
  **高斯分布** `DetRng.next_gaussian(lo, hi)`——Box-Muller 变换（每次消耗两个
  均匀数），均值 = 区间中点，σ = 区间宽 ÷ 4，越界截断到区间；
  离散数值（技能数量）用 `next_gaussian_range`（±0.5 扩展后取整）。

### 2.3 派生主 PRNG 消耗顺序（固定，改变即 breaking）

种子：`md5(归一化名字)` → splitmix64。

1. 元素（加权抽取；仅身份标识，不影响数值）
2. 技能数量 k（**高斯**，[2, 3]）
3. 技能抽取（不放回加权，保持顺序）
4. 称号结构（加权）→ 称号字段（按结构顺序加权；core2 排除已用核心）
5. 称号字段加成叠加到固定属性（纯查表，不消耗随机数）
6. 战力 = Σ(属性 × 权重)

**属性为固定基础值（无随机）**；**稀有度系统已删除**。

---

## 3. 属性（`game/attributes.json`）

| 属性 | 基础值 base | 展示条范围 | 战力权重 |
| --- | --- | --- | --- |
| hp 生命 | 100 | 80–120 | 1 |
| atk 攻击 | 13 | 10–16 | 4 |
| def 防御 | 5 | 2–8 | 3 |
| spd 速度 | 10 | 6–14 | 2 |
| crit 暴击 | 12% | 5–20 | 2 |
| dodge 闪避 | 10% | 5–15 | 2 |

`min/max` 仅用于卡牌进度条展示；实际值 = base + 称号加成（下限 1）。
元素池：烈焰/洪流/青木/惊雷 权重 1，圣光/暗影 0.8（纯装饰）。

---

## 4. 技能体系

### 4.1 技能池（`game/skills.json` → `skills`）

每名斗士不放回抽取 **2–3** 个（高斯，2 更常见）。触发时机：`on_attack` /
`on_defense` / `on_turn_start` / `passive`。

| 技能 | 触发 | 基础效果 |
| --- | --- | --- |
| 重击 heavy_strike | on_attack | 18% 概率，本次伤害 ×1.6 |
| 斩杀 execution | on_attack | 30% 概率，目标 HP≤35% 时伤害 ×2.0 |
| 嗜血 bloodthirst | on_attack | 伤害的 25% 转为生命 |
| 淬毒之刃 venom | on_attack | 30% 概率中毒：每次行动 2 点，持续 3 次 |
| 眩晕重锤 stun_blow | on_attack | 12% 概率眩晕（错过 1 次行动） |
| 连击 combo | on_attack | 22% 概率追加 50% 伤害一击 |
| 铁壁 iron_hide | on_defense | 受击伤害 -20% |
| 荆棘反甲 thorns | on_defense | 反弹 30% 所受伤害 |
| 疾风步 wind_step | passive | 闪避 +8 |
| 心眼 focus | passive | 暴击 +10 |
| 回春术 spring_heal | on_turn_start | 25% 概率回复 6 点 |
| 背水一战 last_stand | passive | HP<30% 时攻击 +50%（一次触发，持续到结束） |

### 4.2 个性化（`md5(名字:技能id)` 独立种子）

消耗顺序固定：**chance → value → damage → 前缀(是否→抽取) → 后缀(是否→抽取)
→ 共鸣(是否→来源→模式→变量→倍率)**。

- **方差扰动 `md5_variance`**（**高斯**）：概率 ×N(1.025, σ0.1625) 区间[0.7,1.35]
  （截断），数值 ×N(1.05, σ0.1) 区间[0.85,1.25]；概率截断到 [2%, 95%]；
- **名称词缀 `name_modifiers`**：前缀 50% / 后缀 40%；词缀对技能**已有参数**做
  小幅加法修正（概率截断、持续至少 1）：

| 词缀 | 修正 | | 词缀 | 修正 |
| --- | --- | --- | --- | --- |
| 疾风(前) | 触发率 +3% | | 破军(后) | 效果值 +6% |
| 猛烈(前) | 效果值 +8% | | 蹈隙(后) | 触发率 +4% |
| 蚀骨(前) | 毒伤 +1 | | 入骨(后) | 毒伤 +1 |
| 缠绵(前) | 持续 +1 次 | | 不息(后) | 持续 +1 次 |
| 浩大(前) | 效果值 +5%、触发率 +2% | | 汹涌(后) | 效果值 +12%、触发率 -3% |

- **变量共鸣 `variable_link`**（重击/斩杀/连击/淬毒/眩晕）：概率 **95%**；
  来源（己方 3 : 敌方 2）、模式（比例 3 : 差值 2）、变量、倍率（**高斯**）依次抽取。

**共鸣公式（触发时刻动态求值）**：

```
比例模式：  bonus = round( 源方[变量].当前值 × rate )
差值模式：  bonus = round( ( 己方[变量].当前值 − 敌方[参照].当前值 ) × rate )
bonus = max(0, bonus)      // 负差不提供加成
```

「当前值」：hp = 当前生命（动态），atk = 含背水一战，crit/dodge = 含被动，
def/spd = 面板。差值参照（`diff_against`）：攻↔防、防↔攻、速↔速、命↔命、
暴击↔闪避、闪避↔暴击。淬毒类共鸣加在毒伤上，其余加在本次打击上。

| 变量 | 权重 | 倍率区间（高斯） | 差值参照 |
| --- | --- | --- | --- |
| atk | 3 | 0.35–0.80 | def |
| def | 2 | 0.50–1.50 | atk |
| spd | 3 | 0.40–0.90 | spd |
| hp | 2 | 0.04–0.10 | hp |
| crit | 1 | 0.10–0.35 | dodge |
| dodge | 1 | 0.20–0.50 | crit |

### 4.3 技能描述（标准化自然语言，v0.5.0 起）

- **描述 = 一句自然语言**，由 `locale/stats.json` 的 `nat_<效果类型>` 模板 +
  个性化真实数值生成，单位随文（点/%/次行动）；
  例：「攻击时有 36% 概率使敌方中毒：其每次行动损失 2 点生命，持续 3 次行动。」；
- **共鸣描述 = 「XX越XX」句式 + 内联公式**：
  比例：「{stat}越高，附加伤害越高（+ {stat} × {pct} 点）。」
  差值：「{own}越高于{enemy}，附加伤害越高（+（{own} − {enemy}）× {pct} 点）。」
  例：「敌方防御越高，附加伤害越高（+ 敌方防御 × 75% 点）。」；
- 技能名组装：`[前缀·]技能名[·后缀][·共鸣标记]`（标记：锋/坚/疾/命/锐/影），
  连接符 `link_sep`；风味短句（locale `skills.json` 的 `description`）作为副行展示。

---

## 5. 称号系统

| 结构 | 权重 | 字段 | 连接 | 示例 |
| --- | --- | --- | --- | --- |
| core | 20 | 核心 | — | 剑圣 |
| prefix_core | 26 | 前缀+核心 | （无） | 暗夜剑圣 |
| core_suffix | 22 | 核心+后缀 | · | 剑圣·无双 |
| dual_core | 18 | 核心+核心2 | · | 剑圣·酒仙 |
| full | 14 | 前缀+核心+后缀 | （无）/· | 血月剑圣·再临 |

字段池：前缀 ×10、核心 ×12、后缀 ×8（见 `game/titles.json`）。

**称号加成**（字段 `bonus`，叠加到固定属性）：

- 前缀：+1 单项（boundless 生命+2）
- 核心：+1 单项（war_god 攻+2、warlock 命+3、medic 命+4）
- 后缀：无双 攻+1暴+1 / 觉醒 暴+1闪+1 / 再临 命+3 / 传说 攻+2 / 亲卫 防+2 /
  见习 暴+1 / **候补 攻-1** / **退役 速-1 防+1**（负加成为刻意设计）

描述 = 字段描述片段以「，」连接 +「。」；卡牌另展示聚合加成行。

---

## 6. 战斗规则（tick 模型）

**种子**：`md5(排序后的双方规范化名字 以 \x1f 连接)`；内部序 = 速度降序、
名字升序（与输入顺序无关；镜像对战允许）。

```mermaid
flowchart TD
    S[战斗开始] --> T{tick < max_ticks<br/>且无人倒下?}
    T -->|是| G[每刻: 存活方行动槽 += 自身速度]
    G --> R[行动槽 >= 100 者可行动<br/>按槽余量降序、内部序依次]
    R --> A1[毒发结算] --> A2[行动开始技能] --> A3{眩晕?}
    A3 -->|是| A4[消耗行动, 跳过] --> T
    A3 -->|否| A5[背水一战判定] --> A6[执行攻击]
    A6 --> A7{有人倒下?}
    A7 -->|否| T
    A7 -->|是| W[胜利判定]
    T -->|否| J[超时: 按剩余生命比例判定<br/>完全相同则平局]
    W --> V[输出战报与快照]
    J --> V
```

### 攻击结算顺序

1. on_attack 技能逐个判定：触发 → 共鸣附伤（动态）→ 倍率/吸血/中毒/眩晕/追击；
2. 闪避判定（`rand < 敌方闪避%` → 落空）；3. 暴击判定 → **高斯**伤害浮动 → 伤害公式；
4. on_defense 技能：减伤、反甲（可致死）；5. 结算伤害/吸血/上毒/眩晕；6. 追击。

**伤害公式**：

```
raw   = 有效ATK × 追击比例 × 高斯浮动(中心1.0, σ0.075, 截断[0.85,1.15]) × 暴击倍率(1.8) × 技能倍率
dmg   = max( 1, round( raw + 共鸣附伤 − 敌方DEF × 1.0 ) )
减伤后 = max( 1, round( dmg × (1 − 减伤率) ) )
```

### 战斗常数（`game/battle.json`）

| 常数 | 值 |
| --- | --- |
| gauge_threshold | 100（行动槽阈值，每刻 += 速度） |
| max_ticks | 600 |
| crit_multiplier | 1.8 |
| variance | [0.85, 1.15]（高斯截断区间） |
| defense_factor / min_damage | 1.0 / 1 |
| crit_cap / dodge_cap | 100 / 60 |
| seed_separator | `\u001f` |

行动间隔 ≈ `100 ÷ 速度` 刻（速度 10 ≈ 每 10 刻；称号 ±1 速度带来节奏差异）。

---

## 7. 战报与状态快照

条目：`{tick, template, params, state, text}`。`params` 中技能/属性/措辞以
`{"ref": 注册名, "id": 条目id}` 传递（注册名：skill/element/attr/stat_word），
渲染时查 locale。`state` 按输入位置 a/b，含
`{hp, max_hp, atk(有效), def, spd, gauge(0-100), buffs:[{id,name,detail,desc}]}`；
buff 集合：poison/stun/last_stand/crit_up/dodge_up。

前端逐刻回放：每 `TICK_MS(85ms) ÷ 倍速` 推进一刻；行动槽每刻 +速度%
（客户端模拟 + 快照校正）；条目在其所属刻揭示。

---

## 8. API

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | `{status, version}` |
| `GET /api/text?lang=zh` | UI 全部文案 + 语言列表 + 版本 |
| `GET /api/fighter?name=X&lang=zh` | 斗士数据（属性、自然语言技能描述、称号加成、共鸣公式） |
| `POST /api/battle` | `{a,b,lang}` → 双方 + 逐条战报（含快照）+ 胜负 |

错误码：empty_name / name_too_long / unknown_locale / bad_request / not_found / internal_error。

---

## 9. `config/game/*.json` 调整指南（共 6 个文件）

### system.json —— 全局
`version`（更新时同步）、`default_locale`、`available_locales`、
`name{trim,case_sensitive,min_length,max_length}`。
示例：大小写敏感 → `"case_sensitive": true`（**breaking**）。

### attributes.json —— 属性（固定值）
`attributes[] {id, base, min, max, format, power_weight}`：`base` 为固定基础值，
`min/max` 仅用于卡牌进度条；引擎必需 id：hp/atk/def/spd/crit/dodge。
示例：全体提速 → `"base": 12`（影响所有名字的战斗节奏，breaking）。

### elements.json —— 元素池
`elements[] {id,weight}`，纯身份标识。新增元素需同步双语言 locale。

### skills.json —— 技能池与个性化
- `skill_count{min,max}`；
- `skills[] {id,weight,trigger,effect}`（`effect.type` 须为引擎已支持的 12 种；
  伤害倍率类可带 `condition`）；
- `md5_variance{chance[lo,hi], value[lo,hi]}`：高斯扰动区间（σ = 宽/4）；
- `variable_link`：`chance` / `linkable_types` / `source_weights` / `mode_weights` /
  `variables{id→{weight, rate[lo,hi], diff_against}}`；
- `name_modifiers`：`prefix_chance` / `suffix_chance` / `prefixes[]` / `suffixes[]`
  （`mod` 键限定 chance/value/damage/turns）。

示例：共鸣必发且全差值 → `"chance": 1.0, "mode_weights": {"ratio":1,"difference":4}`。

### titles.json —— 称号
`structures[] {id,weight,fields[],connectors[]}`（字段限定 prefix/core/core2/suffix）
与 `prefixes/cores/suffixes[] {id,weight,bonus{属性:增量}}`。

### battle.json —— 战斗常数
见第 6 节表。示例：更快战斗 → `"gauge_threshold": 80`。

---

## 10. `config/locales/<lang>/*.json` 调整指南（共 9 个文件）

| 文件 | 内容 | 注意 |
| --- | --- | --- |
| ui.json | 全部界面文案（含错误码、称号加成标签） | 各语言键集合必须一致（有测试） |
| attributes.json | 属性显示名 | 覆盖全部属性 id |
| elements.json | 元素名 + emoji | 覆盖全部元素 id |
| skills.json | 技能名 + 风味短句 `description` | 覆盖全部技能 id；数值不写在这里 |
| titles.json | 称号字段 name/desc | 覆盖全部字段 id |
| stats.json | 自然语言模板 `nat_*`、共鸣句式 `link_ratio`/`link_difference`、共鸣标记 `link_*`、来源/模式词、词缀修正模板 `mod_*`、连接符 `link_sep` | 占位符如 `{chance}` `{stat}` `{pct}` |
| buffs.json | buff 的 name/detail/desc | 覆盖引擎 5 种 buff id |
| modifiers.json | 词缀名称 | 覆盖全部词缀 id |
| battle_log.json | 全部战报模板 | 覆盖引擎全部模板 id（有测试） |

新增语言：复制 `locales/zh/` 九个文件翻译，加入 `available_locales`，重启生效。

---

## 11. 前端行为要点

- 逐刻回放（85ms/刻 ÷ 倍速）；行动槽每刻推进、满槽揭示该刻条目并按快照回落；
- HUD：HP/攻防速/行动槽/buff 徽章；背水一战攻击高亮；buff 差量刷新；
- 技能卡：自然语言描述（含共鸣句式与公式）为主行，风味短句为副行；
  悬停显示完整说明与词缀明细；
- 全部文案来自 `/api/text` 与 API 响应，前端零硬编码文案。

---

## 12. 数值速查（v0.5.0）

- 属性固定：HP 100 / ATK 13 / DEF 5 / SPD 10 / CRIT 12% / DODGE 10%（+称号加成）
- 技能数 2–3（高斯）；共鸣概率 95%；词缀 前缀 50% / 后缀 40%
- 数值类随机全部高斯（σ = 区间宽 ÷ 4，截断区间）
- 行动槽 100 / 最大 600 刻 / 暴击 ×1.8 / 高斯浮动 ±15%（截断）/ 伤害下限 1
- 暴击上限 100%、闪避上限 60%；中毒按行动结算、眩晕消耗一次行动
- 超时：按剩余生命比例判定，完全相同则平局
