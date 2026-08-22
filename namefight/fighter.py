"""斗士派生：名字 -> MD5 -> 属性 / 技能 / 称号 / 元素。

确定性契约（AGENTS.md 2.1.1）：派生结果是 (归一化名字, 配置快照) 的纯函数。

- 主派生 PRNG 消耗顺序固定：元素 -> 属性（配置顺序，白板 100 基准）-> 技能数量
  -> 技能抽取 -> 称号结构 -> 称号字段（按结构字段顺序）。
- 技能个性化（熟练度/数值/词缀/变数随 MD5 扰动）使用独立种子
  md5(规范化名字 + ":" + 技能id)，与主派生流互不影响。
- v0.9.0 起每个技能附带一个熟练度（0~100）与至多两个变数槽位：
  熟练度按技能各自的区间缩放触发概率（或条件型的效果值），
  变数槽位以 25% 概率实际成为共鸣变数（公式括号紧跟对应数值）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import GameCfg, TITLE_FIELD_POOLS
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 字段名 -> locale.titles 中的键
_FIELD_LOCALE = {"prefix": "prefixes", "core": "cores", "core2": "cores", "suffix": "suffixes"}

# 技能参数标签模板键（测试据此校验每个 locale 都有对应文案）
STATS_KEYS_USED = frozenset({
    "link_sep", "link_formula", "link_expr_difference", "link_expr_sum",
    "link_ratio", "link_difference", "link_sum",
    "scope_own", "scope_enemy", "scope_difference", "scope_sum",
    "field_value", "field_crit", "field_threshold", "field_turns",
    "field_damage", "field_ticks", "field_decay", "field_delay",
    "field_crit_value", "field_ratio", "field_cap", "field_cost",
    "field_convert", "field_per", "field_regen", "field_spd", "field_penalty",
    "mod_chance", "mod_value", "mod_damage", "mod_turns", "mod_ticks",
    "final_damage", "final_turns", "mastery_text", "mastery_text_value",
    "nat_charge", "nat_execution", "nat_lifesteal", "nat_poison",
    "nat_concussive", "nat_thunder", "nat_sever", "nat_gauge_surge",
    "nat_damage_reduction", "nat_reflect", "nat_bulwark", "nat_retribution",
    "nat_iron_will", "nat_heal", "nat_cleanse", "nat_low_hp_atk_bonus",
    "nat_streak_bonus", "nat_overload", "nat_armor_shred", "nat_bleed",
    "nat_gamble", "nat_tempo", "nat_armor_pen", "nat_blood_pact", "nat_grudge",
})

# 对战实时技能数据的占位符：live 文本中每个共鸣数值位替换为该标记
# （每技能至多两个），前端按快照实时计算最终值后依序替换回文本
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
    ("armor_pen", "crit"): ("num", 0.0, 200.0),
    ("blood_pact", "value"): ("pct", 0.05, 1.5),
    ("blood_pact", "convert"): ("pct", 0.05, 2.0),
    ("grudge", "value"): ("pct", 0.005, 0.3),
    ("grudge", "ticks"): _TURNS,
}

# 熟练度作用字段 -> 实际缩放的参数列表（条件触发型技能缩放效果值而非概率）
_MASTERY_PARAMS = {"chance": ("chance",), "value": ("value", "spd"), "immune": ("immune",)}


class InvalidName(Exception):
    """名字不合法。code 用于映射 locale 错误文案。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Fighter:
    name: str            # 原始输入（仅用于展示）
    normalized: str      # 归一化名字（MD5 与对战种子的依据）
    digest: str          # md5 hex
    element_id: str
    attrs: dict          # 属性 id -> 整数值；crit/dodge 以百分数存储
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

    element = rng.pick_weighted((e, e.weight) for e in game.elements)

    # 属性为固定基础值（无随机）；差异来自称号加成与技能个性化
    attrs = {a.id: a.base for a in game.attributes}

    count = rng.next_gaussian_range(game.skill_count_min, game.skill_count_max)
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
        attrs[attr_id] = max(1, attrs[attr_id] + delta)

    power = round(sum(attrs[a.id] * a.power_weight for a in game.attributes))
    return Fighter(
        name=raw_name if isinstance(raw_name, str) and raw_name else normalized,
        normalized=normalized, digest=digest,
        element_id=element.id, attrs=attrs,
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
        # 熟练度：高斯集中于 50，按技能区间换算为触发概率（或效果值）倍率
        mastery = int(round(rng.next_gaussian(0.0, 100.0)))
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
                factor = rng.next_gaussian(var.value_lo, var.value_hi)
                eff[key] = float(eff[key]) * factor
        if name_mod.prefix_chance > 0 and rng.next_float() < name_mod.prefix_chance:
            eff["prefix"] = rng.pick_weighted((m, m.weight) for m in name_mod.prefixes).id
            eff["prefix_scale"] = rng.next_gaussian(name_mod.scale_lo, name_mod.scale_hi)
        if name_mod.suffix_chance > 0 and rng.next_float() < name_mod.suffix_chance:
            eff["suffix"] = rng.pick_weighted((m, m.weight) for m in name_mod.suffixes).id
            eff["suffix_scale"] = rng.next_gaussian(name_mod.scale_lo, name_mod.scale_hi)
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
                rate = rng.next_gaussian(vdef.rate_lo, vdef.rate_hi)
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


def format_resonance_final(scaled_value, field: str, eff: dict, locale=None) -> str:
    """共鸣后目标参数的展示值；locale 为 None 时（引擎战报）不带单位词。"""
    fmt = _res_spec(eff, field)[0]
    text = format_field(scaled_value, fmt)
    if locale is None:
        return text
    if fmt == "num":
        return render_template(locale.stats.get("final_damage", "{v}"),
                               {"v": text}, locale)
    if fmt == "turns":
        return render_template(locale.stats.get("final_turns", "{v}"),
                               {"v": text}, locale)
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


def _nat_params(display_eff: dict):
    """效果类型 -> (模板键, 模板参数)。数值均为共鸣估算后的展示值：
    百分数 2 位小数（format_pct），其余取整（format_num）。
    模板参数名与效果字段名一致，便于共鸣公式内联注入。"""
    ttype = display_eff.get("type")
    chance = float(display_eff.get("chance", 1.0))
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
            "damage": format_num(float(display_eff.get("damage", 0))),
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
            "delay": format_num(float(display_eff.get("delay", 0)))}
    if ttype == "gauge_surge":
        return "nat_gauge_surge", {
            "chance": format_pct(chance),
            "value": format_num(float(display_eff.get("value", 0))),
            "crit_value": format_num(float(display_eff.get("crit_value", 0)))}
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
            "value": format_num(float(display_eff.get("value", 0))),
            "regen": format_num(float(display_eff.get("regen", 0))),
            "tick": int(display_eff.get("tick", 0)),
            "duration": int(display_eff.get("duration", 0))}
    if ttype == "cleanse":
        return "nat_cleanse", {
            "chance": format_pct(chance),
            "value": format_num(float(display_eff.get("value", 0))),
            "per": format_num(float(display_eff.get("per", 0)))}
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
            "value": format_num(float(display_eff.get("value", 0))),
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
            "value": format_num(float(display_eff.get("value", 0)))}
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


