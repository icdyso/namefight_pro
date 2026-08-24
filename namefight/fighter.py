"""斗士派生：名字 -> MD5 -> 属性 / 技能 / 称号。

确定性契约（AGENTS.md 2.1.1）：派生结果是 (归一化名字, 配置快照) 的纯函数。

- 主派生 PRNG 消耗顺序固定（v0.9.1）：属性（配置顺序，三角形分布投掷）-> 技能数量
  -> 技能抽取 -> 称号结构 -> 称号字段（按结构字段顺序）。
- 属性在 [min, max] 内三角形分布投掷（两个均匀数取均值，中点密度最高，
  天然不越界、端点无截断堆积，v1.1.0 起替代正态投掷）；
  命/攻为 ×100 整数量纲（命 20000 / 攻 1500），防御 750（v1.0.0 减半），
  速度同为 ×100 量纲（v1.2.1 起，~1000，与行动槽阈值 10000 配套）；
  投掷结果**取整**；
  crit/dodge 为百分数，保持浮点；
  全部数值**直接以引擎真实值显示**（不再换算白板 100 单位）。
- 技能个性化（熟练度/数值/词缀/变数随 MD5 扰动）使用独立种子
  md5(规范化名字 + ":" + 技能id)，与主派生流互不影响；熟练度为
  [0,100] 的三角形分布投掷（v1.1.0 起）。
- v0.9.0 起每个技能附带一个熟练度（0~100）与至多两个变数槽位：
  熟练度按技能各自的区间缩放触发概率（或条件型的效果值），
  变数槽位以 25% 概率实际成为共鸣变数（公式括号紧跟对应数值）。
- 文案（技能名/属性名/称号字段名等）与数值自 v0.10.0 起合并在
  config/game 同一条目内保存，本模块直接从 GameCfg 读取。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import GameCfg, TITLE_FIELD_POOLS
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 技能参数标签模板键（测试据此校验配置都有对应文案）
STATS_KEYS_USED = frozenset({
    "link_sep", "link_formula", "link_expr_difference", "link_expr_sum",
    "link_ratio", "link_difference", "link_sum",
    "scope_own", "scope_enemy", "scope_difference", "scope_sum",
    "field_value", "field_crit", "field_threshold", "field_turns",
    "field_damage", "field_ticks", "field_decay", "field_delay",
    "field_crit_value", "field_ratio", "field_cap", "field_cost",
    "field_convert", "field_per", "field_regen", "field_spd", "field_penalty",
    "mod_chance", "mod_value", "mod_damage", "mod_turns", "mod_ticks",
    "final_damage", "final_turns",
    "mastery_text", "mastery_text_value", "mastery_text_immune",
    "nat_charge", "nat_execution", "nat_lifesteal", "nat_poison",
    "nat_concussive", "nat_thunder", "nat_sever", "nat_gauge_surge",
    "nat_damage_reduction", "nat_reflect", "nat_bulwark", "nat_retribution",
    "nat_iron_will", "nat_heal", "nat_cleanse", "nat_low_hp_atk_bonus",
    "nat_streak_bonus", "nat_overload", "nat_armor_shred", "nat_bleed",
    "nat_gamble", "nat_tempo", "nat_armor_pen", "nat_blood_pact", "nat_grudge",
})

# 对战实时技能数据的占位符：live 文本中每个共鸣数值位 = 该标记 + 槽位序号，
# 序号 = 该变数在 eff["links"] 中的下标（与 link_calc 数组下标一致）。
# 前端按序号（而非占位符在文本中的位置）取值后替换，避免模板参数顺序
# 与共鸣槽位顺序不一致时数值交叉错位（v1.2.0 修复）。每技能至多两个占位符。
LIVE_MARKER = "\x01"

# 共鸣字段的展示格式与上下限：(效果类型, 字段) -> (fmt, lo, hi)
# fmt: pct 百分数 2 位小数 / num 取整 / turns 整数且至少 1。
# 与 apply_resonance 保持一致，前端实时计算复用同一张表。
_TURNS = ("turns", 1, 20)
RESONANCE_SPECS = {
    ("charge", "value"): ("pct", 0.5, 8.0),
    ("charge", "crit"): ("num", 0.0, 200.0),
    ("damage_multiplier", "value"): ("pct", 0.1, 6.0),
    ("damage_multiplier", "threshold"): ("pct", 0.05, 0.9),
    ("lifesteal", "value"): ("pct", 0.05, 1.5),
    ("lifesteal", "turns"): _TURNS,
    ("poison", "damage"): ("num", 0.0, None),
    ("poison", "ticks"): _TURNS,
    ("concussive", "value"): ("pct", 0.05, 1.0),
    ("concussive", "ticks"): _TURNS,
    ("thunder", "value"): ("pct", 0.05, 1.0),
    ("thunder", "decay"): ("pct", 0.5, 0.99),
    ("sever", "value"): ("pct", 0.1, 2.0),
    ("sever", "delay"): ("num", 0.0, None),
    ("gauge_surge", "value"): ("num", 0.0, None),
    ("gauge_surge", "crit_value"): ("num", 0.0, None),
    ("damage_reduction", "value"): ("pct", 0.01, 0.3),
    ("damage_reduction", "ticks"): _TURNS,
    ("reflect", "value"): ("pct", 0.05, 0.9),
    ("reflect", "ratio"): ("pct", 0.2, 4.0),
    ("bulwark", "value"): ("pct", 0.05, 0.9),
    ("bulwark", "threshold"): ("pct", 0.1, 0.9),
    ("retribution", "ratio"): ("pct", 0.2, 3.0),
    ("retribution", "cap"): _TURNS,
    ("iron_will", "value"): ("pct", 0.05, 0.9),
    ("iron_will", "decay"): ("pct", 0.05, 0.9),
    ("heal", "value"): ("num", 0.0, None),
    ("heal", "regen"): ("num", 0.0, None),
    ("cleanse", "value"): ("num", 0.0, None),
    ("cleanse", "per"): ("num", 0.0, None),
    ("low_hp_atk_bonus", "value"): ("pct", 0.05, 3.0),
    ("low_hp_atk_bonus", "spd"): ("pct", 0.05, 3.0),
    ("streak_bonus", "value"): ("pct", 0.005, 0.3),
    ("streak_bonus", "cap"): _TURNS,
    ("overload", "value"): ("pct", 0.5, 6.0),
    ("overload", "cost"): ("pct", 0.01, 0.4),
    ("armor_shred", "value"): ("num", 0.0, None),
    ("armor_shred", "ticks"): _TURNS,
    ("bleed", "value"): ("pct", 0.02, 1.0),
    ("bleed", "ticks"): _TURNS,
    ("gamble", "value"): ("pct", 0.5, 6.0),
    ("gamble", "penalty"): ("pct", 0.1, 1.0),
    ("tempo", "value"): ("num", 0.0, None),
    ("tempo", "atk"): ("num", 0.0, None),
    ("armor_pen", "crit"): ("num", 0.0, 200.0),
    ("blood_pact", "value"): ("pct", 0.05, 1.5),
    ("blood_pact", "convert"): ("pct", 0.05, 2.0),
    ("grudge", "value"): ("pct", 0.005, 0.3),
    ("grudge", "ticks"): _TURNS,
}

# 熟练度作用字段 -> 实际缩放的参数列表（条件触发型技能缩放效果值而非概率）
_MASTERY_PARAMS = {"chance": ("chance",), "value": ("value", "spd"), "immune": ("immune",)}

# 绝对数值字段表：(效果类型, 字段) -> 有量纲。
# v0.10.0 起仅用于词缀文案的展示语义：有量纲字段的词缀增量以整数展示，
# 纯倍率字段以百分数展示；数值本身一律以引擎真实值直显（不再换算）。
_FIELD_UNITS = {
    ("poison", "damage"): "hp",
    ("heal", "value"): "hp",
    ("heal", "regen"): "hp",
    ("cleanse", "value"): "hp",
    ("cleanse", "per"): "hp",
    ("armor_shred", "value"): "def",
    ("sever", "delay"): "gauge",
    ("gauge_surge", "value"): "gauge",
    ("gauge_surge", "crit_value"): "gauge",
    ("tempo", "value"): "spd",
    ("tempo", "atk"): "atk",
}


def field_unit(eff: dict, field: str):
    """效果数值字段是否为绝对数值（None = 纯倍率/百分比，直接展示）。"""
    return _FIELD_UNITS.get((str(eff.get("type")), field))


class InvalidName(Exception):
    """名字不合法。code 用于映射错误文案。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Fighter:
    name: str            # 原始输入（仅用于展示）
    normalized: str      # 归一化名字（MD5 与对战种子的依据）
    digest: str          # md5 hex
    attrs: dict          # 属性 id -> 三角形分布投掷值（浮点）；crit/dodge 以百分数存储
    skill_ids: tuple     # 按抽取顺序
    title_structure_id: str
    title_fields: dict   # 字段名 -> 字段 id
    power: int


