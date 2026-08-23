"""配置加载与校验。

配置分两层（见 AGENTS.md 2.2）：
- config/game/      数值与规则（与语言无关）
- config/locales/   文案（与数值无关）

启动时一次性加载并校验；修改配置后需重启进程。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 引擎依赖的属性 id（battle.py 直接按这些 key 取值，配置必须提供）
REQUIRED_ATTRIBUTE_IDS = ("hp", "atk", "def", "spd", "crit", "dodge")

# 称号结构允许引用的字段名 -> 数值池名（core2 与 core 共用核心池）
TITLE_FIELD_POOLS = {"prefix": "prefix", "core": "core", "core2": "core", "suffix": "suffix"}


class ConfigError(Exception):
    """配置文件缺失或非法。"""


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError("缺少配置文件: %s" % path)
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError("配置文件 JSON 解析失败: %s (%s)" % (path, e)) from e


@dataclass(frozen=True)
class SystemCfg:
    version: str
    default_locale: str
    available_locales: tuple
    name_trim: bool
    name_case_sensitive: bool
    name_min_length: int
    name_max_length: int


@dataclass(frozen=True)
class AttributeDef:
    id: str
    base: float         # 名义基准值（共鸣归一化等使用；投掷以 min/max 区间为准）
    min: float          # 正态投掷区间下限
    max: float          # 正态投掷区间上限（display_ref 缺省时的显示换算参照）
    format: str         # int / percent，仅影响展示
    power_weight: float # 战力权重
    display_ref: float  # 显示层换算参照（满投掷 = 100）；0 表示原值直显


@dataclass(frozen=True)
class SkillDef:
    id: str
    weight: float
    trigger: str         # on_attack / on_defense / on_turn_start / passive
    effect: dict         # {type: 效果类型, ...参数}
    mastery: tuple       # 熟练度 -> 倍率区间 (lo, hi)，作用于 mastery_on 字段
    mastery_on: str      # 熟练度作用的参数（默认 chance；条件触发型技能可为 value 等）


@dataclass(frozen=True)
class SkillVariance:
    """技能个性化（MD5 扰动）区间：数值字段的倍率范围（触发概率由熟练度系统负责）。"""
    value_lo: float
    value_hi: float


@dataclass(frozen=True)
class VariableLinkDef:
    """可共鸣变量：变量 id、抽取权重、共鸣倍率区间、差值/求和模式的参照属性。"""
    id: str
    weight: float
    rate_lo: float
    rate_hi: float
    diff_against: str    # difference / sum 模式：与敌方哪个属性运算


@dataclass(frozen=True)
class SkillLinkCfg:
    """技能变量共鸣配置（v0.9.0 起每技能两个变数槽位）：

    - chance：单个槽位实际成为变数的概率；
    - mode_weights：own（己方单值）/ enemy（敌方单值）/ difference（差值）/ sum（并值）权重；
    - targets：效果类型 -> 可共鸣参数字段列表（依序为槽位一 / 槽位二）；
    - variables：可共鸣的属性变量池。"""
    chance: float
    variables: tuple         # (VariableLinkDef, ...)
    mode_weights: tuple      # (("own", w), ...)
    targets: dict            # {效果类型: (字段名, ...)}


VALID_LINK_MODES = ("own", "enemy", "difference", "sum")


@dataclass(frozen=True)
class NameModifierDef:
    """技能名称词缀（前缀/后缀）：附带对技能已有参数的小幅修正（数值可个性化缩放）。"""
    id: str
    weight: float
    mod: dict = field(default_factory=dict)  # {参数名: 增量}，仅作用于技能已有参数


@dataclass(frozen=True)
class SkillNameModCfg:
    """技能词缀配置：前缀/后缀获得概率、词缀池、修正值的高斯缩放区间。"""
    prefix_chance: float
    suffix_chance: float
    prefixes: tuple
    suffixes: tuple
    scale_lo: float
    scale_hi: float


@dataclass(frozen=True)
class TitleFieldDef:
    id: str
    weight: float
    bonus: dict = field(default_factory=dict)  # {属性id: 小额加成（可为负）}


@dataclass(frozen=True)
class TitleStructureDef:
    id: str
    weight: float
    fields: tuple        # (字段名, ...)，取值见 TITLE_FIELD_POOLS
    connectors: tuple    # 连接符，长度 = len(fields) - 1


@dataclass(frozen=True)
class BattleCfg:
    crit_multiplier: float
    variance_lo: float
    variance_hi: float
    atk_factor: float       # 攻击换算系数（白板 100 攻击下的实际攻击当量）
    defense_factor: float
    min_damage: int
    max_ticks: int
    gauge_threshold: float   # 行动槽阈值：每 tick 累加速度值，满阈值即可行动
    crit_cap: float          # 百分数上限
    dodge_cap: float         # 百分数上限
    seed_separator: str


@dataclass(frozen=True)
class GameCfg:
    system: SystemCfg
    attributes: tuple    # (AttributeDef, ...)，配置文件顺序
    skills: tuple
    title_structures: tuple
    title_pools: dict    # {"prefix": (TitleFieldDef,..), "core": ..., "suffix": ...}
    skill_count_min: int
    skill_count_max: int
    skill_md5_variance: SkillVariance
    skill_variable_link: SkillLinkCfg
    skill_name_modifiers: SkillNameModCfg
    battle: BattleCfg

    def attr(self, attr_id: str) -> AttributeDef:
        for a in self.attributes:
            if a.id == attr_id:
                return a
        raise ConfigError("未定义的属性: %s" % attr_id)


def load_game_config(config_root) -> GameCfg:
    root = Path(config_root)
    game = root / "game"
    sys_data = _read_json(game / "system.json")
    attrs_data = _read_json(game / "attributes.json")
    skills_data = _read_json(game / "skills.json")
    titles_data = _read_json(game / "titles.json")
    battle_data = _read_json(game / "battle.json")

    name_cfg = sys_data.get("name", {})
    system = SystemCfg(
        version=str(sys_data["version"]),
        default_locale=str(sys_data["default_locale"]),
        available_locales=tuple(sys_data["available_locales"]),
        name_trim=bool(name_cfg.get("trim", True)),
        name_case_sensitive=bool(name_cfg.get("case_sensitive", False)),
        name_min_length=int(name_cfg.get("min_length", 1)),
        name_max_length=int(name_cfg.get("max_length", 32)),
    )
    if not system.available_locales or system.default_locale not in system.available_locales:
        raise ConfigError("system.json 语言配置非法")

    attributes = []
    seen_attrs = set()
    for a in attrs_data.get("attributes", []):
        if a["id"] in seen_attrs:
            raise ConfigError("属性 id 重复: %s" % a["id"])
        seen_attrs.add(a["id"])
        base = float(a["base"])
        lo = float(a.get("min", base))
        hi = float(a.get("max", base))
        if lo > hi:
            raise ConfigError("属性投掷区间非法: %s" % a["id"])
        display_ref = float(a.get("display_ref", 0))
        if display_ref < 0:
            raise ConfigError("属性 display_ref 非法: %s" % a["id"])
        attributes.append(AttributeDef(
            id=str(a["id"]), base=base, min=lo, max=hi,
            format=str(a.get("format", "int")), power_weight=float(a.get("power_weight", 0)),
            display_ref=display_ref,
        ))
    missing = [i for i in REQUIRED_ATTRIBUTE_IDS if i not in seen_attrs]
    if missing:
        raise ConfigError("attributes.json 缺少引擎必需属性: %s" % missing)

    skills = []
    seen_skills = set()
    for s in skills_data.get("skills", []):
        if s["id"] in seen_skills:
            raise ConfigError("技能 id 重复: %s" % s["id"])
        if float(s.get("weight", 1)) <= 0:
            raise ConfigError("技能权重必须为正: %s" % s["id"])
        seen_skills.add(s["id"])
        mastery = s.get("mastery", [1.0, 1.0])
        if (len(mastery) != 2 or float(mastery[0]) <= 0
                or float(mastery[0]) > float(mastery[1])):
            raise ConfigError("技能 %s 的熟练度区间非法" % s["id"])
        skills.append(SkillDef(
            id=str(s["id"]), weight=float(s.get("weight", 1)),
            trigger=str(s.get("trigger", "passive")), effect=dict(s.get("effect", {})),
            mastery=(float(mastery[0]), float(mastery[1])),
            mastery_on=str(s.get("mastery_on", "chance")),
        ))
    if not skills:
        raise ConfigError("技能池为空")
    skill_count = skills_data.get("skill_count", {})
    sc_min = int(skill_count.get("min", 1))
    sc_max = int(skill_count.get("max", 1))
    if not (1 <= sc_min <= sc_max <= len(skills)):
        raise ConfigError("技能数量配置非法: [%s, %s]" % (sc_min, sc_max))

    var = skills_data.get("md5_variance", {})
    var_value = var.get("value", [1.0, 1.0])
    skill_md5_variance = SkillVariance(
        value_lo=float(var_value[0]), value_hi=float(var_value[1]),
    )
    if skill_md5_variance.value_lo > skill_md5_variance.value_hi:
        raise ConfigError("md5_variance.value 区间非法")

    # 技能变量共鸣
    link_data = skills_data.get("variable_link", {})
    link_variables = []
    for vid, spec in (link_data.get("variables", {}) or {}).items():
        if vid not in seen_attrs:
            raise ConfigError("共鸣变量引用了未定义的属性: %s" % vid)
        rate = spec.get("rate", [0.0, 0.0])
        rate_lo, rate_hi = float(rate[0]), float(rate[1])
        if rate_lo > rate_hi or rate_lo < 0:
            raise ConfigError("共鸣变量 %s 倍率区间非法" % vid)
        if float(spec.get("weight", 1)) <= 0:
            raise ConfigError("共鸣变量 %s 权重必须为正" % vid)
        diff_against = str(spec.get("diff_against", vid))
        if diff_against not in seen_attrs:
            raise ConfigError("共鸣变量 %s 的差值参照 %s 不是已定义属性" % (vid, diff_against))
        link_variables.append(VariableLinkDef(
            id=str(vid), weight=float(spec.get("weight", 1)),
            rate_lo=rate_lo, rate_hi=rate_hi, diff_against=diff_against,
        ))

    def _weight_pairs(mapping, name):
        pairs = []
        for key, weight in (mapping or {}).items():
            if float(weight) <= 0:
                raise ConfigError("%s 权重必须为正: %s" % (name, key))
            pairs.append((str(key), float(weight)))
        return tuple(pairs)

    mode_weights = _weight_pairs(link_data.get("mode_weights", {"own": 1}), "共鸣模式")
    for mode, _ in mode_weights:
        if mode not in VALID_LINK_MODES:
            raise ConfigError("共鸣模式非法: %s" % mode)
    targets = {}
    for effect_type, fields in (link_data.get("targets", {}) or {}).items():
        if isinstance(fields, str):
            fields = [fields]
        fields = tuple(str(x) for x in fields)
        if not fields or len(fields) > 2:
            raise ConfigError("共鸣目标字段应为 1~2 个: %s" % effect_type)
        targets[str(effect_type)] = fields
    skill_variable_link = SkillLinkCfg(
        chance=float(link_data.get("chance", 0)),
        variables=tuple(link_variables),
        mode_weights=mode_weights or (("own", 1.0),),
        targets=targets,
    )
    if not 0.0 <= skill_variable_link.chance <= 1.0:
        raise ConfigError("variable_link.chance 必须在 [0, 1]")
    if skill_variable_link.chance > 0 and not link_variables:
        raise ConfigError("variable_link.chance > 0 但变量池为空")
    if skill_variable_link.chance > 0 and not targets:
        raise ConfigError("variable_link.chance > 0 但 targets 为空")

    # 技能名称词缀（前缀/后缀，附带微小参数修正）
    mod_data = skills_data.get("name_modifiers", {})

    def _load_mod_pool(pool_data, label):
        pool = []
        seen_mods = set()
        for entry in (pool_data or []):
            mid = str(entry["id"])
            if mid in seen_mods:
                raise ConfigError("词缀 id 重复: %s/%s" % (label, mid))
            seen_mods.add(mid)
            if float(entry.get("weight", 1)) <= 0:
                raise ConfigError("词缀权重必须为正: %s/%s" % (label, mid))
            mod = {}
            for key, delta in entry.get("mod", {}).items():
                if not isinstance(delta, (int, float)) or isinstance(delta, bool):
                    raise ConfigError("词缀 %s/%s 的修正值必须为数字: %s" % (label, mid, key))
                mod[str(key)] = float(delta)
            pool.append(NameModifierDef(id=mid, weight=float(entry.get("weight", 1)), mod=mod))
        return tuple(pool)

    mod_prefixes = _load_mod_pool(mod_data.get("prefixes"), "prefix")
    mod_suffixes = _load_mod_pool(mod_data.get("suffixes"), "suffix")
    mod_scale = mod_data.get("mod_variance", [1.0, 1.0])
    if float(mod_scale[0]) > float(mod_scale[1]):
        raise ConfigError("mod_variance 区间非法")
    skill_name_modifiers = SkillNameModCfg(
        prefix_chance=float(mod_data.get("prefix_chance", 0)),
        suffix_chance=float(mod_data.get("suffix_chance", 0)),
        prefixes=mod_prefixes, suffixes=mod_suffixes,
        scale_lo=float(mod_scale[0]), scale_hi=float(mod_scale[1]),
    )
    for chance in (skill_name_modifiers.prefix_chance, skill_name_modifiers.suffix_chance):
        if not 0.0 <= chance <= 1.0:
            raise ConfigError("name_modifiers 概率必须在 [0, 1]")

    # 称号：多字段 + 多结构概率生成
    structures = []
    seen_structs = set()
    for s in titles_data.get("structures", []):
        sid = str(s["id"])
        if sid in seen_structs:
            raise ConfigError("称号结构 id 重复: %s" % sid)
        seen_structs.add(sid)
        fields = tuple(str(x) for x in s.get("fields", []))
        if not fields:
            raise ConfigError("称号结构 %s 未定义字段" % sid)
        for fname in fields:
            if fname not in TITLE_FIELD_POOLS:
                raise ConfigError("称号结构 %s 引用了未知字段 %s" % (sid, fname))
        connectors = tuple(str(x) for x in s.get("connectors", []))
        if connectors and len(connectors) != len(fields) - 1:
            raise ConfigError("称号结构 %s 连接符数量应为 %d" % (sid, len(fields) - 1))
        connectors = connectors + ("",) * (len(fields) - 1 - len(connectors))
        if float(s.get("weight", 1)) <= 0:
            raise ConfigError("称号结构权重必须为正: %s" % sid)
        structures.append(TitleStructureDef(
            id=sid, weight=float(s.get("weight", 1)),
            fields=fields, connectors=connectors,
        ))
    if not structures:
        raise ConfigError("称号结构池为空")

    title_pools = {}
    for pool_name, pool_key in (("prefix", "prefixes"), ("core", "cores"), ("suffix", "suffixes")):
        pool = []
        seen_ids = set()
        for entry in titles_data.get(pool_key, []):
            tid = str(entry["id"])
            if tid in seen_ids:
                raise ConfigError("称号字段 id 重复: %s/%s" % (pool_key, tid))
            seen_ids.add(tid)
            if float(entry.get("weight", 1)) <= 0:
                raise ConfigError("称号字段权重必须为正: %s/%s" % (pool_key, tid))
            bonus = {str(k): int(v) for k, v in entry.get("bonus", {}).items()}
            for attr_id in bonus:
                if attr_id not in seen_attrs:
                    raise ConfigError("称号字段 %s/%s 加成了未定义的属性 %s" % (pool_key, tid, attr_id))
            pool.append(TitleFieldDef(id=tid, weight=float(entry.get("weight", 1)), bonus=bonus))
        if not pool:
            raise ConfigError("称号字段池为空: %s" % pool_key)
        title_pools[pool_name] = tuple(pool)

    variance = battle_data.get("variance", [1.0, 1.0])
    battle = BattleCfg(
        crit_multiplier=float(battle_data.get("crit_multiplier", 1.8)),
        variance_lo=float(variance[0]),
        variance_hi=float(variance[1]),
        atk_factor=float(battle_data.get("atk_factor", 1.0)),
        defense_factor=float(battle_data.get("defense_factor", 1.0)),
        min_damage=int(battle_data.get("min_damage", 1)),
        max_ticks=int(battle_data.get("max_ticks", 600)),
        gauge_threshold=float(battle_data.get("gauge_threshold", 100)),
        crit_cap=float(battle_data.get("crit_cap", 100)),
        dodge_cap=float(battle_data.get("dodge_cap", 60)),
        seed_separator=str(battle_data.get("seed_separator", "")),
    )
    if battle.max_ticks < 1:
        raise ConfigError("max_ticks 必须 >= 1")
    if battle.gauge_threshold <= 0:
        raise ConfigError("gauge_threshold 必须 > 0")
    if battle.atk_factor <= 0:
        raise ConfigError("atk_factor 必须 > 0")
    if battle.variance_lo > battle.variance_hi:
        raise ConfigError("variance 区间非法")

    return GameCfg(
        system=system, attributes=tuple(attributes),
        skills=tuple(skills),
        title_structures=tuple(structures), title_pools=title_pools,
        skill_count_min=sc_min, skill_count_max=sc_max,
        skill_md5_variance=skill_md5_variance,
        skill_variable_link=skill_variable_link,
        skill_name_modifiers=skill_name_modifiers,
        battle=battle,
    )


class Locale:
    """某一语言的全部文案（纯文本，不含任何数值规则）。"""

    def __init__(self, lang, ui, attributes, skills, titles,
                 stats, buffs, modifiers, battle_log):
        self.lang = lang
        self.ui = ui
        self.attributes = attributes
        self.skills = skills
        self.titles = titles          # {"prefixes": {...}, "cores": {...}, "suffixes": {...}}
        self.stats = stats            # 技能参数标签模板 + 共鸣标记/词缀修饰模板
        self.buffs = buffs            # buff 名称/说明模板
        self.modifiers = modifiers    # 技能词缀名称 {"prefixes": {...}, "suffixes": {...}}
        self.battle_log = battle_log

    def ref_name(self, registry: str, ref_id: str):
        """按注册名取显示名；缺失返回 None。stat_word 注册名直接返回 stats 字符串。"""
        if registry == "stat_word":
            word = self.stats.get(ref_id)
            return str(word) if word is not None else None
        table = {
            "skill": self.skills, "attr": self.attributes,
        }.get(registry)
        if table is None:
            return None
        entry = table.get(ref_id)
        if isinstance(entry, dict) and "name" in entry:
            return entry["name"]
        return None


LOCALE_FILES = ("ui", "attributes", "skills", "titles",
                "stats", "buffs", "modifiers", "battle_log")


def load_locale(config_root, lang: str) -> Locale:
    root = Path(config_root) / "locales" / str(lang)
    data = {name: _read_json(root / ("%s.json" % name)) for name in LOCALE_FILES}
    return Locale(
        lang=str(lang), ui=data["ui"], attributes=data["attributes"],
        skills=data["skills"],
        titles=data["titles"], stats=data["stats"], buffs=data["buffs"],
        modifiers=data["modifiers"], battle_log=data["battle_log"],
    )
