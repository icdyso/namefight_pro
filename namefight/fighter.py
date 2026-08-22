"""斗士派生：名字 -> MD5 -> 属性 / 技能 / 称号 / 元素 / 稀有度。

确定性契约（AGENTS.md 2.1.1）：派生结果是 (归一化名字, 配置快照) 的纯函数。

- 主派生 PRNG 消耗顺序固定：稀有度 -> 元素 -> 属性（配置顺序）-> 技能数量
  -> 技能抽取 -> 称号结构 -> 称号字段（按结构字段顺序）。
- 技能个性化（概率/数值随 MD5 扰动）使用独立种子
  md5(规范化名字 + ":" + 技能id)，与主派生流互不影响。
- 称号为多字段组合：按结构（前缀+核心+后缀 / 双核心 等）概率生成。
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
    "link_sep", "link_ratio", "link_difference",
    "scope_own", "scope_enemy", "mode_ratio", "mode_difference",
    "mod_chance", "mod_value", "mod_damage", "mod_turns",
    "field_chance", "field_value", "field_damage", "field_turns",
    "final_damage", "final_turns",
    "nat_damage_multiplier", "nat_execution", "nat_lifesteal", "nat_poison",
    "nat_stun", "nat_extra_strikes", "nat_damage_reduction", "nat_reflect",
    "nat_dodge_bonus", "nat_crit_bonus", "nat_heal", "nat_low_hp_atk_bonus",
})


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
    """词缀修正：仅作用于技能已有的参数（chance 截断到 [0.02, 0.95]，turns 至少 1）。"""
    for key, delta in mod.items():
        if key not in eff:
            continue
        if key == "chance":
            eff["chance"] = min(0.95, max(0.02, float(eff["chance"]) + float(delta)))
        elif key == "turns":
            eff["turns"] = max(1, int(round(float(eff["turns"]))) + int(round(float(delta))))
        else:
            eff[key] = round(float(eff[key]) + float(delta), 4)


def personalized_effects(fighter: Fighter, game: GameCfg):
    """技能个性化：以 md5(规范化名字:技能id) 为种子做确定性扰动。

    消耗顺序固定：
    chance -> value -> damage -> 前缀(是否 -> 抽取 -> 缩放) -> 后缀(是否 -> 抽取 -> 缩放)
    -> 共鸣(是否 -> 来源 -> 模式 -> 变量 -> 倍率)。

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
        chance = float(eff.get("chance", 1.0))
        if 0.0 < chance < 1.0:
            factor = rng.next_gaussian(var.chance_lo, var.chance_hi)
            eff["chance"] = min(0.95, max(0.02, round(chance * factor, 4)))
        for key in ("value", "damage"):
            if key in eff:
                factor = rng.next_gaussian(var.value_lo, var.value_hi)
                eff[key] = round(float(eff[key]) * factor, 4)
        if name_mod.prefix_chance > 0 and rng.next_float() < name_mod.prefix_chance:
            eff["prefix"] = rng.pick_weighted((m, m.weight) for m in name_mod.prefixes).id
            eff["prefix_scale"] = round(rng.next_gaussian(name_mod.scale_lo, name_mod.scale_hi), 4)
        if name_mod.suffix_chance > 0 and rng.next_float() < name_mod.suffix_chance:
            eff["suffix"] = rng.pick_weighted((m, m.weight) for m in name_mod.suffixes).id
            eff["suffix_scale"] = round(rng.next_gaussian(name_mod.scale_lo, name_mod.scale_hi), 4)
        for pool, mod_id, scale_key in ((name_mod.prefixes, eff.get("prefix"), "prefix_scale"),
                                        (name_mod.suffixes, eff.get("suffix"), "suffix_scale")):
            if not mod_id:
                continue
            mdef = next((m for m in pool if m.id == mod_id), None)
            if mdef is not None:
                scaled = {k: v * float(eff.get(scale_key, 1.0)) for k, v in mdef.mod.items()}
                _apply_modifier(eff, scaled)
        if link_cfg.chance > 0 and eff.get("type") in link_cfg.linkable_types:
            if rng.next_float() < link_cfg.chance:
                source = rng.pick_weighted(link_cfg.source_weights)
                mode = rng.pick_weighted(link_cfg.mode_weights)
                vdef = rng.pick_weighted((v, v.weight) for v in link_cfg.variables)
                rate = rng.next_gaussian(vdef.rate_lo, vdef.rate_hi)
                eff["link"] = {"variable": vdef.id, "rate": round(rate, 4),
                               "source": source, "mode": mode}
        out.append((sdef, eff))
    return out