def normalize_name(raw, system) -> str:
    """按 system 配置归一化名字（trim / 大小写折叠）。"""
    name = raw if isinstance(raw, str) else ""
    if system.name_trim:
        name = name.strip()
    if not system.name_case_sensitive:
        name = name.lower()
    if len(name) < system.name_min_length:
        raise InvalidName("empty_name")
    if len(name) > system.name_max_length:
        raise InvalidName("name_too_long")
    return name


def derive_fighter(raw_name, game: GameCfg) -> Fighter:
    normalized = normalize_name(raw_name, game.system)
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
    rng = DetRng(int(digest, 16))

    # 属性三角形分布投掷：[min, max] 内（两均匀数取均值，中点密度最高），
    # 消耗顺序 = 配置顺序。v0.10.0：非百分比属性为 ×100 整数量纲，投掷即取整；
    # crit/dodge 为百分数，保持浮点
    attrs = {}
    for a in game.attributes:
        roll = rng.next_triangular(a.min, a.max)
        attrs[a.id] = round(roll) if a.format != "percent" else roll

    count = rng.next_triangular_range(game.skill_count_min, game.skill_count_max)
    skills = rng.sample_weighted(((s, s.weight) for s in game.skills), count)

    structure = rng.pick_weighted((s, s.weight) for s in game.title_structures)
    title_fields = {}
    core_id = None
    for fname in structure.fields:
        pool = game.title_pools[TITLE_FIELD_POOLS[fname]]
        candidates = pool
        if fname == "core2" and core_id is not None:
            filtered = [t for t in pool if t.id != core_id]
            candidates = filtered or pool
        item = rng.pick_weighted((t, t.weight) for t in candidates)
        title_fields[fname] = item.id
        if fname == "core":
            core_id = item.id

    # 称号字段小额加成（不消耗随机数，纯查表）
    for attr_id, delta in title_bonus_items(title_fields, structure, game):
        attrs[attr_id] = max(1.0, attrs[attr_id] + delta)

    power = round(sum(attrs[a.id] * a.power_weight for a in game.attributes))
    return Fighter(
        name=raw_name if isinstance(raw_name, str) and raw_name else normalized,
        normalized=normalized, digest=digest,
        attrs=attrs,
        skill_ids=tuple(s.id for s in skills),
        title_structure_id=structure.id, title_fields=title_fields, power=power,
    )