def _link_formula(eff: dict, link: dict, field: str, game: GameCfg, locale):
    """共鸣描述两部分（v0.9.0）：

    1. 内联最简线性公式（紧跟对应数值）：最终值 = 基数 + 变量式 * 合并系数；
       变量式以属性 emoji 表示——own 省略范围词、enemy 前缀「对方」、
       difference「【己方-对方】」、sum「【己方+对方】」；
    2. 尾句依赖描述（使用属性全名，如「己方攻击越高，效果值越高。」）。
    """
    tmpl = locale.stats
    var_id = str(link.get("variable"))
    attr_loc = locale.attributes.get(var_id, {})
    var_name = str(attr_loc.get("name", var_id))
    var_emoji = str(attr_loc.get("emoji", var_name))
    try:
        base = max(1, game.attr(var_id).base)
    except Exception:
        base = 1
    mode = str(link.get("mode", "own"))
    scope_own = str(tmpl.get("scope_own", ""))
    scope_enemy = str(tmpl.get("scope_enemy", ""))
    field_word = str(tmpl.get("field_" + field, field))
    fmt = _res_spec(eff, field)[0]
    base_display = format_field(eff.get(field, 0.0), fmt)
    merged = format_pct(float(eff.get(field, 0.0)) * float(link.get("rate", 0.0)) / base)
    against = var_id
    if mode in ("difference", "sum"):
        vdef = next((v for v in game.skill_variable_link.variables if v.id == var_id), None)
        against = vdef.diff_against if vdef else var_id
        against_name = str(locale.attributes.get(against, {}).get("name", against))
        expr_tmpl = "link_expr_difference" if mode == "difference" else "link_expr_sum"
        expr = render_template(tmpl.get(expr_tmpl, "{emoji}"),
                               {"own": scope_own, "enemy": scope_enemy,
                                "emoji": var_emoji}, locale)
        tail_tmpl = "link_difference" if mode == "difference" else "link_sum"
        tail = render_template(tmpl.get(tail_tmpl, ""),
                               {"own": scope_own + var_name,
                                "enemy": scope_enemy + against_name,
                                "field": field_word}, locale)
    elif mode == "enemy":
        expr = scope_enemy + var_emoji
        tail = render_template(tmpl.get("link_ratio", ""),
                               {"scope": scope_enemy, "stat": var_name,
                                "field": field_word}, locale)
    else:
        expr = var_emoji
        tail = render_template(tmpl.get("link_ratio", ""),
                               {"scope": scope_own, "stat": var_name,
                                "field": field_word}, locale)
    formula = render_template(tmpl.get("link_formula", ""),
                              {"base": base_display, "expr": expr, "merged": merged},
                              locale)
    return formula, tail


