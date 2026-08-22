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
    "chance", "mult", "lifesteal", "poison_damage", "poison_turns", "stun",
    "extra", "reduction", "reflect", "dodge", "crit", "heal", "threshold",
    "atk_bonus",
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
    rarity_id: str
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

    rarity = rng.pick_weighted((r, r.weight) for r in game.rarities)
    element = rng.pick_weighted((e, e.weight) for e in game.elements)

    attrs = {}
    for a in game.attributes:
        attrs[a.id] = rng.next_range(a.min, a.max)
    for attr_id in game.rarity_scaled_attributes:
        mult = rarity.multipliers.get(attr_id, 1.0)
        attrs[attr_id] = max(1, round(attrs[attr_id] * mult))

    count = rng.next_range(game.skill_count_min, game.skill_count_max)
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

    power = round(sum(attrs[a.id] * a.power_weight for a in game.attributes))
    return Fighter(
        name=raw_name if isinstance(raw_name, str) and raw_name else normalized,
        normalized=normalized, digest=digest,
        rarity_id=rarity.id, element_id=element.id, attrs=attrs,
        skill_ids=tuple(s.id for s in skills),
        title_structure_id=structure.id, title_fields=title_fields, power=power,
    )


def personalized_effects(fighter: Fighter, game: GameCfg):
    """技能个性化：以 md5(规范化名字:技能id) 为种子，对触发概率与数值施加
    确定性扰动（区间见 config/game/skills.json 的 md5_variance）。

    返回 [(SkillDef, 个性化后的效果dict), ...]，顺序与 fighter.skill_ids 一致。
    """
    var = game.skill_md5_variance
    out = []
    for sid in fighter.skill_ids:
        sdef = next(s for s in game.skills if s.id == sid)
        eff = dict(sdef.effect)
        seed_hex = hashlib.md5((fighter.normalized + ":" + sid).encode("utf-8")).hexdigest()
        rng = DetRng(int(seed_hex, 16))
        chance = float(eff.get("chance", 1.0))
        if 0.0 < chance < 1.0:
            factor = var.chance_lo + rng.next_float() * (var.chance_hi - var.chance_lo)
            eff["chance"] = min(0.95, max(0.02, round(chance * factor, 4)))
        for key in ("value", "damage"):
            if key in eff:
                factor = var.value_lo + rng.next_float() * (var.value_hi - var.value_lo)
                eff[key] = round(float(eff[key]) * factor, 4)
        out.append((sdef, eff))
    return out


def _skill_stats(eff: dict, locale) -> list:
    """把个性化后的技能参数渲染为可读文案列表（模板来自 locale.stats）。"""
    tmpl = locale.stats

    def stat(key, value):
        return render_template(tmpl.get(key, key), {"v": value}, locale)

    stats = []
    ttype = eff.get("type")
    chance = float(eff.get("chance", 1.0))
    if chance < 1.0:
        stats.append(stat("chance", format_pct(chance)))
    if ttype == "damage_multiplier":
        stats.append(stat("mult", format_pct(float(eff.get("value", 1.0)))))
    elif ttype == "lifesteal":
        stats.append(stat("lifesteal", format_pct(float(eff.get("value", 0.0)))))
    elif ttype == "poison":
        stats.append(stat("poison_damage", int(round(float(eff.get("damage", 0))))))
        stats.append(stat("poison_turns", int(eff.get("turns", 0))))
    elif ttype == "stun":
        stats.append(stat("stun", ""))
    elif ttype == "extra_strikes":
        ratios = eff.get("ratios", [])
        stats.append(stat("extra", format_pct(float(ratios[0]) if ratios else 0.0)))
    elif ttype == "damage_reduction":
        stats.append(stat("reduction", format_pct(float(eff.get("value", 0.0)))))
    elif ttype == "reflect":
        stats.append(stat("reflect", format_pct(float(eff.get("value", 0.0)))))
    elif ttype == "dodge_bonus":
        stats.append(stat("dodge", format_num(float(eff.get("value", 0.0)))))
    elif ttype == "crit_bonus":
        stats.append(stat("crit", format_num(float(eff.get("value", 0.0)))))
    elif ttype == "heal":
        stats.append(stat("heal", int(round(float(eff.get("value", 0))))))
    elif ttype == "low_hp_atk_bonus":
        stats.append(stat("threshold", format_pct(float(eff.get("threshold", 0.3)))))
        stats.append(stat("atk_bonus", format_pct(float(eff.get("value", 0.5)))))
    return [s for s in stats if s]


def _find_structure(fighter: Fighter, game: GameCfg):
    for s in game.title_structures:
        if s.id == fighter.title_structure_id:
            return s
    return None


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
        skills_api.append({
            "id": sdef.id,
            "name": entry.get("name", sdef.id),
            "description": entry.get("description", ""),
            "stats": _skill_stats(eff, locale),
        })
    rar_def = next(r for r in game.rarities if r.id == fighter.rarity_id)
    rar = locale.rarities.get(fighter.rarity_id, {})
    elem = locale.elements.get(fighter.element_id, {})
    return {
        "name": fighter.name,
        "normalized": fighter.normalized,
        "digest": fighter.digest,
        "digest_short": fighter.digest[:8],
        "rarity": {"id": fighter.rarity_id, "name": rar.get("name", fighter.rarity_id), "stars": rar_def.stars},
        "element": {"id": fighter.element_id, "name": elem.get("name", fighter.element_id),
                    "emoji": elem.get("emoji", "")},
        "title": {
            "structure": fighter.title_structure_id,
            "name": compose_title_name(fighter, game, locale),
            "description": compose_title_desc(fighter, game, locale),
        },
        "attributes": attrs_api,
        "skills": skills_api,
        "power": fighter.power,
    }