def _apply_modifier(eff: dict, mod: dict) -> None:
    """词缀修正：仅作用于技能已有的参数（chance 截断到 [0.02, 0.95]，计数至少 1）。"""
    for key, delta in mod.items():
        if key not in eff:
            continue
        if key == "chance":
            eff["chance"] = min(0.95, max(0.02, float(eff["chance"]) + float(delta)))
        elif key in ("turns", "ticks", "cap"):
            eff[key] = max(1, int(round(float(eff[key]))) + int(round(float(delta))))
        else:
            eff[key] = float(eff[key]) + float(delta)


def personalized_effects(fighter: Fighter, game: GameCfg):
    """技能个性化：以 md5(规范化名字:技能id) 为种子做确定性扰动。

    消耗顺序固定（v0.9.0）：
    熟练度 -> value -> damage -> 前缀(是否 -> 抽取 -> 缩放) -> 后缀(是否 -> 抽取 -> 缩放)
    -> 变数槽位一(是否 -> 模式 -> 变量 -> 倍率) -> 变数槽位二(同前)。

    熟练度（0~100）按技能各自区间缩放 mastery_on 字段（默认触发概率）；
    每个变数槽位独立以 link.chance 概率成为共鸣变数。

    返回 [(SkillDef, 个性化后的效果dict), ...]，顺序与 fighter.skill_ids 一致。
    """
    var = game.skill_md5_variance
    link_cfg = game.skill_variable_link
    name_mod = game.skill_name_modifiers
    out = []
    for sid in fighter.skill_ids:
        sdef = next(s for s in game.skills if s.id == sid)
        eff = dict(sdef.effect)
        seed_hex = hashlib.md5((fighter.normalized + ":" + sid).encode("utf-8")).hexdigest()
        rng = DetRng(int(seed_hex, 16))
        # 熟练度：[0,100] 三角形分布投掷（集中于 50），按技能区间换算为触发概率
        # （或效果值）倍率。v1.1.0 起使用离散三角形 next_triangular_range。
        mastery = rng.next_triangular_range(0, 100)
        lo, hi = sdef.mastery
        mult = lo + (hi - lo) * mastery / 100.0
        eff["mastery"] = mastery
        eff["mastery_mult"] = mult
        for param in _MASTERY_PARAMS.get(sdef.mastery_on, ("chance",)):
            if param not in eff:
                continue
            scaled = float(eff[param]) * mult
            if param == "chance":
                eff["chance"] = min(0.95, max(0.02, scaled))
            elif param == "immune":
                eff["immune"] = min(0.5, max(0.01, scaled))
            else:
                eff[param] = scaled
        for key in ("value", "damage"):
            if key in eff:
                factor = rng.next_triangular(var.value_lo, var.value_hi)
                eff[key] = float(eff[key]) * factor
        if name_mod.prefix_chance > 0 and rng.next_float() < name_mod.prefix_chance:
            eff["prefix"] = rng.pick_weighted((m, m.weight) for m in name_mod.prefixes).id
            eff["prefix_scale"] = rng.next_triangular(name_mod.scale_lo, name_mod.scale_hi)
        if name_mod.suffix_chance > 0 and rng.next_float() < name_mod.suffix_chance:
            eff["suffix"] = rng.pick_weighted((m, m.weight) for m in name_mod.suffixes).id
            eff["suffix_scale"] = rng.next_triangular(name_mod.scale_lo, name_mod.scale_hi)
        for pool, mod_id, scale_key in ((name_mod.prefixes, eff.get("prefix"), "prefix_scale"),
                                        (name_mod.suffixes, eff.get("suffix"), "suffix_scale")):
            if not mod_id:
                continue
            mdef = next((m for m in pool if m.id == mod_id), None)
            if mdef is not None:
                scaled = {k: v * float(eff.get(scale_key, 1.0)) for k, v in mdef.mod.items()}
                _apply_modifier(eff, scaled)
        eff_type = str(eff.get("type"))
        fields = link_cfg.targets.get(eff_type, ())
        links = []
        if link_cfg.chance > 0 and fields:
            for field in fields:
                if field not in eff:
                    continue
                if rng.next_float() >= link_cfg.chance:
                    continue
                mode = rng.pick_weighted(link_cfg.mode_weights)
                vdef = rng.pick_weighted((v, v.weight) for v in link_cfg.variables)
                rate = rng.next_triangular(vdef.rate_lo, vdef.rate_hi)
                links.append({"field": field, "variable": vdef.id,
                              "rate": rate, "mode": mode})
        if links:
            eff["links"] = links
        out.append((sdef, eff))
    return out