def _natural_text(eff: dict, fighter: Fighter, game: GameCfg, locale,
                  simple: bool = False, live: bool = False) -> str:
    """标准化自然语言描述。参数为共鸣估算后的最终值（敌方按基础值估算）：

    - 每个共鸣字段的公式括号紧跟该数值（simple 模式隐藏公式）；
    - live=True 时共鸣数值位替换为 LIVE_MARKER，供前端按快照实时填充。
    """
    display_eff, _ = estimated_resonanced_eff(fighter, eff, game)
    key, params = _nat_params(display_eff)
    params = dict(params)
    tails = []
    for link in eff.get("links", ()):
        field = str(link.get("field"))
        if field not in params:
            continue
        formula, tail = _link_formula(eff, link, field, game, locale)
        final = str(params[field])
        if live:
            params[field] = LIVE_MARKER + ("" if simple else formula)
        elif simple:
            params[field] = final
        else:
            params[field] = final + formula
        if tail:
            tails.append(tail)
    text = render_template(locale.stats.get(key, key), params, locale)
    for tail in tails:
        text += tail
    if not text.endswith("。") and text:
        text = text + "。"
    return text


def _link_calc(eff: dict, game: GameCfg) -> list:
    """对战实时技能数据（每个共鸣变数一条）：前端按公式
    最终值 = base + 变量式 × coeff（含上下限）用双方快照逐刻重算，
    与引擎 resonance_coeff + apply_resonance 完全一致，按槽位顺序排列。"""
    out = []
    for link in eff.get("links", ()):
        field = str(link.get("field"))
        if field not in eff:
            continue
        var_id = str(link.get("variable"))
        try:
            base = max(1, game.attr(var_id).base)
        except Exception:
            base = 1
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


def _mod_texts(eff: dict, game: GameCfg, locale) -> list:
    """词缀修正的可读文案（显示个性化缩放后的实际值），如「疾风：触发率 +3%」。"""
    texts = []
    for pool, key, scale_key in ((game.skill_name_modifiers.prefixes, "prefix", "prefix_scale"),
                                 (game.skill_name_modifiers.suffixes, "suffix", "suffix_scale")):
        mod_id = eff.get(key)
        if not mod_id:
            continue
        mdef = next((m for m in pool if m.id == mod_id), None)
        if mdef is None:
            continue
        scale = float(eff.get(scale_key, 1.0))
        parts = []
        for param, delta in mdef.mod.items():
            template_key = _MOD_TEMPLATES.get(param)
            if not template_key:
                continue
            scaled = float(delta) * scale
            sign = "+" if scaled > 0 else "-"
            magnitude = (format_pct(abs(scaled)) if param in ("chance", "value")
                         else format_num(abs(scaled)))
            parts.append(render_template(locale.stats.get(template_key, template_key),
                                         {"v": sign + magnitude}, locale))
        if parts:
            name = locale.modifiers.get(key + "es", {}).get(mod_id, {}).get("name", mod_id)
            texts.append(name + "：" + "，".join(parts))
    return texts


def _mastery_text(eff: dict, sdef, locale) -> str:
    """熟练度文案：「熟练度 63：触发率 ×121.00%」（条件型技能为效果值）。"""
    mastery = eff.get("mastery")
    if mastery is None:
        return ""
    key = "mastery_text_value" if sdef.mastery_on != "chance" else "mastery_text"
    return render_template(locale.stats.get(key, ""),
                           {"v": int(mastery),
                            "mult": format_pct(float(eff.get("mastery_mult", 1.0)))},
                           locale)


def _find_structure(fighter: Fighter, game: GameCfg):
    for s in game.title_structures:
        if s.id == fighter.title_structure_id:
            return s
    return None


def _format_bonus(value: int, attr_format: str) -> str:
    sign = "+" if value > 0 else ""
    if attr_format == "percent":
        return "%s%s%%" % (sign, value)
    return "%s%s" % (sign, value)


