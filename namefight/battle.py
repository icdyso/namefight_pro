"""确定性对战引擎（核心不变量见 AGENTS.md 2.1）。

tick 战斗模型：
- 每个 tick 双方行动槽（gauge）累加自身速度值，达到阈值
  （battle.json 的 gauge_threshold）即可行动一次并扣回阈值；速度决定行动频率。
  同一 tick 多人可行动时，按（gauge 余量降序、内部序）依次执行；
- 内部序 = 速度降序、规范化名字升序，与输入顺序无关；
- 行动开始时依次结算：毒发 -> 流血 -> 破甲递减 -> 行动开始技能（净化/回复）
  -> 眩晕判定 -> 背水一战 -> 攻击；
- 技能参数经 fighter.personalized_effects 按斗士 MD5 个性化；
- 元素仅为身份标识，不参与伤害计算；
- 对战种子 = md5(字典序排序后的双方规范化名字，以配置分隔符连接)；
- 每条战报附带双方状态快照（HP/属性/暴击/闪避/行动槽/buff），
  前端据此实时渲染 HUD 与技能实时数值。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .config import GameCfg
from .fighter import (Fighter, apply_resonance, format_resonance_final,
                      personalized_effects, resonance_coeff, resonance_target)
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 引擎支持的技能效果类型（新增效果必须同步此表与 tests/test_config.py）
SUPPORTED_EFFECTS = frozenset({
    "damage_multiplier", "lifesteal", "poison", "stun", "extra_strikes",
    "damage_reduction", "reflect", "dodge_bonus", "crit_bonus", "heal",
    "low_hp_atk_bonus",
    "streak_bonus", "overload", "armor_shred", "bleed", "exploit",
    "gauge_surge", "gamble", "bulwark", "retribution", "iron_will",
    "cleanse", "tempo", "armor_pen", "blood_pact", "grudge",
})

# 引擎会输出的战报模板 id（测试据此校验每个 locale 都有对应文案）
TEMPLATES_USED = frozenset({
    "battle_start", "tick_marker", "turn_stun", "poison_tick", "poison_death",
    "skill_proc", "effect_damage_up", "effect_execution", "effect_lifesteal",
    "effect_poison", "effect_stun", "effect_extra_strike", "effect_heal",
    "effect_reduction", "effect_reflect", "effect_link", "low_hp_trigger",
    "attack_crit", "attack_miss", "attack_hit", "death", "victory", "draw",
    "timeout",
    "overload_cost", "gamble_win", "gamble_lose", "effect_exploit",
    "shred_apply", "bleed_apply", "bleed_tick", "bleed_death", "streak_up",
    "effect_bulwark", "retribution_stack", "iron_will", "effect_cleanse",
    "tempo_up", "blood_pact",
})

# 引擎会写入状态快照的 buff id（测试据此校验每个 locale 都有对应文案）
BUFF_IDS = frozenset({
    "poison", "stun", "last_stand", "crit_up", "dodge_up",
    "bleed", "shred", "momentum", "grudge", "retribution",
    "iron_will", "iron_will_used", "tempo", "tempo_up", "blood_pact", "pen_up",
})

# 触发即记入战报「发动技能」行的效果类型；常驻/姿态类效果以自身专属事件表达
_PROC_LOGGED = frozenset({
    "damage_multiplier", "lifesteal", "poison", "stun", "extra_strikes",
    "armor_shred", "bleed",
})


@dataclass
class _Combatant:
    fighter: Fighter
    pos: int                 # 在输入中的位置 0/1（快照键 a/b 与此对应）
    name: str
    max_hp: int
    hp: float
    atk: float
    defense: float
    spd: float
    dodge: float             # 百分数
    crit: float              # 百分数
    element_id: str
    skills: list             # [(SkillDef, 个性化效果dict), ...] 按派生顺序
    pen: float = 0.0         # 护甲穿透（true_sight 被动，比例）
    has_iron_will: bool = False
    gauge: float = 0.0       # 行动槽
    seq: int = 0             # 内部序（速度降序、名字升序）
    poison_turns: int = 0
    poison_damage: float = 0.0
    bleed_turns: int = 0
    bleed_damage: float = 0.0
    shred_stacks: int = 0
    shred_total: float = 0.0
    shred_turns: int = 0
    stunned: bool = False
    last_stand_active: bool = False
    last_stand_bonus: float = 0.0
    hit_streak: int = 0      # 乘胜追击连击层
    grudge_stacks: int = 0   # 怨念层
    retribution_stacks: int = 0  # 以牙还牙层
    iron_will_used: bool = False
    tempo_active: bool = False   # 大器晚成是否已生效
    damage_dealt: float = 0.0


@dataclass
class BattleOutcome:
    winner_pos: int          # 0/1；平局为 -1
    winner_name: str | None
    draw: bool
    ticks: int
    damage: dict             # {输入位置0: float, 输入位置1: float}
    seed: str
    events: list = field(default_factory=list)


def _eff_atk(c: _Combatant) -> float:
    return c.atk * (1.0 + c.last_stand_bonus) if c.last_stand_active else c.atk


def _eff_def(c: _Combatant) -> float:
    """有效防御：面板防御 − 破甲总量（不低于 0）。"""
    return max(0.0, c.defense - c.shred_total)


def _live_value(c: _Combatant, vid: str) -> float:
    """共鸣取用的「当前值」：hp 为当前生命，atk 含背水一战加成，
    def 为破甲后的有效防御，crit/dodge 含被动与以牙还牙。"""
    if vid == "hp":
        return float(max(0, c.hp))
    if vid == "atk":
        return _eff_atk(c)
    if vid == "def":
        return _eff_def(c)
    if vid == "spd":
        return float(c.spd)
    if vid == "crit":
        return float(c.crit)
    if vid == "dodge":
        return float(c.dodge)
    return 0.0


def _skill_param(c: _Combatant, effect_type: str, key: str, default=0.0):
    """取该斗士第一个指定类型技能的某个参数（快照 buff 展示用）。"""
    for sdef, eff in c.skills:
        if eff.get("type") == effect_type and key in eff:
            return eff.get(key)
    return default


def _hurt(c: _Combatant, amount: float, ev) -> bool:
    """扣除生命；若有未耗尽的不倒意志且本次伤害致命，保留 1 点生命。"""
    c.hp -= amount
    if c.hp <= 0 and c.has_iron_will and not c.iron_will_used:
        c.hp = 1.0
        c.iron_will_used = True
        ev("iron_will", {"a": c.name})
        return True
    return False


def _make_combatant(f: Fighter, pos: int, game: GameCfg) -> _Combatant:
    bc = game.battle
    skills = personalized_effects(f, game)
    dodge = float(f.attrs["dodge"])
    crit = float(f.attrs["crit"])
    pen = 0.0
    has_iron_will = False
    for sdef, eff in skills:
        if sdef.trigger == "passive":
            if eff.get("type") == "dodge_bonus":
                dodge += float(eff.get("value", 0))
            elif eff.get("type") == "crit_bonus":
                crit += float(eff.get("value", 0))
            elif eff.get("type") == "armor_pen":
                pen += float(eff.get("value", 0))
            elif eff.get("type") == "iron_will":
                has_iron_will = True
    dodge = min(dodge, bc.dodge_cap)
    crit = min(crit, bc.crit_cap)
    return _Combatant(
        fighter=f, pos=pos, name=f.name,
        max_hp=f.attrs["hp"], hp=float(f.attrs["hp"]),
        atk=float(f.attrs["atk"]), defense=float(f.attrs["def"]),
        spd=float(f.attrs["spd"]), dodge=dodge, crit=crit,
        element_id=f.element_id, skills=skills,
        pen=min(0.8, pen), has_iron_will=has_iron_will,
    )


def _compute_damage(actor, enemy, mult, crit, game, rng, ratio=1.0) -> float:
    bc = game.battle
    variance = rng.next_gaussian(bc.variance_lo, bc.variance_hi)
    crit_mult = bc.crit_multiplier if crit else 1.0
    raw = _eff_atk(actor) * ratio * variance * crit_mult * mult
    armor = _eff_def(enemy) * (1.0 - actor.pen) * bc.defense_factor
    return max(float(bc.min_damage), raw - armor)


def _snapshot(combatants, threshold: float) -> dict:
    """双方状态快照（按输入位置 a/b），buff 以 id+params 存储、渲染时查 locale。
    参数均为展示格式（百分数 2 位小数 / 其余取整）。"""
    def one(c):
        buffs = []
        if c.poison_turns > 0:
            buffs.append({"id": "poison",
                          "params": {"damage": format_num(c.poison_damage),
                                     "turns": c.poison_turns}})
        if c.bleed_turns > 0:
            buffs.append({"id": "bleed",
                          "params": {"damage": format_num(c.bleed_damage),
                                     "turns": c.bleed_turns}})
        if c.stunned:
            buffs.append({"id": "stun", "params": {}})
        if c.shred_stacks > 0:
            buffs.append({"id": "shred",
                          "params": {"value": format_num(c.shred_total),
                                     "stacks": c.shred_stacks,
                                     "turns": c.shred_turns}})
        if c.last_stand_active:
            buffs.append({"id": "last_stand", "params": {"value": format_pct(c.last_stand_bonus)}})
        if c.hit_streak > 0:
            bonus = float(_skill_param(c, "streak_bonus", "value", 0.0))
            buffs.append({"id": "momentum", "params": {
                "stacks": c.hit_streak,
                "mult": format_pct(1.0 + bonus * c.hit_streak)}})
        if c.grudge_stacks > 0:
            bonus = float(_skill_param(c, "grudge", "value", 0.0))
            buffs.append({"id": "grudge", "params": {
                "stacks": c.grudge_stacks,
                "mult": format_pct(1.0 + bonus * c.grudge_stacks)}})
        if c.retribution_stacks > 0:
            buffs.append({"id": "retribution", "params": {
                "stacks": c.retribution_stacks,
                "crit": format_pct(c.crit / 100.0)}})
        for sdef, eff in c.skills:
            if sdef.trigger != "passive":
                continue
            t = eff.get("type")
            if t == "crit_bonus":
                buffs.append({"id": "crit_up",
                              "params": {"value": format_num(float(eff.get("value", 0)))}})
            elif t == "dodge_bonus":
                buffs.append({"id": "dodge_up",
                              "params": {"value": format_num(float(eff.get("value", 0)))}})
            elif t == "armor_pen":
                buffs.append({"id": "pen_up",
                              "params": {"value": format_pct(float(eff.get("value", 0.0)))}})
            elif t == "blood_pact":
                buffs.append({"id": "blood_pact",
                              "params": {"value": format_pct(float(eff.get("value", 0.0)))}})
            elif t == "iron_will":
                buffs.append({"id": "iron_will_used" if c.iron_will_used else "iron_will",
                              "params": {}})
            elif t == "tempo":
                value = format_num(float(eff.get("value", 0.0)))
                if c.tempo_active:
                    buffs.append({"id": "tempo_up", "params": {"value": value}})
                else:
                    buffs.append({"id": "tempo",
                                  "params": {"tick": int(eff.get("tick", 0)),
                                             "value": value}})
        return {
            "hp": round(max(0.0, float(c.hp)), 2),
            "max_hp": c.max_hp,
            "atk": round(_eff_atk(c), 2),
            "def": round(_eff_def(c), 2),
            "spd": round(float(c.spd), 2),
            "crit": round(float(c.crit), 2),
            "dodge": round(float(c.dodge), 2),
            "gauge": max(0.0, min(100.0, round(c.gauge * 100.0 / threshold, 2))),
            "buffs": buffs,
        }
    return {"a": one(combatants[0]), "b": one(combatants[1])}


def run_battle(fighter_a: Fighter, fighter_b: Fighter, game: GameCfg,
               snapshots: bool = True) -> BattleOutcome:
    """运行一场对战。snapshots=False 时不为战报条目附带状态快照（极速模式，
    供 /api/battle/fast 使用；胜负与事件序列与快照模式完全一致）。"""
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
        state = _snapshot(combatants, bc.gauge_threshold) if snapshots else None
        if tick != last_logged_tick:
            marker = {"tick": tick, "template": "tick_marker",
                      "params": {"tick": tick}}
            if snapshots:
                marker["state"] = state
            events.append(marker)
            last_logged_tick = tick
        entry = {"tick": tick, "template": template, "params": params or {}}
        if snapshots:
            entry["state"] = state
        events.append(entry)

    first, second = internal[0], internal[1]
    ev("battle_start", {
        "a": first.name, "b": second.name,
        "element_a": {"ref": "element", "id": first.element_id},
        "element_b": {"ref": "element", "id": second.element_id},
    })

    # 血契：开战即结算（按内部序），消耗生命换取永久攻击
    for c in internal:
        for sdef, eff in c.skills:
            if sdef.trigger == "passive" and eff.get("type") == "blood_pact":
                cost = c.max_hp * float(eff.get("cost", 0.0))
                c.hp = max(1.0, c.hp - cost)
                c.atk *= (1.0 + float(eff.get("value", 0.0)))
                ev("blood_pact", {"a": c.name, "cost": format_num(cost),
                                  "value": format_pct(float(eff.get("value", 0.0)))})

    winner = None
    draw = False
    while tick < bc.max_ticks and winner is None and not draw:
        tick += 1
        # 大器晚成：到达阈值刻后速度提升（一次性）
        for c in internal:
            if c.tempo_active or c.hp <= 0:
                continue
            for sdef, eff in c.skills:
                if (sdef.trigger == "passive" and eff.get("type") == "tempo"
                        and tick >= int(eff.get("tick", 0))):
                    c.tempo_active = True
                    c.spd = float(c.spd) + float(eff.get("value", 0))
                    ev("tempo_up", {"a": c.name, "spd": format_num(c.spd)})
                    break
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
                actor.poison_turns -= 1
                _hurt(actor, dmg, ev)
                ev("poison_tick", {"a": actor.name, "damage": format_num(dmg)})
                if actor.hp <= 0:
                    ev("poison_death", {"a": actor.name})
                    winner = enemy
                    break
            # 流血（与毒相同：拥有者行动时结算，眩晕不影响）
            if actor.bleed_turns > 0:
                dmg = actor.bleed_damage
                actor.bleed_turns -= 1
                _hurt(actor, dmg, ev)
                ev("bleed_tick", {"a": actor.name, "damage": format_num(dmg)})
                if actor.hp <= 0:
                    ev("bleed_death", {"a": actor.name})
                    winner = enemy
                    break
            # 破甲递减（受方的行动时机）
            if actor.shred_turns > 0:
                actor.shred_turns -= 1
                if actor.shred_turns <= 0:
                    actor.shred_stacks = 0
                    actor.shred_total = 0.0
            # 行动开始技能（净化 / 治疗等）
            for sdef, eff in actor.skills:
                if sdef.trigger != "on_turn_start":
                    continue
                t = eff.get("type")
                if t == "cleanse":
                    if actor.poison_turns <= 0 and actor.bleed_turns <= 0:
                        continue
                    if rng.next_float() > float(eff.get("chance", 1.0)):
                        continue
                    ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sdef.id}})
                    actor.poison_turns = 0
                    actor.poison_damage = 0.0
                    actor.bleed_turns = 0
                    actor.bleed_damage = 0.0
                    healed = min(float(eff.get("value", 0.0)),
                                 actor.max_hp - actor.hp)
                    if healed > 0:
                        actor.hp += healed
                        ev("effect_cleanse", {"a": actor.name, "heal": format_num(healed)})
                    else:
                        ev("effect_cleanse", {"a": actor.name, "heal": format_num(0.0)})
                elif t == "heal":
                    if rng.next_float() > float(eff.get("chance", 1.0)):
                        continue
                    ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sdef.id}})
                    if actor.hp > 0:
                        gained = min(float(eff.get("value", 0.0)),
                                     actor.max_hp - actor.hp)
                        if gained > 0:
                            actor.hp += gained
                            ev("effect_heal", {"a": actor.name, "heal": format_num(gained)})
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


def _after_hit(actor, enemy, dmg, crit, game, ev):
    """一次命中的反应式结算：乘胜追击叠层（攻击方）、
    以牙还牙（防守方被暴击叠暴击）、怨念（防守方挨打叠伤害层）。"""
    bc = game.battle
    for sdef, eff in actor.skills:
        if (eff.get("type") == "streak_bonus"
                and actor.hit_streak < int(eff.get("cap", 0))):
            actor.hit_streak += 1
            ev("streak_up", {
                "a": actor.name, "stacks": actor.hit_streak,
                "mult": format_pct(1.0 + float(eff.get("value", 0.0)) * actor.hit_streak)})
    if crit:
        for sdef, eff in enemy.skills:
            if (eff.get("type") == "retribution"
                    and enemy.retribution_stacks < int(eff.get("cap", 0))):
                enemy.retribution_stacks += 1
                gain = float(eff.get("value", 0.0))
                enemy.crit = min(bc.crit_cap, enemy.crit + gain)
                ev("retribution_stack", {
                    "a": enemy.name, "value": format_num(gain),
                    "crit": format_pct(enemy.crit / 100.0)})
    if dmg > 0 and enemy.hp > 0:
        for sdef, eff in enemy.skills:
            if (eff.get("type") == "grudge"
                    and enemy.grudge_stacks < int(eff.get("cap", 0))):
                enemy.grudge_stacks += 1


def _attack(actor, enemy, game, rng, ev):
    bc = game.battle
    mult = 1.0
    lifesteal = 0.0
    poison = None
    stun = False
    extra_ratios = []
    surge = 0.0
    for sdef, eff in actor.skills:
        if sdef.trigger != "on_attack":
            continue
        # 共鸣修正技能自身参数（触发前修正概率类；其余在使用处修正）
        target = resonance_target(eff, game)
        proc_eff = eff
        if target:
            live_coeff = resonance_coeff(
                lambda vid: _live_value(actor, vid),
                lambda vid: _live_value(enemy, vid),
                eff["link"], game)
            proc_eff = apply_resonance(eff, live_coeff, target)
        roll_chance = float(proc_eff.get("chance", eff.get("chance", 1.0)))
        if rng.next_float() > roll_chance:
            continue
        t = eff.get("type")
        if t in _PROC_LOGGED:
            ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sdef.id}})
        if target:
            ev("effect_link", {
                "a": actor.name,
                "stat": {"ref": "attr", "id": eff["link"]["variable"]},
                "scope": {"ref": "stat_word", "id": "scope_" + eff["link"].get("source", "own")},
                "mode": {"ref": "stat_word", "id": "mode_" + eff["link"].get("mode", "ratio")},
                "field": {"ref": "stat_word", "id": "field_" + target},
                "final": format_resonance_final(proc_eff.get(target), target),
            })
        satisfied = True
        if t == "damage_multiplier":
            cond = eff.get("condition")
            if cond and cond.get("type") == "target_hp_below":
                satisfied = enemy.hp <= enemy.max_hp * float(cond.get("value", 0))
        if not satisfied:
            continue
        if t == "damage_multiplier":
            mult *= float(proc_eff.get("value", eff.get("value", 1.0)))
            if eff.get("condition"):
                ev("effect_execution", {"mult": format_pct(mult)})
            else:
                ev("effect_damage_up", {"mult": format_pct(mult)})
        elif t == "lifesteal":
            lifesteal += float(proc_eff.get("value", eff.get("value", 0)))
        elif t == "poison":
            poison = (float(proc_eff.get("damage", eff.get("damage", 0.0))),
                      int(proc_eff.get("turns", eff.get("turns", 0))))
        elif t == "stun":
            stun = True
        elif t == "extra_strikes":
            extra_ratios.extend(float(x) for x in eff.get("ratios", []))
        elif t == "streak_bonus":
            mult *= 1.0 + float(proc_eff.get("value", 0.0)) * min(
                actor.hit_streak, int(proc_eff.get("cap", 0)))
        elif t == "overload":
            boost = float(proc_eff.get("value", 1.0))
            mult *= boost
            cost = actor.max_hp * float(eff.get("cost", 0.0))
            actor.hp = max(1.0, actor.hp - cost)
            ev("overload_cost", {"a": actor.name, "cost": format_num(cost),
                                 "mult": format_pct(boost)})
        elif t == "gamble":
            if rng.next_float() < float(proc_eff.get("chance", 0.5)):
                boost = float(proc_eff.get("value", 1.0))
                mult *= boost
                ev("gamble_win", {"mult": format_pct(boost)})
            else:
                drop = float(proc_eff.get("penalty", 1.0))
                mult *= drop
                ev("gamble_lose", {"mult": format_pct(drop)})
        elif t == "exploit":
            opening = (enemy.stunned or enemy.poison_turns > 0
                       or enemy.bleed_turns > 0 or enemy.shred_stacks > 0
                       or enemy.gauge >= 0.95 * bc.gauge_threshold)
            if opening:
                boost = 1.0 + float(proc_eff.get("value", 0.0))
                mult *= boost
                ev("effect_exploit", {"mult": format_pct(boost)})
        elif t == "armor_shred":
            if enemy.shred_stacks < int(proc_eff.get("max_stacks", 0)):
                enemy.shred_stacks += 1
                enemy.shred_total += float(proc_eff.get("value", 0.0))
            enemy.shred_turns = max(enemy.shred_turns,
                                    int(proc_eff.get("turns", 0)))
            ev("shred_apply", {"b": enemy.name,
                               "value": format_num(enemy.shred_total),
                               "def": format_num(_eff_def(enemy))})
        elif t == "bleed":
            enemy.bleed_damage = _eff_atk(actor) * float(proc_eff.get("value", 0.0))
            enemy.bleed_turns = int(proc_eff.get("turns", 0))
            ev("bleed_apply", {"b": enemy.name, "damage": format_num(enemy.bleed_damage),
                               "turns": enemy.bleed_turns})
        elif t == "gauge_surge":
            surge += float(proc_eff.get("value", 0.0))

    # 怨念：挨打积累的层数转化为本次攻击伤害加成
    for sdef, eff in actor.skills:
        if (sdef.trigger == "on_defense" and eff.get("type") == "grudge"
                and actor.grudge_stacks > 0):
            mult *= 1.0 + float(eff.get("value", 0.0)) * min(
                actor.grudge_stacks, int(eff.get("cap", 0)))

    # 闪避判定（落空时乘胜追击清零）
    if rng.next_float() < enemy.dodge / 100.0:
        ev("attack_miss", {"a": actor.name, "b": enemy.name})
        for sdef, eff in actor.skills:
            if eff.get("type") == "streak_bonus":
                actor.hit_streak = 0
                break
        return

    crit = rng.next_float() < actor.crit / 100.0
    if crit:
        ev("attack_crit", {})
    dmg = _compute_damage(actor, enemy, mult, crit, game, rng)

    # 防守方技能（减伤 / 反甲 / 坚守）
    for sdef, eff in enemy.skills:
        if sdef.trigger != "on_defense":
            continue
        t = eff.get("type")
        if t in ("grudge", "retribution"):
            continue  # 反应式效果在命中后结算（_after_hit）
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        ev("skill_proc", {"a": enemy.name, "skill": {"ref": "skill", "id": sdef.id}})
        if t == "damage_reduction":
            ratio = float(eff.get("value", 0))
            dmg = max(float(bc.min_damage), dmg * (1.0 - ratio))
            ev("effect_reduction", {"b": enemy.name, "ratio": format_pct(ratio)})
        elif t == "reflect":
            refl = max(1.0, dmg * float(eff.get("value", 0.0)))
            enemy.damage_dealt += refl
            _hurt(actor, refl, ev)
            ev("effect_reflect", {"a": actor.name, "b": enemy.name, "damage": format_num(refl)})
        elif t == "bulwark":
            if enemy.hp >= enemy.max_hp * float(eff.get("threshold", 0.0)):
                ratio = float(eff.get("value", 0.0))
                dmg = max(float(bc.min_damage), dmg * (1.0 - ratio))
                ev("effect_bulwark", {"b": enemy.name, "ratio": format_pct(ratio)})

    _hurt(enemy, dmg, ev)
    actor.damage_dealt += dmg
    ev("attack_hit", {"a": actor.name, "b": enemy.name,
                      "damage": format_num(dmg), "hp": format_num(max(0, enemy.hp))})
    _after_hit(actor, enemy, dmg, crit, game, ev)

    if lifesteal > 0 and dmg > 0 and actor.hp > 0:
        gained = min(dmg * lifesteal, actor.max_hp - actor.hp)
        if gained > 0:
            actor.hp += gained
            ev("effect_lifesteal", {"a": actor.name, "heal": format_num(gained)})
    if poison is not None and enemy.hp > 0 and poison[0] > 0 and poison[1] > 0:
        enemy.poison_damage, enemy.poison_turns = poison
        ev("effect_poison", {"b": enemy.name, "damage": format_num(poison[0]),
                             "turns": poison[1]})
    if stun and enemy.hp > 0:
        enemy.stunned = True
        ev("effect_stun", {"b": enemy.name})
    # 疾影突袭：命中后行动槽额外前进
    if surge > 0 and enemy.hp > 0 and actor.hp > 0:
        actor.gauge += surge
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
            _hurt(enemy, dmg2, ev)
            actor.damage_dealt += dmg2
            ev("attack_hit", {"a": actor.name, "b": enemy.name,
                              "damage": format_num(dmg2), "hp": format_num(max(0, enemy.hp))})
            _after_hit(actor, enemy, dmg2, crit2, game, ev)
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