def resonance_fields(eff: dict, game: GameCfg):
    """该技能的共鸣变数字段列表（按槽位顺序）；无共鸣返回空列表。"""
    return [str(link["field"]) for link in eff.get("links", ())
            if str(link.get("field")) in eff]


def resonance_coeff(own_get, enemy_get, link: dict, game: GameCfg) -> float:
    """归一化共鸣系数（不改变技能逻辑，只按比例修正技能自身参数）：

    - own / enemy：coeff = rate × (该方[变量]当前值 ÷ 变量基础值)
    - difference：coeff = rate × ((己方[变量] − 敌方[参照]) ÷ 变量基础值)，可为负
    - sum：       coeff = rate × ((己方[变量] + 敌方[参照]) ÷ 变量基础值)

    own_get/enemy_get 为「变量id -> 数值」的取值函数；基础值归一化保证
    不同量纲的变量产生可比的修正幅度。
    """
    vid = link.get("variable")
    try:
        base = max(1, game.attr(vid).base)
    except Exception:
        base = 1
    rate = float(link.get("rate", 0.0))
    mode = link.get("mode", "own")
    if mode in ("difference", "sum"):
        vdef = next((v for v in game.skill_variable_link.variables if v.id == vid), None)
        against = vdef.diff_against if vdef else vid
        raw = float(own_get(vid))
        other = float(enemy_get(against))
        raw = raw - other if mode == "difference" else raw + other
    elif mode == "enemy":
        raw = float(enemy_get(vid))
    else:
        raw = float(own_get(vid))
    return rate * (raw / base)


def _res_spec(eff: dict, field: str):
    return RESONANCE_SPECS.get((str(eff.get("type")), field), ("pct", 0.02, 5.0))


def apply_resonance(eff: dict, coeff: float, field: str) -> dict:
    """按共鸣系数缩放目标参数，返回新的效果 dict（按字段规格截断/取整）。"""
    scaled = dict(eff)
    if field not in scaled:
        return scaled
    fmt, lo, hi = _res_spec(scaled, field)
    value = float(scaled[field]) * (1.0 + coeff)
    if fmt == "turns":
        scaled[field] = max(1, int(round(value)))
        if hi is not None:
            scaled[field] = min(int(hi), scaled[field])
        return scaled
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    scaled[field] = value
    return scaled


def format_field(value, fmt: str) -> str:
    """共鸣字段统一展示：pct 两位百分数、num 整数、turns 整数。"""
    if fmt == "turns":
        return str(int(round(float(value))))
    if fmt == "num":
        return format_num(float(value))
    return format_pct(float(value))


def _sig_decimals(v: float, cap: int = 6) -> int:
    """保留两位有效数字所需的小数位数（至少 2 位、至多 cap 位）。"""
    v = abs(float(v))
    decimals = 2
    while decimals < cap and v * (10 ** decimals) < 10:
        decimals += 1
    return decimals


def _format_formula_number(display_value: float, bracket: bool) -> str:
    """共鸣公式内数值展示（v1.2.0）：

    默认保留两位小数（如 6.31 / 0.42%）；当数值低于 0.1 时「保留两位小数」
    会吞掉全部有效位变成 0.00——此时改为「保留两位有效数字」：
    - bracket=True（百分数字段括号内的纯数字，正常时不带 %）：
      数值 ×100 并加 %，如 0.00209 -> 0.21%；
    - bracket=False（已是百分数形式的系数，如数值字段的合并系数）：
      仅加深小数位，如 0.00209 -> 0.0021%。"""
    if abs(display_value) >= 0.1:
        return ("%.2f" if bracket else "%.2f%%") % display_value
    if bracket:
        p = display_value * 100.0
        return "%.*f%%" % (_sig_decimals(p), p)
    return "%.*f%%" % (_sig_decimals(display_value), display_value)


def format_resonance_final(scaled_value, field: str, eff: dict, game=None) -> str:
    """共鸣后目标参数的展示值（引擎真实值）；game 为 None 时不带单位词。"""
    fmt = _res_spec(eff, field)[0]
    text = format_field(scaled_value, fmt)
    if game is None:
        return text
    if fmt == "num":
        return render_template(game.stats.get("final_damage", "{v}"),
                               {"v": text}, game)
    if fmt == "turns":
        return render_template(game.stats.get("final_turns", "{v}"),
                               {"v": text}, game)
    return text