def resonance_target(eff: dict, game: GameCfg):
    """该技能共鸣修正的参数字段（如 stun -> chance）；无共鸣返回 None。"""
    link = eff.get("link")
    if not link:
        return None
    return game.skill_variable_link.targets.get(str(eff.get("type")))


def resonance_coeff(own_get, enemy_get, link: dict, game: GameCfg) -> float:
    """归一化共鸣系数（不改变技能逻辑，只按比例修正技能自身参数）：

    - 比例模式：coeff = rate × (源方[变量]当前值 ÷ 变量基础值)
    - 差值模式：coeff = rate × ((己方[变量] − 敌方[参照]) ÷ 变量基础值)，可为负

    own_get/enemy_get 为「变量id -> 数值」的取值函数；基础值归一化保证
    不同量纲的变量（攻击 13 / 生命 100 等）产生可比的修正幅度。
    """
    vid = link.get("variable")
    try:
        base = max(1, game.attr(vid).base)
    except Exception:
        base = 1
    rate = float(link.get("rate", 0.0))
    if link.get("mode") == "difference":
        vdef = next((v for v in game.skill_variable_link.variables if v.id == vid), None)
        against = vdef.diff_against if vdef else vid
        raw = float(own_get(vid)) - float(enemy_get(against))
    else:
        src = enemy_get if link.get("source") == "enemy" else own_get
        raw = float(src(vid))
    return rate * (raw / base)


def apply_resonance(eff: dict, coeff: float, target: str) -> dict:
    """按共鸣系数缩放目标参数，返回新的效果 dict（带各自的上下限截断）。"""
    scaled = dict(eff)
    if target not in scaled:
        return scaled
    factor = 1.0 + coeff
    if target == "chance":
        scaled["chance"] = min(0.95, max(0.02, round(float(scaled["chance"]) * factor, 4)))
    elif target == "turns":
        scaled["turns"] = max(1, int(round(float(scaled["turns"]) * factor)))
    elif target == "damage":
        scaled["damage"] = max(0.0, round(float(scaled["damage"]) * factor, 4))
    else:  # value
        scaled["value"] = min(5.0, max(0.05, round(float(scaled["value"]) * factor, 4)))
    return scaled


def format_resonance_final(scaled_value, target: str, locale=None) -> str:
    """共鸣后目标参数的展示值；locale 为 None 时（引擎战报）不带单位词。"""
    if target == "chance" or target == "value":
        return format_pct(float(scaled_value))
    if target == "damage":
        value = int(round(float(scaled_value)))
        if locale is None:
            return str(value)
        return render_template(locale.stats.get("final_damage", "{v}"),
                               {"v": value}, locale)
    if target == "turns":
        value = int(round(float(scaled_value)))
        if locale is None:
            return str(value)
        return render_template(locale.stats.get("final_turns", "{v}"),
                               {"v": value}, locale)
    return format_num(float(scaled_value))


def estimated_resonanced_eff(fighter: Fighter, eff: dict, game: GameCfg):
    """卡牌展示用的共鸣估算：敌方取基础值（实际战斗中按当前值动态计算）。
    返回 (估算后的效果dict, 系数, 目标字段)；无共鸣时返回 (eff, 0.0, None)。"""
    target = resonance_target(eff, game)
    if not target:
        return eff, 0.0, None
    base_get = lambda vid: game.attr(vid).base  # noqa: E731
    coeff = resonance_coeff(lambda vid: fighter.attrs.get(vid, 0), base_get,
                            eff["link"], game)
    return apply_resonance(eff, coeff, target), coeff, target


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


