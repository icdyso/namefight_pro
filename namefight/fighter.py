"""斗士派生：名字 -> MD5 -> 属性 / 技能 / 称号 / 元素 / 稀有度。

确定性契约（AGENTS.md 2.1.1）：派生结果是 (归一化名字, 配置快照) 的纯函数。
PRNG 消耗顺序固定：稀有度 -> 元素 -> 属性（配置顺序）-> 技能数 -> 技能 -> 称号。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import GameCfg
from .rng import DetRng


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
    title_id: str
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
    title = rng.pick_weighted((t, t.weight) for t in game.titles)

    power = round(sum(attrs[a.id] * a.power_weight for a in game.attributes))
    return Fighter(
        name=raw_name if isinstance(raw_name, str) and raw_name else normalized,
        normalized=normalized, digest=digest,
        rarity_id=rarity.id, element_id=element.id, attrs=attrs,
        skill_ids=tuple(s.id for s in skills), title_id=title.id, power=power,
    )


def fighter_to_api(fighter: Fighter, game: GameCfg, locale) -> dict:
    """斗士数据的对外表示：数值来自 Fighter，显示名全部来自 locale。"""
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
    for sid in fighter.skill_ids:
        entry = locale.skills.get(sid, {})
        skills_api.append({
            "id": sid,
            "name": entry.get("name", sid),
            "description": entry.get("description", ""),
        })
    rar_def = next(r for r in game.rarities if r.id == fighter.rarity_id)
    rar = locale.rarities.get(fighter.rarity_id, {})
    elem = locale.elements.get(fighter.element_id, {})
    ttl = locale.titles.get(fighter.title_id, {})
    return {
        "name": fighter.name,
        "normalized": fighter.normalized,
        "digest": fighter.digest,
        "digest_short": fighter.digest[:8],
        "rarity": {"id": fighter.rarity_id, "name": rar.get("name", fighter.rarity_id), "stars": rar_def.stars},
        "element": {"id": fighter.element_id, "name": elem.get("name", fighter.element_id),
                    "emoji": elem.get("emoji", "")},
        "title": {"id": fighter.title_id, "name": ttl.get("name", fighter.title_id),
                  "description": ttl.get("description", "")},
        "attributes": attrs_api,
        "skills": skills_api,
        "power": fighter.power,
    }
