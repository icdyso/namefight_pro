"""确定性对战引擎（核心不变量见 AGENTS.md 2.1）。

v0.2.0 起的战斗模型：
- 以 tick 推进：每个 tick 双方行动槽（gauge）累加自身速度值，达到阈值
  （battle.json 的 gauge_threshold）即可行动一次并扣回阈值；速度决定行动频率。
  同一 tick 多人可行动时，按（gauge 余量降序、内部序）依次执行；
- 内部序 = 速度降序、规范化名字升序，与输入顺序无关；
- 技能参数经 fighter.personalized_effects 按斗士 MD5 个性化；
- 元素仅为身份标识，不参与伤害计算；
- 对战种子 = md5(字典序排序后的双方规范化名字，以配置分隔符连接)；
- 每条战报附带双方状态快照（HP/属性/行动槽/buff），前端据此实时渲染 HUD。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .config import GameCfg
from .fighter import Fighter, personalized_effects
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 引擎支持的技能效果类型（新增效果必须同步此表与 tests/test_config.py）
SUPPORTED_EFFECTS = frozenset({
    "damage_multiplier", "lifesteal", "poison", "stun", "extra_strikes",
    "damage_reduction", "reflect", "dodge_bonus", "crit_bonus", "heal",
    "low_hp_atk_bonus",
})

# 引擎会输出的战报模板 id（测试据此校验每个 locale 都有对应文案）
TEMPLATES_USED = frozenset({
    "battle_start", "tick_marker", "turn_stun", "poison_tick", "poison_death",
    "skill_proc", "effect_damage_up", "effect_execution", "effect_lifesteal",
    "effect_poison", "effect_stun", "effect_extra_strike", "effect_heal",
    "effect_reduction", "effect_reflect", "effect_link", "low_hp_trigger",
    "attack_crit", "attack_miss", "attack_hit", "death", "victory", "draw",
    "timeout",
})

# 引擎会写入状态快照的 buff id（测试据此校验每个 locale 都有对应文案）
BUFF_IDS = frozenset({"poison", "stun", "last_stand", "crit_up", "dodge_up"})


@dataclass
class _Combatant:
    fighter: Fighter
    pos: int                 # 在输入中的位置 0/1（快照键 a/b 与此对应）
    name: str
    max_hp: int
    hp: int
    atk: float
    defense: int
    spd: int
    dodge: float             # 百分数
    crit: float              # 百分数
    element_id: str
    skills: list             # [(SkillDef, 个性化效果dict), ...] 按派生顺序
    gauge: float = 0.0       # 行动槽
    seq: int = 0             # 内部序（速度降序、名字升序）
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
    ticks: int
    damage: dict             # {输入位置0: int, 输入位置1: int}
    seed: str
    events: list = field(default_factory=list)


def _eff_atk(c: _Combatant) -> float:
    return c.atk * (1.0 + c.last_stand_bonus) if c.last_stand_active else c.atk


def _live_value(c: _Combatant, vid: str) -> float:
    """共鸣取用的「当前值」：hp 为当前生命，atk 含背水一战加成，
    crit/dodge 含被动，def/spd 为面板值。"""
    if vid == "hp":
        return float(max(0, c.hp))
    if vid == "atk":
        return _eff_atk(c)
    if vid == "def":
        return float(c.defense)
    if vid == "spd":
        return float(c.spd)
    if vid == "crit":
        return float(c.crit)
    if vid == "dodge":
        return float(c.dodge)
    return 0.0


def compute_link_bonus(actor: _Combatant, enemy: _Combatant, eff: dict, game: GameCfg) -> int:
    """共鸣附伤（触发时刻的动态值）：

    - 比例模式：bonus = 源方属性当前值 × rate（源方为己方或敌方）；
    - 差值模式：bonus = (己方变量 − 敌方参照属性) × rate，参照属性见
      config 中该变量的 diff_against（如 攻↔防、速↔速、暴击↔闪避）；
    - 结果向下取整到非负整数。
    """
    link = eff.get("link")
    if not link:
        return 0
    rate = float(link.get("rate", 0))
    if link.get("mode") == "difference":
        vdef = next((v for v in game.skill_variable_link.variables
                     if v.id == link.get("variable")), None)
        against = vdef.diff_against if vdef else link.get("variable")
        raw = _live_value(actor, link.get("variable")) - _live_value(enemy, against)
    else:
        src = enemy if link.get("source") == "enemy" else actor
        raw = _live_value(src, link.get("variable"))
    return max(0, round(raw * rate))


def _make_combatant(f: Fighter, pos: int, game: GameCfg) -> _Combatant:
    bc = game.battle
    skills = personalized_effects(f, game)
    dodge = float(f.attrs["dodge"])
    crit = float(f.attrs["crit"])
    for sdef, eff in skills:
        if sdef.trigger == "passive":
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
        spd=int(f.attrs["spd"]), dodge=dodge, crit=crit,
        element_id=f.element_id, skills=skills,
    )


def _compute_damage(actor, enemy, mult, crit, game, rng, ratio=1.0) -> int:
    bc = game.battle
    variance = bc.variance_lo + rng.next_float() * (bc.variance_hi - bc.variance_lo)
    crit_mult = bc.crit_multiplier if crit else 1.0
    raw = _eff_atk(actor) * ratio * variance * crit_mult * mult
    return max(bc.min_damage, round(raw - enemy.defense * bc.defense_factor))


def _snapshot(combatants, threshold: float) -> dict:
    """双方状态快照（按输入位置 a/b），buff 以 id+params 存储、渲染时查 locale。"""
    def one(c):
        buffs = []
        if c.poison_turns > 0:
            buffs.append({"id": "poison",
                          "params": {"damage": c.poison_damage, "turns": c.poison_turns}})
        if c.stunned:
            buffs.append({"id": "stun", "params": {}})
        if c.last_stand_active:
            buffs.append({"id": "last_stand", "params": {"value": format_pct(c.last_stand_bonus)}})
        for sdef, eff in c.skills:
            if sdef.trigger == "passive":
                t = eff.get("type")
                if t == "crit_bonus":
                    buffs.append({"id": "crit_up", "params": {"value": format_num(float(eff.get("value", 0)))}})
                elif t == "dodge_bonus":
                    buffs.append({"id": "dodge_up", "params": {"value": format_num(float(eff.get("value", 0)))}})
        return {
            "hp": max(0, int(c.hp)),
            "max_hp": c.max_hp,
            "atk": int(round(_eff_atk(c))),
            "def": c.defense,
            "spd": c.spd,
            "gauge": max(0.0, min(100.0, round(c.gauge * 100.0 / threshold, 1))),
            "buffs": buffs,
        }
    return {"a": one(combatants[0]), "b": one(combatants[1])}


def run_battle(fighter_a: Fighter, fighter_b: Fighter, game: GameCfg) -> BattleOutcome:
    bc = game.battle
    combatants = [_make_combatant(f, pos, game)
                  for pos, f in enumerate((fighter_a, fighter_b))]
    internal = sorted(combatants,
                      key=lambda c: (-c.fighter.attrs["spd"], c.fighter.normalized))
    for i, c in enumerate(internal):
        c.seq = i
    joined = bc.seed_separator.join(sorted((fighter_a.normalized, fighter_b.normalized)))
    seed_hex = hashlib.md5(joined.encode("utf-8")).hexdigest()
    rng = DetRng(int(seed_hex, 16))

    events = []
    tick = 0
    last_logged_tick = 0

    def ev(template, params=None):
        nonlocal last_logged_tick
        if tick != last_logged_tick:
            events.append({"tick": tick, "template": "tick_marker",
                           "params": {"tick": tick},
                           "state": _snapshot(combatants, bc.gauge_threshold)})
            last_logged_tick = tick
        events.append({"tick": tick, "template": template, "params": params or {},
                       "state": _snapshot(combatants, bc.gauge_threshold)})

    first, second = internal[0], internal[1]
    ev("battle_start", {
        "a": first.name, "b": second.name,
        "element_a": {"ref": "element", "id": first.element_id},
        "element_b": {"ref": "element", "id": second.element_id},
    })

    winner = None
    draw = False
    while tick < bc.max_ticks and winner is None and not draw:
        tick += 1
        for c in combatants:
            if c.hp > 0:
                c.gauge += c.spd
        ready = [c for c in internal if c.hp > 0 and c.gauge >= bc.gauge_threshold]
        ready.sort(key=lambda c: (-c.gauge, c.seq))
        for actor in ready:
            enemy = internal[1] if actor is internal[0] else internal[0]
            actor.gauge -= bc.gauge_threshold
            if actor.hp <= 0 or enemy.hp <= 0:
                break
            # 毒发（在拥有者的行动时机结算；眩晕不影响毒）
            if actor.poison_turns > 0:
                dmg = actor.poison_damage
                actor.hp -= dmg
                actor.poison_turns -= 1
                ev("poison_tick", {"a": actor.name, "damage": dmg})
                if actor.hp <= 0:
                    ev("poison_death", {"a": actor.name})
                    winner = enemy
                    break
            # 行动开始技能（治疗等）
            for sdef, eff in actor.skills:
                if sdef.trigger != "on_turn_start":
                    continue
                if rng.next_float() > float(eff.get("chance", 1.0)):
                    continue
                ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sdef.id}})
                if eff.get("type") == "heal" and actor.hp > 0:
                    gained = min(int(round(float(eff.get("value", 0)))),
                                 actor.max_hp - actor.hp)
                    if gained > 0:
                        actor.hp += gained
                        ev("effect_heal", {"a": actor.name, "heal": gained})
            if winner is not None:
                break
            # 眩晕：消耗本次行动
            if actor.stunned:
                actor.stunned = False
                ev("turn_stun", {"a": actor.name})
                continue
            # 背水一战（生命低于阈值时一次性触发）
            for sdef, eff in actor.skills:
                if sdef.trigger != "passive" or eff.get("type") != "low_hp_atk_bonus":
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
        ticks=tick,
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
    resonance = 0        # 共鸣附伤（加在本次主伤害上）
    poison_resonance = 0  # 淬毒类技能的共鸣（加在毒伤上）
    for sdef, eff in actor.skills:
        if sdef.trigger != "on_attack":
            continue
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sdef.id}})
        t = eff.get("type")
        satisfied = True
        if t == "damage_multiplier":
            cond = eff.get("condition")
            if cond and cond.get("type") == "target_hp_below":
                satisfied = enemy.hp <= enemy.max_hp * float(cond.get("value", 0))
        # 变量共鸣：触发时按「当前值」动态计算附伤（己方/敌方、比例/差值）
        bonus = compute_link_bonus(actor, enemy, eff, game) if satisfied else 0
        if bonus > 0:
            if t == "poison":
                poison_resonance += bonus
            else:
                resonance += bonus
            ev("effect_link", {
                "a": actor.name,
                "stat": {"ref": "attr", "id": eff["link"]["variable"]},
                "scope": {"ref": "stat_word", "id": "scope_" + eff["link"].get("source", "own")},
                "mode": {"ref": "stat_word", "id": "mode_" + eff["link"].get("mode", "ratio")},
                "damage": bonus,
            })
        if not satisfied:
            continue
        if t == "damage_multiplier":
            mult *= float(eff.get("value", 1.0))
            if eff.get("condition"):
                ev("effect_execution", {"mult": format_pct(mult)})
            else:
                ev("effect_damage_up", {"mult": format_pct(mult)})
        elif t == "lifesteal":
            lifesteal += float(eff.get("value", 0))
        elif t == "poison":
            poison = (int(round(float(eff.get("damage", 0)))) + poison_resonance,
                      int(eff.get("turns", 0)))
            poison_resonance = 0
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
    if resonance > 0:
        dmg = max(bc.min_damage, dmg + resonance)

    # 防守方技能（减伤 / 反甲）
    for sdef, eff in enemy.skills:
        if sdef.trigger != "on_defense":
            continue
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        ev("skill_proc", {"a": enemy.name, "skill": {"ref": "skill", "id": sdef.id}})
        t = eff.get("type")
        if t == "damage_reduction":
            ratio = float(eff.get("value", 0))
            dmg = max(bc.min_damage, round(dmg * (1.0 - ratio)))
            ev("effect_reduction", {"b": enemy.name, "ratio": format_pct(ratio)})
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
            ev("attack_hit", {"a": actor.name, "b": enemy.name,
                              "damage": dmg2, "hp": max(0, enemy.hp)})
            if enemy.hp <= 0:
                ev("death", {"b": enemy.name})
                break


def render_events(events, locale) -> list:
    """把结构化事件渲染为当前语言的文本列表。"""
    return [render_template(locale.battle_log.get(e["template"], e["template"]),
                            e.get("params"), locale) for e in events]


def _render_state(state, locale) -> dict:
    """把快照中的 buff（id+params）渲染为带名称/说明的条目。"""
    out = {}
    for side, snap in (state or {}).items():
        buffs = []
        for b in snap.get("buffs", []):
            entry = locale.buffs.get(b["id"], {})
            buffs.append({
                "id": b["id"],
                "name": entry.get("name", b["id"]),
                "detail": render_template(entry.get("detail", ""), b.get("params"), locale),
                "desc": entry.get("desc", ""),
            })
        out[side] = dict(snap, buffs=buffs)
    return out


def battle_to_api(outcome: BattleOutcome, fighters_api: list, locale) -> dict:
    texts = render_events(outcome.events, locale)
    log = []
    for e, text in zip(outcome.events, texts):
        entry = dict(e)
        entry["text"] = text
        if "state" in entry:
            entry["state"] = _render_state(entry["state"], locale)
        log.append(entry)
    return {
        "fighters": fighters_api,
        "result": {
            "winner": outcome.winner_name,
            "winner_pos": outcome.winner_pos,
            "draw": outcome.draw,
            "ticks": outcome.ticks,
            "damage": {"a": outcome.damage[0], "b": outcome.damage[1]},
        },
        "seed": outcome.seed,
        "log": log,
    }