def _natural_text(eff: dict, fighter: Fighter, game: GameCfg, locale) -> str:
    """标准化自然语言描述：参数为共鸣修正后的最终值（敌方按基础值估算），
    共鸣部分追加「XX越XX / XX高于XX越多」句式与内联公式。"""
    display_eff, coeff, target = estimated_resonanced_eff(fighter, eff, game)
    tmpl = locale.stats
    ttype = display_eff.get("type")
    chance = float(display_eff.get("chance", 1.0))
    key = "nat_" + str(ttype)
    params = {}
    if ttype == "damage_multiplier":
        cond = display_eff.get("condition")
        if cond:
            key = "nat_execution"
            params = {"chance": format_pct(chance), "mult": format_pct(float(display_eff.get("value", 1.0))),
                      "threshold": format_pct(float(cond.get("value", 0)))}
        else:
            params = {"chance": format_pct(chance), "mult": format_pct(float(display_eff.get("value", 1.0)))}
    elif ttype == "lifesteal":
        params = {"value": format_pct(float(display_eff.get("value", 0.0)))}
    elif ttype == "poison":
        params = {"chance": format_pct(chance),
                  "damage": int(round(float(display_eff.get("damage", 0)))),
                  "turns": int(display_eff.get("turns", 0))}
    elif ttype == "stun":
        params = {"chance": format_pct(chance)}
    elif ttype == "extra_strikes":
        ratios = display_eff.get("ratios", [])
        params = {"chance": format_pct(chance),
                  "extra": format_pct(float(ratios[0]) if ratios else 0.0)}
    elif ttype == "damage_reduction":
        params = {"value": format_pct(float(display_eff.get("value", 0.0)))}
    elif ttype == "reflect":
        params = {"value": format_pct(float(display_eff.get("value", 0.0)))}
    elif ttype == "dodge_bonus":
        params = {"value": format_num(float(display_eff.get("value", 0.0)))}
    elif ttype == "crit_bonus":
        params = {"value": format_num(float(display_eff.get("value", 0.0)))}
    elif ttype == "heal":
        params = {"chance": format_pct(chance),
                  "value": int(round(float(display_eff.get("value", 0))))}
    elif ttype == "low_hp_atk_bonus":
        params = {"threshold": format_pct(float(display_eff.get("threshold", 0.3))),
                  "value": format_pct(float(display_eff.get("value", 0.5)))}
    text = render_template(tmpl.get(key, key), params, locale)
    if target:
        clause = _link_clause(eff["link"], target, display_eff, coeff, game, locale)
        if clause:
            text = text + clause
    return text


def _link_clause(link: dict, target: str, display_eff: dict, coeff: float,
                 game: GameCfg, locale) -> str:
    """共鸣描述：「XX越XX / XX高于XX越多」句式 + 归一化公式 + 当前最终值。"""
    tmpl = locale.stats
    var_id = link.get("variable")
    var_name = locale.attributes.get(var_id, {}).get("name", var_id)
    base = game.attr(var_id).base if var_id else 1
    scope_own = str(tmpl.get("scope_own", ""))
    scope_enemy = str(tmpl.get("scope_enemy", ""))
    rate = format_pct(float(link.get("rate", 0)))
    field = str(tmpl.get("field_" + target, target))
    final = format_resonance_final(display_eff.get(target), target, locale)
    if link.get("mode") == "difference":
        vdef = next((v for v in game.skill_variable_link.variables if v.id == var_id), None)
        against = vdef.diff_against if vdef else var_id
        against_name = locale.attributes.get(against, {}).get("name", against)
        return render_template(tmpl.get("link_difference", ""),
                               {"own": scope_own + var_name,
                                "enemy": scope_enemy + against_name,
                                "base": base, "field": field,
                                "pct": rate, "final": final}, locale)
    scope = scope_enemy if link.get("source") == "enemy" else scope_own
    stat_full = scope + var_name
    return render_template(tmpl.get("link_ratio", ""),
                           {"stat": stat_full, "base": base, "field": field,
                            "pct": rate, "final": final}, locale)


_MOD_TEMPLATES = {"chance": "mod_chance", "value": "mod_value",
                  "damage": "mod_damage", "turns": "mod_turns"}


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
                         else str(int(round(abs(scaled)))))
            parts.append(render_template(locale.stats.get(template_key, template_key),
                                         {"v": sign + magnitude}, locale))
        if parts:
            name = locale.modifiers.get(key + "es", {}).get(mod_id, {}).get("name", mod_id)
            texts.append(name + "：" + "，".join(parts))
    return texts


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
        attrs_api.append({
            "id": a.id,
            "name": locale.attributes.get(a.id, {}).get("name", a.id),
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
        link = eff.get("link")
        if link:
            marker = locale.stats.get("link_" + link["variable"])
            if marker:
                name = name + sep + str(marker)
        link_api = None
        if link:
            link_api = {
                "variable": link["variable"],
                "name": locale.attributes.get(link["variable"], {}).get("name", link["variable"]),
                "source": link.get("source", "own"),
                "mode": link.get("mode", "ratio"),
                "rate": link.get("rate", 0),
            }
        skills_api.append({
            "id": sdef.id,
            "name": name,
            "flavor": entry.get("description", ""),
            "text": _natural_text(eff, fighter, game, locale),
            "modifiers": _mod_texts(eff, game, locale),
            "link": link_api,
        })
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