def estimated_resonanced_eff(fighter: Fighter, eff: dict, game: GameCfg):
    """卡牌展示用的共鸣估算：敌方取基础值（实际战斗中按当前值动态计算）。
    返回 (估算后的效果dict, [(link, 系数), ...])；无共鸣时返回 (eff, [])。"""
    display = dict(eff)
    coeffs = []
    base_get = lambda vid: game.attr(vid).base  # noqa: E731
    own_get = lambda vid: fighter.attrs.get(vid, 0)  # noqa: E731
    for link in eff.get("links", ()):
        field = str(link.get("field"))
        if field not in display:
            continue
        coeff = resonance_coeff(own_get, base_get, link, game)
        display = apply_resonance(display, coeff, field)
        coeffs.append((link, coeff))
    return display, coeffs


def title_bonus_items(title_fields, structure, game: GameCfg):
    """按结构字段顺序展开称号加成 [(属性id, 加成值), ...]（不去重，直接叠加）。"""
    items = []
    for fname in structure.fields:
        fid = title_fields.get(fname)
        pool = game.title_pools[TITLE_FIELD_POOLS[fname]]
        fdef = next((t for t in pool if t.id == fid), None)
        if fdef is None:
            continue
        for attr_id, delta in fdef.bonus.items():
            items.append((attr_id, int(delta)))
    return items


def _nat_params(display_eff: dict, game: GameCfg):
    """效果类型 -> (模板键, 模板参数)。数值为共鸣估算后的引擎真实值：
    百分数 2 位小数（format_pct），其余取整（format_num）。
    模板参数名与效果字段名一致，便于共鸣公式内联注入。"""
    ttype = display_eff.get("type")
    chance = float(display_eff.get("chance", 1.0))

    def num(field, default=0.0):
        """绝对数值字段：引擎真实值取整展示。"""
        return format_num(float(display_eff.get(field, default)))

    if ttype == "charge":
        return "nat_charge", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 1.0))),
            "crit": format_num(float(display_eff.get("crit", 0)))}
    if ttype == "damage_multiplier":
        return "nat_execution", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 1.0))),
            "threshold": format_pct(float(display_eff.get("threshold", 0)))}
    if ttype == "lifesteal":
        return "nat_lifesteal", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "turns": int(display_eff.get("turns", 0))}
    if ttype == "poison":
        return "nat_poison", {
            "chance": format_pct(chance),
            "damage": num("damage"),
            "ticks": int(display_eff.get("ticks", 0))}
    if ttype == "concussive":
        return "nat_concussive", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "ticks": int(display_eff.get("ticks", 0))}
    if ttype == "thunder":
        return "nat_thunder", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "decay": format_pct(float(display_eff.get("decay", 1.0))),
            "max": int(display_eff.get("max_hits", 0))}
    if ttype == "sever":
        return "nat_sever", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "delay": num("delay")}
    if ttype == "gauge_surge":
        return "nat_gauge_surge", {
            "chance": format_pct(chance),
            "value": num("value"),
            "crit_value": num("crit_value")}
    if ttype == "damage_reduction":
        return "nat_damage_reduction", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "ticks": int(display_eff.get("ticks", 0))}
    if ttype == "reflect":
        return "nat_reflect", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "ratio": format_pct(float(display_eff.get("ratio", 1.0)))}
    if ttype == "bulwark":
        return "nat_bulwark", {
            "threshold": format_pct(float(display_eff.get("threshold", 0.0))),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "immune": format_pct(float(display_eff.get("immune", 0.0)))}
    if ttype == "retribution":
        return "nat_retribution", {
            "chance": format_pct(chance),
            "ratio": format_pct(float(display_eff.get("ratio", 1.0))),
            "cap": int(display_eff.get("cap", 0))}
    if ttype == "iron_will":
        return "nat_iron_will", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "decay": format_pct(float(display_eff.get("decay", 0.0)))}
    if ttype == "heal":
        return "nat_heal", {
            "chance": format_pct(chance),
            "value": num("value"),
            "regen": num("regen"),
            "tick": int(display_eff.get("tick", 0)),
            "duration": int(display_eff.get("duration", 0))}
    if ttype == "cleanse":
        return "nat_cleanse", {
            "chance": format_pct(chance),
            "value": num("value"),
            "per": num("per")}
    if ttype == "low_hp_atk_bonus":
        return "nat_low_hp_atk_bonus", {
            "threshold": format_pct(float(display_eff.get("threshold", 0.3))),
            "value": format_pct(float(display_eff.get("value", 0.5))),
            "spd": format_pct(float(display_eff.get("spd", 0.0)))}
    if ttype == "streak_bonus":
        return "nat_streak_bonus", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "cap": int(display_eff.get("cap", 0))}
    if ttype == "overload":
        return "nat_overload", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 1.0))),
            "cost": format_pct(float(display_eff.get("cost", 0.0)))}
    if ttype == "armor_shred":
        return "nat_armor_shred", {
            "chance": format_pct(chance),
            "value": num("value"),
            "ticks": int(display_eff.get("ticks", 0)),
            "max_stacks": int(display_eff.get("max_stacks", 0))}
    if ttype == "bleed":
        return "nat_bleed", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "ticks": int(display_eff.get("ticks", 0))}
    if ttype == "gamble":
        return "nat_gamble", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 1.0))),
            "penalty": format_pct(float(display_eff.get("penalty", 1.0)))}
    if ttype == "tempo":
        return "nat_tempo", {
            "chance": format_pct(chance),
            "value": num("value"),
            "atk": num("atk")}
    if ttype == "armor_pen":
        return "nat_armor_pen", {
            "chance": format_pct(chance),
            "crit": format_num(float(display_eff.get("crit", 0)))}
    if ttype == "blood_pact":
        return "nat_blood_pact", {
            "chance": format_pct(chance),
            "cost": format_pct(float(display_eff.get("cost", 0.0))),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "convert": format_pct(float(display_eff.get("convert", 0.0)))}
    if ttype == "grudge":
        return "nat_grudge", {
            "chance": format_pct(chance),
            "value": format_pct(float(display_eff.get("value", 0.0))),
            "ticks": int(display_eff.get("ticks", 0))}
    return "nat_" + str(ttype), {}


