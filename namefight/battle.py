"""确定性对战引擎（核心不变量见 AGENTS.md 2.1）。

- 对战种子 = md5(字典序排序后的双方规范化名字，以配置分隔符连接)；
- 先后手 = 速度降序 -> 规范化名字升序，与输入顺序无关；
- 一切随机来自 DetRng；事件以 (模板 id, 参数) 结构化记录，渲染时才套语言。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .config import GameCfg
from .fighter import Fighter
from .rng import DetRng
from .text import render_template

# 引擎支持的技能效果类型（新增效果必须同步此表与 tests/test_config.py）
SUPPORTED_EFFECTS = frozenset({
    "damage_multiplier", "lifesteal", "poison", "stun", "extra_strikes",
    "damage_reduction", "reflect", "dodge_bonus", "crit_bonus", "heal",
    "low_hp_atk_bonus",
})

# 引擎会输出的战报模板 id（测试据此校验每个 locale 都有对应文案）
TEMPLATES_USED = frozenset({
    "battle_start", "round_start", "turn_stun", "poison_tick", "poison_death",
    "skill_proc", "effect_damage_up", "effect_execution", "effect_lifesteal",
    "effect_poison", "effect_stun", "effect_extra_strike", "effect_heal",
    "effect_reduction", "effect_reflect", "low_hp_trigger", "attack_crit",
    "attack_miss", "attack_hit", "death", "victory", "draw", "timeout",
})


@dataclass
class _Combatant:
    fighter: Fighter
    pos: int                 # 在输入中的位置 0/1
    name: str
    max_hp: int
    hp: int
    atk: float
    defense: int
    dodge: float             # 百分数
    crit: float              # 百分数
    element_id: str
    skills: list             # SkillDef 列表（按派生顺序）
    poison_turns: int = 0
    poison_damage: int = 0
    stunned: bool = False
    last_stand_active: bool = False
    last_stand_bonus: float = 0.0
    damage_dealt: int = 0


@dataclass
class BattleOutcome:
    winner_pos: int          # 0/1；平局为 -1
    winner_name: str | None
    draw: bool
    rounds: int
    damage: dict             # {输入位置0: int, 输入位置1: int}
    seed: str
    events: list = field(default_factory=list)


def _pct(x: float) -> str:
    return "%s%%" % round(x * 100)


def _make_combatant(f: Fighter, pos: int, game: GameCfg) -> _Combatant:
    bc = game.battle
    skills = [next(s for s in game.skills if s.id == sid) for sid in f.skill_ids]
    dodge = float(f.attrs["dodge"])
    crit = float(f.attrs["crit"])
    for sk in skills:
        if sk.trigger == "passive":
            eff = sk.effect
            if eff.get("type") == "dodge_bonus":
                dodge += float(eff.get("value", 0))
            elif eff.get("type") == "crit_bonus":
                crit += float(eff.get("value", 0))
    dodge = min(dodge, bc.dodge_cap)
    crit = min(crit, bc.crit_cap)
    return _Combatant(
        fighter=f, pos=pos, name=f.name,
        max_hp=f.attrs["hp"], hp=f.attrs["hp"],
        atk=float(f.attrs["atk"]), defense=int(f.attrs["def"]),
        dodge=dodge, crit=crit, element_id=f.element_id, skills=skills,
    )


def _element_multiplier(attacker_id: str, defender_id: str, game: GameCfg) -> float:
    for e in game.elements:
        if e.id == attacker_id:
            return e.advantage.get(defender_id, 1.0)
    return 1.0


def _compute_damage(actor, enemy, mult, crit, game, rng, ratio=1.0) -> int:
    bc = game.battle
    variance = bc.variance_lo + rng.next_float() * (bc.variance_hi - bc.variance_lo)
    atk = actor.atk * (1.0 + actor.last_stand_bonus) if actor.last_stand_active else actor.atk
    crit_mult = bc.crit_multiplier if crit else 1.0
    element_mult = _element_multiplier(actor.element_id, enemy.element_id, game)
    raw = atk * ratio * variance * crit_mult * element_mult * mult
    return max(bc.min_damage, round(raw - enemy.defense * bc.defense_factor))


def run_battle(fighter_a: Fighter, fighter_b: Fighter, game: GameCfg) -> BattleOutcome:
    bc = game.battle
    combatants = [_make_combatant(f, pos, game)
                  for pos, f in enumerate((fighter_a, fighter_b))]
    # 行动顺序：速度降序、规范化名字升序（与输入顺序无关；sorted 稳定）
    order = sorted(combatants, key=lambda c: (-c.fighter.attrs["spd"], c.fighter.normalized))
    joined = bc.seed_separator.join(sorted((fighter_a.normalized, fighter_b.normalized)))
    seed_hex = hashlib.md5(joined.encode("utf-8")).hexdigest()
    rng = DetRng(int(seed_hex, 16))

    events = []
    round_no = 0

    def ev(template, params=None):
        events.append({"round": round_no, "template": template, "params": params or {}})

    first, second = order[0], order[1]
    ev("battle_start", {
        "a": first.name, "b": second.name,
        "title_a": {"ref": "title", "id": first.fighter.title_id},
        "title_b": {"ref": "title", "id": second.fighter.title_id},
        "element_a": {"ref": "element", "id": first.element_id},
        "element_b": {"ref": "element", "id": second.element_id},
    })

    winner = None
    draw = False
    while round_no < bc.max_rounds and winner is None and not draw:
        round_no += 1
        ev("round_start", {"round": round_no})
        for actor in order:
            enemy = order[1] if actor is order[0] else order[0]
            if actor.hp <= 0 or enemy.hp <= 0:
                break
            # 毒发（眩晕/跳过行动也不影响毒结算）
            if actor.poison_turns > 0:
                dmg = actor.poison_damage
                actor.hp -= dmg
                actor.poison_turns -= 1
                ev("poison_tick", {"a": actor.name, "damage": dmg})
                if actor.hp <= 0:
                    ev("poison_death", {"a": actor.name})
                    winner = enemy
                    break
            # 回合开始技能（治疗等）
            for sk in actor.skills:
                if sk.trigger != "on_turn_start":
                    continue
                eff = sk.effect
                if rng.next_float() > float(eff.get("chance", 1.0)):
                    continue
                ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sk.id}})
                if eff.get("type") == "heal" and actor.hp > 0:
                    gained = min(int(eff.get("value", 0)), actor.max_hp - actor.hp)
                    if gained > 0:
                        actor.hp += gained
                        ev("effect_heal", {"a": actor.name, "heal": gained})
            if winner is not None:
                break
            # 眩晕
            if actor.stunned:
                actor.stunned = False
                ev("turn_stun", {"a": actor.name})
                continue
            # 背水一战（生命低于阈值时一次性触发）
            for sk in actor.skills:
                eff = sk.effect
                if sk.trigger != "passive" or eff.get("type") != "low_hp_atk_bonus":
                    continue
                threshold = float(eff.get("threshold", 0.3))
                if not actor.last_stand_active and actor.hp < actor.max_hp * threshold:
                    actor.last_stand_active = True
                    actor.last_stand_bonus = float(eff.get("value", 0.5))
                    ev("low_hp_trigger", {"a": actor.name})
            _attack(actor, enemy, game, rng, ev)
            if actor.hp <= 0 and enemy.hp <= 0:
                draw = True
            elif enemy.hp <= 0:
                winner = actor
            elif actor.hp <= 0:
                winner = enemy
            if winner is not None or draw:
                break

    if winner is None and not draw:
        ev("timeout", {})
        ratio_a = combatants[0].hp / combatants[0].max_hp
        ratio_b = combatants[1].hp / combatants[1].max_hp
        if ratio_a > ratio_b:
            winner = combatants[0]
        elif ratio_b > ratio_a:
            winner = combatants[1]
        else:
            draw = True

    if draw:
        ev("draw", {})
    else:
        ev("victory", {"winner": winner.name})

    return BattleOutcome(
        winner_pos=-1 if draw else winner.pos,
        winner_name=None if draw else winner.name,
        draw=draw,
        rounds=round_no,
        damage={0: combatants[0].damage_dealt, 1: combatants[1].damage_dealt},
        seed=seed_hex,
        events=events,
    )


def _attack(actor, enemy, game, rng, ev):
    bc = game.battle
    mult = 1.0
    lifesteal = 0.0
    poison = None
    stun = False
    extra_ratios = []
    for sk in actor.skills:
        if sk.trigger != "on_attack":
            continue
        eff = sk.effect
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sk.id}})
        t = eff.get("type")
        if t == "damage_multiplier":
            cond = eff.get("condition")
            satisfied = True
            if cond and cond.get("type") == "target_hp_below":
                satisfied = enemy.hp <= enemy.max_hp * float(cond.get("value", 0))
            if satisfied:
                mult *= float(eff.get("value", 1.0))
                if cond:
                    ev("effect_execution", {"mult": _pct(mult)})
                else:
                    ev("effect_damage_up", {"mult": _pct(mult)})
        elif t == "lifesteal":
            lifesteal += float(eff.get("value", 0))
        elif t == "poison":
            poison = (int(eff.get("damage", 0)), int(eff.get("turns", 0)))
        elif t == "stun":
            stun = True
        elif t == "extra_strikes":
            extra_ratios.extend(float(x) for x in eff.get("ratios", []))

    # 闪避判定
    if rng.next_float() < enemy.dodge / 100.0:
        ev("attack_miss", {"a": actor.name, "b": enemy.name})
        return

    crit = rng.next_float() < actor.crit / 100.0
    if crit:
        ev("attack_crit", {})
    dmg = _compute_damage(actor, enemy, mult, crit, game, rng)

    # 防守方技能（减伤 / 反甲）
    for sk in enemy.skills:
        if sk.trigger != "on_defense":
            continue
        eff = sk.effect
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        ev("skill_proc", {"a": enemy.name, "skill": {"ref": "skill", "id": sk.id}})
        t = eff.get("type")
        if t == "damage_reduction":
            ratio = float(eff.get("value", 0))
            dmg = max(bc.min_damage, round(dmg * (1.0 - ratio)))
            ev("effect_reduction", {"b": enemy.name, "ratio": _pct(ratio)})
        elif t == "reflect":
            refl = max(1, round(dmg * float(eff.get("value", 0))))
            enemy.damage_dealt += refl
            actor.hp -= refl
            ev("effect_reflect", {"a": actor.name, "b": enemy.name, "damage": refl})

    enemy.hp -= dmg
    actor.damage_dealt += dmg
    ev("attack_hit", {"a": actor.name, "b": enemy.name, "damage": dmg, "hp": max(0, enemy.hp)})

    if lifesteal > 0 and dmg > 0 and actor.hp > 0:
        gained = min(round(dmg * lifesteal), actor.max_hp - actor.hp)
        if gained > 0:
            actor.hp += gained
            ev("effect_lifesteal", {"a": actor.name, "heal": gained})
    if poison is not None and enemy.hp > 0 and poison[0] > 0 and poison[1] > 0:
        enemy.poison_damage, enemy.poison_turns = poison
        ev("effect_poison", {"b": enemy.name, "damage": poison[0], "turns": poison[1]})
    if stun and enemy.hp > 0:
        enemy.stunned = True
        ev("effect_stun", {"b": enemy.name})
    if enemy.hp <= 0:
        ev("death", {"b": enemy.name})
    elif actor.hp <= 0:
        ev("death", {"b": actor.name})

    # 追击（双方存活时）
    if extra_ratios and enemy.hp > 0 and actor.hp > 0:
        for ratio in extra_ratios:
            ev("effect_extra_strike", {})
            if rng.next_float() < enemy.dodge / 100.0:
                ev("attack_miss", {"a": actor.name, "b": enemy.name})
                continue
            crit2 = rng.next_float() < actor.crit / 100.0
            if crit2:
                ev("attack_crit", {})
            dmg2 = _compute_damage(actor, enemy, mult, crit2, game, rng, ratio)
            enemy.hp -= dmg2
            actor.damage_dealt += dmg2
            ev("attack_hit", {"a": actor.name, "b": enemy.name, "damage": dmg2, "hp": max(0, enemy.hp)})
            if enemy.hp <= 0:
                ev("death", {"b": enemy.name})
                break


def render_events(events, locale) -> list:
    """把结构化事件渲染为当前语言的文本列表。"""
    return [render_template(locale.battle_log.get(e["template"], e["template"]),
                            e.get("params"), locale) for e in events]


def battle_to_api(outcome: BattleOutcome, fighters_api: list, locale) -> dict:
    texts = render_events(outcome.events, locale)
    log = [dict(e, text=text) for e, text in zip(outcome.events, texts)]
    return {
        "fighters": fighters_api,
        "result": {
            "winner": outcome.winner_name,
            "winner_pos": outcome.winner_pos,
            "draw": outcome.draw,
            "rounds": outcome.rounds,
            "damage": {"a": outcome.damage[0], "b": outcome.damage[1]},
        },
        "seed": outcome.seed,
        "log": log,
    }