def _title_bonus_api(fighter: Fighter, game: GameCfg, locale) -> dict:
    """称号加成的对外表示：按属性配置顺序聚合，供卡牌展示。"""
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
        name = locale.attributes.get(a.id, {}).get("name", a.id)
        text = _format_bonus(sums[a.id], a.format)
        bonuses.append({"attr": a.id, "name": name, "value": sums[a.id], "format": a.format})
        parts.append("%s %s" % (name, text))
    return {"bonuses": bonuses, "bonuses_text": " · ".join(parts)}


def compose_title_name(fighter: Fighter, game: GameCfg, locale) -> str:
    """按结构与连接符拼接称号显示名。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return fighter.title_structure_id
    parts = []
    for fname in structure.fields:
        fid = fighter.title_fields.get(fname)
        entry = locale.titles.get(_FIELD_LOCALE[fname], {}).get(fid, {})
        parts.append(str(entry.get("name", fid or "")))
    if not parts:
        return ""
    out = parts[0]
    for i in range(1, len(parts)):
        connector = structure.connectors[i - 1] if i - 1 < len(structure.connectors) else ""
        out += connector + parts[i]
    return out


def compose_title_desc(fighter: Fighter, game: GameCfg, locale) -> str:
    """把各字段的描述片段用「，」连接成称号描述。"""
    structure = _find_structure(fighter, game)
    if structure is None:
        return ""
    frags = []
    for fname in structure.fields:
        fid = fighter.title_fields.get(fname)
        entry = locale.titles.get(_FIELD_LOCALE[fname], {}).get(fid, {})
        desc = entry.get("desc", "")
        if desc:
            frags.append(str(desc))
    return "，".join(frags) + "。" if frags else ""


def fighter_to_api(fighter: Fighter, game: GameCfg, locale) -> dict:
    """斗士数据的对外表示：数值来自 Fighter/个性化效果，显示名全部来自 locale。"""
    attrs_api = []
    for a in game.attributes:
        a_loc = locale.attributes.get(a.id, {})
        attrs_api.append({
            "id": a.id,
            "name": a_loc.get("name", a.id),
            "emoji": a_loc.get("emoji", ""),
            "value": fighter.attrs[a.id],
            "min": a.min,
            "max": a.max,
            "format": a.format,
        })
    skills_api = []
    for sdef, eff in personalized_effects(fighter, game):
        entry = locale.skills.get(sdef.id, {})
        sep = str(locale.stats.get("link_sep", "·"))
        name = str(entry.get("name", sdef.id))
        mod_names = locale.modifiers
        if eff.get("prefix"):
            pname = mod_names.get("prefixes", {}).get(eff["prefix"], {}).get("name")
            if pname:
                name = pname + sep + name
        if eff.get("suffix"):
            sname = mod_names.get("suffixes", {}).get(eff["suffix"], {}).get("name")
            if sname:
                name = name + sep + sname
        links = eff.get("links", ())
        for link in links:
            marker = locale.stats.get("link_" + str(link.get("variable")))
            if marker:
                name = name + sep + str(marker)
        link_api = [{
            "field": str(link.get("field")),
            "variable": link.get("variable"),
            "name": locale.attributes.get(link.get("variable"), {}).get(
                "name", link.get("variable")),
            "mode": link.get("mode", "own"),
            "rate": link.get("rate", 0),
        } for link in links]
        skill_entry = {
            "id": sdef.id,
            "name": name,
            "flavor": entry.get("description", ""),
            "text": _natural_text(eff, fighter, game, locale),
            "text_simple": _natural_text(eff, fighter, game, locale, simple=True),
            "modifiers": _mod_texts(eff, game, locale),
            "mastery": int(eff.get("mastery", 0)),
            "mastery_text": _mastery_text(eff, sdef, locale),
            "link": link_api if link_api else None,
        }
        if links:
            skill_entry["live_text"] = _natural_text(eff, fighter, game, locale, live=True)
            skill_entry["live_text_simple"] = _natural_text(
                eff, fighter, game, locale, simple=True, live=True)
            skill_entry["link_calc"] = _link_calc(eff, game)
        skills_api.append(skill_entry)
    elem = locale.elements.get(fighter.element_id, {})
    title_bonus = _title_bonus_api(fighter, game, locale)
    return {
        "name": fighter.name,
        "normalized": fighter.normalized,
        "digest": fighter.digest,
        "digest_short": fighter.digest[:8],
        "element": {"id": fighter.element_id, "name": elem.get("name", fighter.element_id),
                    "emoji": elem.get("emoji", "")},
        "title": {
            "structure": fighter.title_structure_id,
            "name": compose_title_name(fighter, game, locale),
            "description": compose_title_desc(fighter, game, locale),
            "bonuses": title_bonus["bonuses"],
            "bonuses_text": title_bonus["bonuses_text"],
        },
        "attributes": attrs_api,
        "skills": skills_api,
        "power": fighter.power,
    }