def _link_formula(eff: dict, link: dict, field: str, game: GameCfg):
    """共鸣描述两部分（v0.10.0 起全部为引擎真实值）：

    1. 内联最简线性公式（紧跟对应数值）：最终值 = 基数 + 变量式 * 合并系数；
       变量式以属性 emoji 表示--own 省略范围词、enemy 前缀「对方」、
       difference「【己方-对方】」、sum「【己方+对方】」；
       v1.1.0 起百分数字段括号内为纯数字、百分号移到括号外，
       如（355.61+❤️*0.01）%；绝对数值字段仍以整数基数 + 百分数系数表示。
       v1.2.0 起数值保留两位有效数字：低于 0.1 的数值改为百分数形式
       （如 ❤️*0.21%），不再出现被两位小数吞没的 *0.00；
    2. 尾句依赖描述（使用属性全名，如「己方攻击越高，效果值越高。」）。
    """
    tmpl = game.stats
    var_id = str(link.get("variable"))
    var_def = game.attr(var_id)
    var_name = var_def.name
    var_emoji = var_def.emoji or var_name
    base = max(1.0, float(var_def.base))
    mode = str(link.get("mode", "own"))
    scope_own = str(tmpl.get("scope_own", ""))
    scope_enemy = str(tmpl.get("scope_enemy", ""))
    field_word = str(tmpl.get("field_" + field, field))
    fmt = _res_spec(eff, field)[0]
    eff_raw = float(eff.get(field, 0.0))
    merged_raw = eff_raw * float(link.get("rate", 0.0)) / base
    if fmt == "pct":
        # v1.1.0：百分数字段括号内为纯数字、百分号移到括号外，
        # 如「伤害提升至 375.52%（355.61+❤️*0.01）%」；
        # v1.2.0：低于 0.1 的数值改百分数形式保留有效位（如 ❤️*0.21%）
        base_display = _format_formula_number(eff_raw * 100.0, bracket=True)
        merged = _format_formula_number(merged_raw * 100.0, bracket=True)
    else:
        base_display = format_field(eff_raw, fmt)
        merged = _format_formula_number(merged_raw * 100.0, bracket=False)
    against = var_id
    if mode in ("difference", "sum"):
        vdef = next((v for v in game.skill_variable_link.variables if v.id == var_id), None)
        against = vdef.diff_against if vdef else var_id
        against_name = game.attr(against).name
        expr_tmpl = "link_expr_difference" if mode == "difference" else "link_expr_sum"
        expr = render_template(tmpl.get(expr_tmpl, "{emoji}"),
                               {"own": scope_own, "enemy": scope_enemy,
                                "emoji": var_emoji}, game)
        tail_tmpl = "link_difference" if mode == "difference" else "link_sum"
        tail = render_template(tmpl.get(tail_tmpl, ""),
                               {"own": scope_own + var_name,
                                "enemy": scope_enemy + against_name,
                                "field": field_word}, game)
    elif mode == "enemy":
        expr = scope_enemy + var_emoji
        tail = render_template(tmpl.get("link_ratio", ""),
                               {"scope": scope_enemy, "stat": var_name,
                                "field": field_word}, game)
    else:
        expr = var_emoji
        tail = render_template(tmpl.get("link_ratio", ""),
                               {"scope": scope_own, "stat": var_name,
                                "field": field_word}, game)
    formula = render_template(tmpl.get("link_formula", ""),
                              {"base": base_display, "expr": expr, "merged": merged},
                              game)
    if fmt == "pct":
        formula += "%"
    return formula, tail


