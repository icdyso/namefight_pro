"""配置加载与校验。

配置分两层（见 AGENTS.md 2.2）：
- config/game/      数值与规则（与语言无关）
- config/locales/   文案（与数值无关）

启动时一次性加载并校验；修改配置后需重启进程。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 引擎依赖的属性 id（battle.py 直接按这些 key 取值，配置必须提供）
REQUIRED_ATTRIBUTE_IDS = ("hp", "atk", "def", "spd", "crit", "dodge")


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
    min: int
    max: int
    format: str          # int / percent，仅影响展示
    power_weight: float  # 战力权重


@dataclass(frozen=True)
class ElementDef:
    id: str
    weight: float
    advantage: dict      # {防守方元素id: 克制倍率}


@dataclass(frozen=True)
class RarityDef:
    id: str
    weight: float
    stars: int
    multipliers: dict    # {属性id: 倍率}


@dataclass(frozen=True)
class SkillDef:
    id: str
    weight: float
    trigger: str         # on_attack / on_defense / on_turn_start / passive
    effect: dict         # {type: 效果类型, ...参数}


@dataclass(frozen=True)
class TitleDef:
    id: str
    weight: float


@dataclass(frozen=True)
class BattleCfg:
    crit_multiplier: float
    variance_lo: float
    variance_hi: float
    defense_factor: float
    min_damage: int
    max_rounds: int
    crit_cap: float      # 百分数上限
    dodge_cap: float     # 百分数上限
    seed_separator: str


@dataclass(frozen=True)
class GameCfg:
    system: SystemCfg
    attributes: tuple    # (AttributeDef, ...)，配置文件顺序
    elements: tuple
    rarities: tuple
    skills: tuple
    titles: tuple
    skill_count_min: int
    skill_count_max: int
    rarity_scaled_attributes: tuple
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
    elems_data = _read_json(game / "elements.json")
    rar_data = _read_json(game / "rarities.json")
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
        if int(a["min"]) > int(a["max"]):
            raise ConfigError("属性区间非法: %s" % a["id"])
        seen_attrs.add(a["id"])
        attributes.append(AttributeDef(
            id=str(a["id"]), min=int(a["min"]), max=int(a["max"]),
            format=str(a.get("format", "int")), power_weight=float(a.get("power_weight", 0)),
        ))
    missing = [i for i in REQUIRED_ATTRIBUTE_IDS if i not in seen_attrs]
    if missing:
        raise ConfigError("attributes.json 缺少引擎必需属性: %s" % missing)

    elements = []
    seen_elems = set()
    for e in elems_data.get("elements", []):
        if e["id"] in seen_elems:
            raise ConfigError("元素 id 重复: %s" % e["id"])
        if float(e.get("weight", 1)) <= 0:
            raise ConfigError("元素权重必须为正: %s" % e["id"])
        seen_elems.add(e["id"])
        elements.append(ElementDef(
            id=str(e["id"]), weight=float(e.get("weight", 1)),
            advantage={str(k): float(v) for k, v in e.get("advantage", {}).items()},
        ))
    if not elements:
        raise ConfigError("元素池为空")
    for e in elements:
        for defender in e.advantage:
            if defender not in seen_elems:
                raise ConfigError("元素 %s 克制了未定义的元素 %s" % (e.id, defender))

    scaled = tuple(str(x) for x in rar_data.get("scaled_attributes", []))
    rarities = []
    for r in rar_data.get("rarities", []):
        mult = {str(k): float(v) for k, v in r.get("multipliers", {}).items()}
        for attr_id in mult:
            if attr_id not in seen_attrs:
                raise ConfigError("稀有度 %s 加成了未定义的属性 %s" % (r["id"], attr_id))
        if float(r.get("weight", 1)) <= 0:
            raise ConfigError("稀有度权重必须为正: %s" % r["id"])
        rarities.append(RarityDef(
            id=str(r["id"]), weight=float(r.get("weight", 1)),
            stars=int(r.get("stars", 1)), multipliers=mult,
        ))
    if not rarities:
        raise ConfigError("稀有度池为空")

    skill_count = skills_data.get("skill_count", {})
    skills = []
    seen_skills = set()
    for s in skills_data.get("skills", []):
        if s["id"] in seen_skills:
            raise ConfigError("技能 id 重复: %s" % s["id"])
        if float(s.get("weight", 1)) <= 0:
            raise ConfigError("技能权重必须为正: %s" % s["id"])
        seen_skills.add(s["id"])
        skills.append(SkillDef(
            id=str(s["id"]), weight=float(s.get("weight", 1)),
            trigger=str(s.get("trigger", "passive")), effect=dict(s.get("effect", {})),
        ))
    if not skills:
        raise ConfigError("技能池为空")
    sc_min = int(skill_count.get("min", 1))
    sc_max = int(skill_count.get("max", 1))
    if not (1 <= sc_min <= sc_max <= len(skills)):
        raise ConfigError("技能数量配置非法: [%s, %s]" % (sc_min, sc_max))

    titles = []
    for ttl in titles_data.get("titles", []):
        if float(ttl.get("weight", 1)) <= 0:
            raise ConfigError("称号权重必须为正: %s" % ttl["id"])
        titles.append(TitleDef(id=str(ttl["id"]), weight=float(ttl.get("weight", 1))))
    if not titles:
        raise ConfigError("称号池为空")

    variance = battle_data.get("variance", [1.0, 1.0])
    battle = BattleCfg(
        crit_multiplier=float(battle_data.get("crit_multiplier", 1.8)),
        variance_lo=float(variance[0]),
        variance_hi=float(variance[1]),
        defense_factor=float(battle_data.get("defense_factor", 1.0)),
        min_damage=int(battle_data.get("min_damage", 1)),
        max_rounds=int(battle_data.get("max_rounds", 120)),
        crit_cap=float(battle_data.get("crit_cap", 100)),
        dodge_cap=float(battle_data.get("dodge_cap", 60)),
        seed_separator=str(battle_data.get("seed_separator", "\u001f")),
    )
    if battle.max_rounds < 1:
        raise ConfigError("max_rounds 必须 >= 1")
    if battle.variance_lo > battle.variance_hi:
        raise ConfigError("variance 区间非法")

    return GameCfg(
        system=system, attributes=tuple(attributes), elements=tuple(elements),
        rarities=tuple(rarities), skills=tuple(skills), titles=tuple(titles),
        skill_count_min=sc_min, skill_count_max=sc_max,
        rarity_scaled_attributes=scaled, battle=battle,
    )


class Locale:
    """某一语言的全部文案（纯文本，不含任何数值规则）。"""

    def __init__(self, lang, ui, attributes, elements, rarities, skills, titles, battle_log):
        self.lang = lang
        self.ui = ui
        self.attributes = attributes
        self.elements = elements
        self.rarities = rarities
        self.skills = skills
        self.titles = titles
        self.battle_log = battle_log

    def ref_name(self, registry: str, ref_id: str):
        """按注册名（skill/title/element/rarity/attr）取显示名；缺失返回 None。"""
        table = {
            "skill": self.skills, "title": self.titles, "element": self.elements,
            "rarity": self.rarities, "attr": self.attributes,
        }.get(registry)
        if table is None:
            return None
        entry = table.get(ref_id)
        if isinstance(entry, dict) and "name" in entry:
            return entry["name"]
        return None


LOCALE_FILES = ("ui", "attributes", "elements", "rarities", "skills", "titles", "battle_log")


def load_locale(config_root, lang: str) -> Locale:
    root = Path(config_root) / "locales" / str(lang)
    data = {name: _read_json(root / ("%s.json" % name)) for name in LOCALE_FILES}
    return Locale(
        lang=str(lang), ui=data["ui"], attributes=data["attributes"],
        elements=data["elements"], rarities=data["rarities"], skills=data["skills"],
        titles=data["titles"], battle_log=data["battle_log"],
    )