def _natural_text(eff: dict, fighter: Fighter, game: GameCfg,
                  simple: bool = False, live: bool = False) -> str:
    """标准化自然语言描述。参数为共鸣估算后的最终值（敌方按基础值估算）：

    - 每个共鸣字段的公式括号紧跟该数值（simple 模式隐藏公式）；
    - live=True 时共鸣数值位替换为「LIVE_MARKER + 槽位序号」（序号对应
      link_calc 下标），供前端按快照实时填充，与模板中的出现位置无关。
    """
    display_eff, _ = estimated_resonanced_eff(fighter, eff, game)
    key, params = _nat_params(display_eff, game)
    params = dict(params)
    tails = []
    for slot, link in enumerate(eff.get("links", ())):
        field = str(link.get("field"))
        if field not in params:
            continue
        formula, tail = _link_formula(eff, link, field, game)
        final = str(params[field])
        if live:
            params[field] = "%s%d%s" % (LIVE_MARKER, slot,
                                        "" if simple else formula)
        elif simple:
            params[field] = final
        else:
            params[field] = final + formula
        if tail:
            tails.append(tail)
    text = render_template(game.stats.get(key, key), params, game)
    for tail in tails:
        text += tail
    if not text.endswith("。") and text:
        text = text + "。"
    return text


def _link_calc(eff: dict, game: GameCfg) -> list:
    """对战实时技能数据（每个共鸣变数一条）：前端按公式
    最终值 = base + 变量式 × coeff（含上下限）用双方快照逐刻重算，
    与引擎 resonance_coeff + apply_resonance 完全一致，按槽位顺序排列。
    v0.10.0 起 base/coeff/clamp 均为引擎真实值，与快照属性口径一致。"""
    out = []
    for link in eff.get("links", ()):
        field = str(link.get("field"))
        if field not in eff:
            continue
        var_id = str(link.get("variable"))
        base = max(1.0, float(game.attr(var_id).base))
        mode = str(link.get("mode", "own"))
        against = var_id
        if mode in ("difference", "sum"):
            vdef = next((v for v in game.skill_variable_link.variables
                         if v.id == var_id), None)
            against = vdef.diff_against if vdef else var_id
        fmt, lo, hi = _res_spec(eff, field)
        value = float(eff.get(field, 0.0))
        out.append({
            "field": field,
            "fmt": fmt,
            "base": value,
            "coeff": value * float(link.get("rate", 0.0)) / base,
            "mode": mode,
            "variable": var_id,
            "against": against,
            "clamp": [lo, hi],
        })
    return out


_MOD_TEMPLATES = {"chance": "mod_chance", "value": "mod_value",
                  "damage": "mod_damage", "turns": "mod_turns", "ticks": "mod_ticks"}


def _mod_texts(eff: dict, game: GameCfg) -> list:
    """词缀修正的可读文案（显示个性化缩放后的实际值），如「疾风：触发率 +3%」。
    绝对数值字段以整数展示，纯倍率字段以百分数展示。"""
    texts = []
    for kind, key, scale_key in (("prefix", "prefix", "prefix_scale"),
                                 ("suffix", "suffix", "suffix_scale")):
        mod_id = eff.get(key)
        if not mod_id:
            continue
        mdef = game.name_modifier(kind, mod_id)
        if mdef is None:
            continue
        scale = float(eff.get(scale_key, 1.0))
        parts = []
        for param, delta in mdef.mod.items():
            template_key = _MOD_TEMPLATES.get(param)
            if not template_key:
                continue
            scaled = float(delta) * scale
            if param == "chance":
                magnitude = format_pct(abs(scaled))
            elif param == "value" and not field_unit(eff, "value"):
                magnitude = format_pct(abs(scaled))  # 纯倍率字段：增量以百分数展示
            else:
                magnitude = format_num(abs(scaled))  # 绝对数值字段：真实值整数展示
            sign = "+" if scaled > 0 else "-"
            parts.append(render_template(game.stats.get(template_key, template_key),
                                         {"v": sign + magnitude}, game))
        if parts:
            texts.append(mdef.name + "：" + "，".join(parts))
    return texts


def _mastery_text(eff: dict, sdef, game: GameCfg) -> str:
    """熟练度文案（v0.9.1）：直接给出该技能实例的最终触发率，
    如「熟练度 63：触发率 36.52%」--永远不超过 100%；
    条件型技能给出效果倍率（×1.21），壁垒类给出免疫触发率。"""
    mastery = eff.get("mastery")
    if mastery is None:
        return ""
    if sdef.mastery_on == "immune":
        rate = min(0.5, max(0.01, float(eff.get("immune", 0.0))))
        return render_template(game.stats.get("mastery_text_immune", ""),
                               {"v": int(mastery), "rate": format_pct(rate)},
                               game)
    if sdef.mastery_on == "chance":
        rate = min(0.95, max(0.02, float(eff.get("chance", 0.0))))
        return render_template(game.stats.get("mastery_text", ""),
                               {"v": int(mastery), "rate": format_pct(rate)},
                               game)
    return render_template(game.stats.get("mastery_text_value", ""),
                           {"v": int(mastery),
                            "mult": "%.2f" % float(eff.get("mastery_mult", 1.0))},
                           game)


def _find_structure(fighter: Fighter, game: GameCfg):
    for s in game.title_structures:
        if s.id == fighter.title_structure_id:
            return s
    return None


def _format_bonus(value: float, attr_format: str) -> str:
    sign = "+" if value > 0 else ""
    if attr_format == "percent":
        return "%s%s%%" % (sign, format_num(value))
    return "%s%s" % (sign, format_num(value))


def _title_bonus_api(fighter: Fighter, game: GameCfg) -> dict:
    """称号加成的对外表示：按属性配置顺序聚合，供卡牌展示。
    value 为引擎真实值增量（v0.10.0 起直显，不再换算）。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return {"bonuses": [], "bonuses_text": ""}
    sums = {}
    for attr_id, delta in title_bonus_items(fighter.title_fields, structure, game):
        sums[attr_id] = sums.get(attr_id, 0) + delta
    bonuses = []
    parts = []
    for a in game.attributes:
        if a.id not in sums or sums[a.id] == 0:
            continue
        text = _format_bonus(sums[a.id], a.format)
        bonuses.append({"attr": a.id, "name": a.name, "value": sums[a.id],
                        "display": text, "format": a.format})
        parts.append("%s %s" % (a.name, text))
    return {"bonuses": bonuses, "bonuses_text": " · ".join(parts)}


def compose_title_name(fighter: Fighter, game: GameCfg) -> str:
    """按结构与连接符拼接称号显示名。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return fighter.title_structure_id
    parts = []
    for fname in structure.fields:
        fid = fighter.title_fields.get(fname)
        fdef = game.title_field(TITLE_FIELD_POOLS[fname], fid)
        parts.append(fdef.name if fdef is not None else str(fid or ""))
    if not parts:
        return ""
    out = parts[0]
    for i in range(1, len(parts)):
        connector = structure.connectors[i - 1] if i - 1 < len(structure.connectors) else ""
        out += connector + parts[i]
    return out


def compose_title_desc(fighter: Fighter, game: GameCfg) -> str:
    """把各字段的描述片段用「，」连接成称号描述。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return ""
    frags = []
    for fname in structure.fields:
        fid = fighter.title_fields.get(fname)
        fdef = game.title_field(TITLE_FIELD_POOLS[fname], fid)
        if fdef is not None and fdef.desc:
            frags.append(fdef.desc)
    return "，".join(frags) + "。" if frags else ""


def fighter_to_api(fighter: Fighter, game: GameCfg) -> dict:
    """斗士数据的对外表示：数值来自 Fighter/个性化效果，显示名来自同一配置。
    v0.10.0 起属性 value/min/max 均为引擎真实值（不再换算白板单位）。"""
    attrs_api = []
    for a in game.attributes:
        raw = fighter.attrs[a.id]
        attrs_api.append({
            "id": a.id,
            "name": a.name,
            "emoji": a.emoji,
            "value": round(float(raw), 4),
            "min": round(a.min, 4),
            "max": round(a.max, 4),
            "format": a.format,
        })
    skills_api = []
    for sdef, eff in personalized_effects(fighter, game):
        sep = str(game.stats.get("link_sep", "·"))
        name = sdef.name
        if eff.get("prefix"):
            pdef = game.name_modifier("prefix", eff["prefix"])
            if pdef is not None:
                name = pdef.name + sep + name
        if eff.get("suffix"):
            smod = game.name_modifier("suffix", eff["suffix"])
            if smod is not None:
                name = name + sep + smod.name
        links = eff.get("links", ())
        for link in links:
            marker = game.stats.get("link_" + str(link.get("variable")))
            if marker:
                name = name + sep + str(marker)
        link_api = []
        for link in links:
            vdef = next((a for a in game.attributes if a.id == link.get("variable")), None)
            link_api.append({
                "field": str(link.get("field")),
                "variable": link.get("variable"),
                "name": vdef.name if vdef else str(link.get("variable")),
                "mode": link.get("mode", "own"),
                "rate": link.get("rate", 0),
            })
        skill_entry = {
            "id": sdef.id,
            "name": name,
            "flavor": sdef.description,
            "text": _natural_text(eff, fighter, game),
            "text_simple": _natural_text(eff, fighter, game, simple=True),
            "modifiers": _mod_texts(eff, game),
            "mastery": int(eff.get("mastery", 0)),
            "mastery_text": _mastery_text(eff, sdef, game),
            "link": link_api if link_api else None,
        }
        if links:
            skill_entry["live_text"] = _natural_text(eff, fighter, game, live=True)
            skill_entry["live_text_simple"] = _natural_text(
                eff, fighter, game, simple=True, live=True)
            skill_entry["link_calc"] = _link_calc(eff, game)
        skills_api.append(skill_entry)
    title_bonus = _title_bonus_api(fighter, game)
    return {
        "name": fighter.name,
        "normalized": fighter.normalized,
        "digest": fighter.digest,
        "digest_short": fighter.digest[:8],
        "title": {
            "structure": fighter.title_structure_id,
            "name": compose_title_name(fighter, game),
            "description": compose_title_desc(fighter, game),
            "bonuses": title_bonus["bonuses"],
            "bonuses_text": title_bonus["bonuses_text"],
        },
        "attributes": attrs_api,
        "skills": skills_api,
        "power": fighter.power,
    }
