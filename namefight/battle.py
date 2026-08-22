"""确定性对战引擎（核心不变量见 AGENTS.md 2.1）。

tick 战斗模型（v0.9.0）：
- 每个 tick 双方行动槽（gauge）累加自身有效速度，达到阈值
  （battle.json 的 gauge_threshold，白板速度 100 / 阈值 1000 ≈ 每 10 刻一动）
  即可行动一次并扣回阈值；速度决定行动频率。
  同一 tick 多人可行动时，按（gauge 余量降序、内部序）依次执行；
- 内部序 = 速度降序、规范化名字升序，与输入顺序无关；
- 属性白板基准 100：伤害公式使用 atk_factor / defense_factor 把面板值
  折算为与旧数值体系等价的实际攻防当量（数值实际不变，量纲归一）；
- 行动开始时依次结算：斩断打断 -> 毒发 -> 流血 -> 行动开始技能（血契/回春/净化）
  -> 眩晕判定 -> 背水一战 -> 攻击；行动结束后结算：大器晚成叠速、
  嗜血增益递减、血契吸血转化；
- 持续类状态（中毒/流血/眩晕/破甲/回春/怨念/锻痕）均以「刻」为期限，
  于每刻开始时统一结算与过期；
- 技能参数经 fighter.personalized_effects 按斗士 MD5 个性化
  （熟练度影响触发概率，共鸣变数修正其余数值字段）；
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
                      personalized_effects, resonance_coeff)
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 引擎支持的技能效果类型（新增效果必须同步此表与 tests/test_config.py）
SUPPORTED_EFFECTS = frozenset({
    "charge", "damage_multiplier", "lifesteal", "poison", "concussive",
    "thunder", "sever", "gauge_surge", "damage_reduction", "reflect",
    "bulwark", "retribution", "iron_will", "heal", "cleanse",
    "low_hp_atk_bonus", "streak_bonus", "overload", "armor_shred",
    "bleed", "gamble", "tempo", "armor_pen", "blood_pact", "grudge",
})

# 引擎会输出的战报模板 id（测试据此校验每个 locale 都有对应文案）
TEMPLATES_USED = frozenset({
    "battle_start", "tick_marker", "turn_stun", "poison_tick", "poison_death",
    "bleed_tick", "bleed_death", "regen_tick", "skill_proc",
    "effect_execution", "effect_lifesteal",
    "effect_poison", "effect_stun", "effect_heal", "regen_mark",
    "effect_reduction", "effect_reflect", "effect_link", "low_hp_trigger",
    "attack_crit", "attack_miss", "attack_hit", "death", "victory", "draw",
    "timeout",
    "charge_start", "charge_release", "thunder_cast", "thunder_hit",
    "sever_proc", "will_trigger", "purify_cleanse", "pact_proc", "pact_gain",
    "immune", "guard_stack", "grudge_stack", "tempo_stack", "lifesteal_buff",
    "shred_apply", "bleed_apply", "overload_cost", "gamble_win", "gamble_lose",
    "streak_up", "effect_bulwark", "retribution_record", "retribution_release",
})

# 引擎会写入状态快照的 buff id（测试据此校验每个 locale 都有对应文案）
BUFF_IDS = frozenset({
    "poison", "bleed", "stun", "shred", "charge", "momentum", "grudge",
    "guard", "retribution", "last_stand", "regen", "lifesteal", "will",
    "will_used", "tempo", "blood_pact",
})

# 触发即记入战报「发动技能」行的效果类型；常驻/姿态类效果以自身专属事件表达
_PROC_LOGGED = frozenset({
    "damage_multiplier", "lifesteal", "poison", "concussive", "thunder",
    "charge", "gauge_surge", "overload", "bleed", "armor_shred", "gamble",
    "sever", "blood_pact", "heal", "cleanse", "streak_bonus",
})

# 防守结算时跳过的反应式类型（打断在行动开始结算，叠层/记录在命中后结算）
_REACTIVE_TYPES = frozenset({"sever", "grudge", "retribution"})


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
    gauge: float = 0.0       # 行动槽
    seq: int = 0             # 内部序（速度降序、名字升序）
    # ---- 刻期限状态 ----
    poison_until: int = 0
    poison_damage: float = 0.0
    bleed_until: int = 0
    bleed_damage: float = 0.0
    stun_until: int = 0
    shred_until: int = 0
    shred_stacks: int = 0
    shred_value: float = 0.0     # 每层破甲量
    grudge_exp: list = field(default_factory=list)   # 怨念层到期刻
    grudge_value: float = 0.0    # 每层伤害加成
    guard_exp: list = field(default_factory=list)    # 锻痕层到期刻
    guard_value: float = 0.0     # 每层伤害减免
    regen_value: float = 0.0     # 回春：每次回复量
    regen_interval: int = 0      # 回春：间隔刻
    regen_next: int = 0
    regen_until: int = 0
    # ---- 行动 / 反应状态 ----
    charging: bool = False
    charge_eff: dict = None
    stunned: bool = False        # 兼容字段（以 stun_until 为准）
    hit_streak: int = 0          # 乘胜追击连击层
    ret_records: list = field(default_factory=list)  # 以牙还牙记录的伤害
    ret_ratio: float = 0.0
    ret_cap: int = 0
    ls_value: float = 0.0        # 嗜血增益：吸血比例
    ls_turns: int = 0            # 剩余行动数
    last_stand_active: bool = False
    last_stand_bonus: float = 0.0
    last_stand_spd: float = 0.0
    tempo_stacks: int = 0        # 大器晚成层数
    will_chance: float = 0.0     # 不屈意志当前触发概率
    will_decay: float = 0.0
    will_heal: float = 0.0
    will_used: int = 0
    pact_active: bool = False    # 血契：本行动已献祭
    pact_steal: float = 0.0
    pact_convert: float = 0.0
    pact_stolen: float = 0.0
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
    shred = c.shred_value * c.shred_stacks if c.shred_until > 0 else 0.0
    return max(0.0, c.defense - shred)


def _eff_spd(c: _Combatant) -> float:
    return c.spd * (1.0 + c.last_stand_spd) if c.last_stand_active else c.spd


def _grudge_stacks(c: _Combatant, tick: int) -> int:
    return sum(1 for t in c.grudge_exp if t > tick)


def _guard_stacks(c: _Combatant, tick: int) -> int:
    return sum(1 for t in c.guard_exp if t > tick)


def _live_value(c: _Combatant, vid: str) -> float:
    """共鸣取用的「当前值」：hp 为当前生命，atk 含背水一战加成，
    def 为破甲后的有效防御，spd 含背水一战速度加成。"""
    if vid == "hp":
        return float(max(0, c.hp))
    if vid == "atk":
        return _eff_atk(c)
    if vid == "def":
        return _eff_def(c)
    if vid == "spd":
        return _eff_spd(c)
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


def _hurt(c: _Combatant, amount: float, ev, rng) -> None:
    """扣除生命；若致命且不屈意志仍可触发，按当前概率判定：
    成功则回复一定百分比最大生命，且触发概率按指数衰减。"""
    c.hp -= amount
    if c.hp <= 0 and c.will_chance > 0:
        if rng.next_float() < c.will_chance:
            c.hp = c.max_hp * c.will_heal
            c.will_chance *= c.will_decay
            c.will_used += 1
            ev("will_trigger", {"a": c.name, "heal": format_num(c.hp)})


def _make_combatant(f: Fighter, pos: int, game: GameCfg) -> _Combatant:
    bc = game.battle
    skills = personalized_effects(f, game)
    c = _Combatant(
        fighter=f, pos=pos, name=f.name,
        max_hp=f.attrs["hp"], hp=float(f.attrs["hp"]),
        atk=float(f.attrs["atk"]), defense=float(f.attrs["def"]),
        spd=float(f.attrs["spd"]), dodge=float(f.attrs["dodge"]),
        crit=float(f.attrs["crit"]),
        element_id=f.element_id, skills=skills,
    )
    for sdef, eff in skills:
        if sdef.trigger == "passive" and eff.get("type") == "iron_will":
            c.will_chance = float(eff.get("chance", 0.0))
            c.will_decay = float(eff.get("decay", 0.0))
            c.will_heal = float(eff.get("value", 0.0))
    c.dodge = min(c.dodge, bc.dodge_cap)
    c.crit = min(c.crit, bc.crit_cap)
    return c


def _compute_damage(actor, enemy, mult, crit, game, rng, pen=0.0) -> float:
    bc = game.battle
    variance = rng.next_gaussian(bc.variance_lo, bc.variance_hi)
    crit_mult = bc.crit_multiplier if crit else 1.0
    raw = _eff_atk(actor) * bc.atk_factor * variance * crit_mult * mult
    armor = _eff_def(enemy) * (1.0 - pen) * bc.defense_factor
    return max(float(bc.min_damage), raw - armor)


def _snapshot(combatants, threshold: float, tick: int) -> dict:
    """双方状态快照（按输入位置 a/b），buff 以 id+params 存储、渲染时查 locale。
    参数均为展示格式（百分数 2 位小数 / 其余取整）；
    gauge_gain 为每刻行动槽推进百分比（前端逐刻动画用）。"""
    def one(c):
        buffs = []
        if c.poison_until > tick:
            buffs.append({"id": "poison",
                          "params": {"damage": format_num(c.poison_damage),
                                     "turns": max(0, c.poison_until - tick)}})
        if c.bleed_until > tick:
            buffs.append({"id": "bleed",
                          "params": {"damage": format_num(c.bleed_damage),
                                     "turns": max(0, c.bleed_until - tick)}})
        if c.stun_until > tick:
            buffs.append({"id": "stun", "params": {"turns": c.stun_until - tick}})
        if c.shred_until > tick and c.shred_stacks > 0:
            buffs.append({"id": "shred",
                          "params": {"value": format_num(c.shred_value * c.shred_stacks),
                                     "stacks": c.shred_stacks,
                                     "turns": c.shred_until - tick}})
        if c.charging:
            buffs.append({"id": "charge", "params": {}})
        if c.last_stand_active:
            buffs.append({"id": "last_stand",
                          "params": {"value": format_pct(c.last_stand_bonus),
                                     "spd": format_pct(c.last_stand_spd)}})
        if c.hit_streak > 0:
            bonus = float(_skill_param(c, "streak_bonus", "value", 0.0))
            buffs.append({"id": "momentum", "params": {
                "stacks": c.hit_streak,
                "mult": format_pct(1.0 + bonus * c.hit_streak)}})
        grudge_n = _grudge_stacks(c, tick)
        if grudge_n > 0:
            buffs.append({"id": "grudge", "params": {
                "stacks": grudge_n,
                "mult": format_pct(1.0 + c.grudge_value * grudge_n)}})
        guard_n = _guard_stacks(c, tick)
        if guard_n > 0:
            buffs.append({"id": "guard", "params": {
                "stacks": guard_n,
                "value": format_pct(min(0.75, c.guard_value * guard_n))}})
        if c.ret_records:
            buffs.append({"id": "retribution", "params": {
                "stacks": len(c.ret_records),
                "value": format_num(sum(c.ret_records))}})
        if c.regen_until > tick:
            buffs.append({"id": "regen", "params": {
                "value": format_num(c.regen_value),
                "tick": c.regen_interval,
                "turns": c.regen_until - tick}})
        if c.ls_turns > 0:
            buffs.append({"id": "lifesteal", "params": {
                "value": format_pct(c.ls_value), "turns": c.ls_turns}})
        for sdef, eff in c.skills:
            t = eff.get("type")
            if sdef.trigger == "passive" and t == "iron_will":
                buffs.append({"id": "will_used" if c.will_used else "will",
                              "params": {"value": format_pct(c.will_chance),
                                         "heal": format_pct(c.will_heal)}})
            elif sdef.trigger == "passive" and t == "tempo" and c.tempo_stacks > 0:
                buffs.append({"id": "tempo", "params": {
                    "stacks": c.tempo_stacks,
                    "value": format_num(
                        c.tempo_stacks * float(eff.get("value", 0.0)))}})
            elif sdef.trigger == "on_turn_start" and t == "blood_pact":
                buffs.append({"id": "blood_pact",
                              "params": {"value": format_pct(float(eff.get("value", 0.0))),
                                         "convert": format_pct(
                                             float(eff.get("convert", 0.0)))}})
        spd = _eff_spd(c)
        return {
            "hp": round(max(0.0, float(c.hp)), 2),
            "max_hp": c.max_hp,
            "atk": round(_eff_atk(c), 2),
            "def": round(_eff_def(c), 2),
            "spd": round(spd, 2),
            "crit": round(float(c.crit), 2),
            "dodge": round(float(c.dodge), 2),
            "gauge": max(0.0, min(100.0, round(c.gauge * 100.0 / threshold, 2))),
            "gauge_gain": round(spd * 100.0 / threshold, 2),
            "buffs": buffs,
        }
    return {"a": one(combatants[0]), "b": one(combatants[1])}


def _proc_eff_of(actor, enemy, eff, game):
    """按双方当前值应用全部共鸣变数，返回该次触发的实际参数。"""
    proc = eff
    links = eff.get("links")
    if links:
        proc = dict(eff)
        for link in links:
            field = str(link.get("field"))
            if field not in proc:
                continue
            coeff = resonance_coeff(
                lambda vid: _live_value(actor, vid),
                lambda vid: _live_value(enemy, vid),
                link, game)
            proc = apply_resonance(proc, coeff, field)
    return proc


def _emit_link_events(actor, eff, proc, ev):
    for link in eff.get("links", ()):
        field = str(link.get("field"))
        if field not in proc:
            continue
        mode = str(link.get("mode", "own"))
        ev("effect_link", {
            "a": actor.name,
            "stat": {"ref": "attr", "id": link.get("variable")},
            "scope": {"ref": "stat_word", "id": "scope_" + mode},
            "field": {"ref": "stat_word", "id": "field_" + field},
            "final": format_resonance_final(proc.get(field), field, eff),
        })


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
        state = _snapshot(combatants, bc.gauge_threshold, tick) if snapshots else None
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

    winner = None
    draw = False

    def _settle_deaths(actor, enemy):
        """一次行动后的死亡结算；返回 True 表示战斗结束。"""
        nonlocal winner, draw
        if actor.hp <= 0 and enemy.hp <= 0:
            draw = True
        elif enemy.hp <= 0:
            ev("death", {"b": enemy.name})
            winner = actor
        elif actor.hp <= 0:
            ev("death", {"b": actor.name})
            winner = enemy
        return winner is not None or draw

    while tick < bc.max_ticks and winner is None and not draw:
        tick += 1
        # ---- 每刻开始：回春回复与到期层清理（按内部序，确定性顺序） ----
        for c in internal:
            if c.hp <= 0:
                continue
            if c.regen_until > tick - 1 and tick >= c.regen_next:
                gained = min(c.regen_value, c.max_hp - c.hp)
                if gained > 0:
                    c.hp += gained
                    ev("regen_tick", {"a": c.name, "heal": format_num(gained)})
                c.regen_next += max(1, c.regen_interval)
            c.grudge_exp = [t for t in c.grudge_exp if t > tick - 1]
            c.guard_exp = [t for t in c.guard_exp if t > tick - 1]
        # ---- 行动槽推进 ----
        for c in combatants:
            if c.hp > 0:
                c.gauge += _eff_spd(c)
        ready = [c for c in internal if c.hp > 0 and c.gauge >= bc.gauge_threshold]
        ready.sort(key=lambda c: (-c.gauge, c.seq))
        for actor in ready:
            enemy = internal[1] if actor is internal[0] else internal[0]
            actor.gauge -= bc.gauge_threshold
            if actor.hp <= 0 or enemy.hp <= 0:
                break
            # ---- 斩断：敌方即将行动时打断其回合并反击 ----
            interrupted = False
            if enemy.hp > 0:
                for sdef, eff in enemy.skills:
                    if (sdef.trigger != "on_defense"
                            or eff.get("type") != "sever"):
                        continue
                    if rng.next_float() > float(eff.get("chance", 1.0)):
                        continue
                    proc = _proc_eff_of(enemy, actor, eff, game)
                    ev("skill_proc", {"a": enemy.name,
                                      "skill": {"ref": "skill", "id": sdef.id}})
                    _emit_link_events(enemy, eff, proc, ev)
                    ev("sever_proc", {"a": enemy.name, "b": actor.name})
                    actor.gauge = max(0.0, actor.gauge - float(proc.get("delay", 0.0)))
                    _quick_strike(enemy, actor, float(proc.get("value", 0.5)),
                                  game, rng, ev, tick)
                    interrupted = True
                    break
            if interrupted:
                if _settle_deaths(enemy, actor):
                    break
                continue
            # ---- 毒发（拥有者行动时机；眩晕与打断不影响毒的到期，仅跳过行动） ----
            if actor.poison_until > tick:
                dmg = actor.poison_damage
                _hurt(actor, dmg, ev, rng)
                ev("poison_tick", {"a": actor.name, "damage": format_num(dmg)})
                if actor.hp <= 0:
                    ev("poison_death", {"a": actor.name})
                    winner = enemy
                    break
            # ---- 流血 ----
            if actor.bleed_until > tick:
                dmg = actor.bleed_damage
                _hurt(actor, dmg, ev, rng)
                ev("bleed_tick", {"a": actor.name, "damage": format_num(dmg)})
                if actor.hp <= 0:
                    ev("bleed_death", {"a": actor.name})
                    winner = enemy
                    break
            # ---- 行动开始技能：血契 / 回春术 / 净化 ----
            for sdef, eff in actor.skills:
                if sdef.trigger != "on_turn_start":
                    continue
                t = eff.get("type")
                if t not in ("blood_pact", "heal", "cleanse"):
                    continue
                if rng.next_float() > float(eff.get("chance", 1.0)):
                    continue
                proc = _proc_eff_of(actor, enemy, eff, game)
                ev("skill_proc", {"a": actor.name,
                                  "skill": {"ref": "skill", "id": sdef.id}})
                _emit_link_events(actor, eff, proc, ev)
                if t == "blood_pact":
                    cost = actor.max_hp * float(proc.get("cost", 0.0))
                    actor.hp = max(1.0, actor.hp - cost)
                    actor.pact_active = True
                    actor.pact_steal = float(proc.get("value", 0.0))
                    actor.pact_convert = float(proc.get("convert", 0.0))
                    actor.pact_stolen = 0.0
                    ev("pact_proc", {"a": actor.name, "cost": format_num(cost),
                                     "value": format_pct(actor.pact_steal)})
                elif t == "heal":
                    gained = min(float(proc.get("value", 0.0)),
                                 actor.max_hp - actor.hp)
                    if gained > 0:
                        actor.hp += gained
                        ev("effect_heal", {"a": actor.name, "heal": format_num(gained)})
                    actor.regen_value = float(proc.get("regen", 0.0))
                    actor.regen_interval = max(1, int(proc.get("tick", 1)))
                    actor.regen_next = tick + actor.regen_interval
                    actor.regen_until = tick + max(1, int(proc.get("duration", 1)))
                    ev("regen_mark", {"a": actor.name,
                                      "value": format_num(actor.regen_value),
                                      "tick": actor.regen_interval,
                                      "turns": actor.regen_until - tick})
                elif t == "cleanse":
                    count = _dispel_all(combatants, tick)
                    healed = min(float(proc.get("value", 0.0))
                                 + float(proc.get("per", 0.0)) * count,
                                 actor.max_hp - actor.hp)
                    if healed > 0:
                        actor.hp += healed
                    ev("purify_cleanse", {"a": actor.name, "count": count,
                                          "heal": format_num(healed)})
            if winner is not None:
                break
            # ---- 眩晕：消耗本次行动 ----
            if actor.stun_until > tick:
                ev("turn_stun", {"a": actor.name})
                continue
            # ---- 背水一战（生命低于阈值时一次性触发，攻击与速度双加成） ----
            for sdef, eff in actor.skills:
                if sdef.trigger != "passive" or eff.get("type") != "low_hp_atk_bonus":
                    continue
                proc = _proc_eff_of(actor, enemy, eff, game)
                threshold = float(proc.get("threshold", 0.3))
                if not actor.last_stand_active and actor.hp < actor.max_hp * threshold:
                    actor.last_stand_active = True
                    actor.last_stand_bonus = float(proc.get("value", 0.5))
                    actor.last_stand_spd = float(proc.get("spd", 0.0))
                    ev("low_hp_trigger", {
                        "a": actor.name, "value": format_pct(actor.last_stand_bonus),
                        "spd": format_pct(actor.last_stand_spd)})
            _attack(actor, enemy, game, rng, ev, tick)
            if _settle_deaths(actor, enemy):
                break
            # ---- 行动结束后：大器晚成叠速 / 嗜血增益递减 / 血契转化 ----
            if actor.hp > 0:
                for sdef, eff in actor.skills:
                    if (sdef.trigger != "passive" or eff.get("type") != "tempo"):
                        continue
                    if rng.next_float() > float(eff.get("chance", 1.0)):
                        continue
                    proc = _proc_eff_of(actor, enemy, eff, game)
                    gain = float(proc.get("value", 0.0))
                    actor.spd += gain
                    actor.tempo_stacks += 1
                    ev("tempo_stack", {"a": actor.name, "stacks": actor.tempo_stacks,
                                       "spd": format_num(actor.spd)})
                    break
            if actor.ls_turns > 0:
                actor.ls_turns -= 1
                if actor.ls_turns <= 0:
                    actor.ls_value = 0.0
            if actor.pact_active:
                actor.pact_active = False
                gain = actor.pact_stolen * actor.pact_convert
                if gain > 0:
                    actor.atk += gain
                    ev("pact_gain", {"a": actor.name, "value": format_num(gain),
                                     "atk": format_num(actor.atk)})

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


def _dispel_all(combatants, tick: int) -> int:
    """驱散双方所有标记与增益减益（永久成长与已转化的属性不在此列）。
    返回驱散的种类数（供净化回复计算）。"""
    count = 0
    for c in combatants:
        if c.hp <= 0:
            continue
        if c.poison_until > tick:
            c.poison_until = 0
            count += 1
        if c.bleed_until > tick:
            c.bleed_until = 0
            count += 1
        if c.stun_until > tick:
            c.stun_until = 0
            count += 1
        if c.shred_until > tick and c.shred_stacks > 0:
            c.shred_until = 0
            c.shred_stacks = 0
            count += 1
        if c.charging:
            c.charging = False
            c.charge_eff = None
            count += 1
        if c.hit_streak > 0:
            c.hit_streak = 0
            count += 1
        if c.grudge_exp:
            c.grudge_exp = []
            count += 1
        if c.guard_exp:
            c.guard_exp = []
            count += 1
        if c.ret_records:
            c.ret_records = []
            count += 1
        if c.ls_turns > 0:
            c.ls_turns = 0
            c.ls_value = 0.0
            count += 1
        if c.regen_until > tick:
            c.regen_until = 0
            count += 1
    return count


def _on_hit_reactions(actor, enemy, dmg, crit, game, rng, ev, tick):
    """一次命中后的反应式结算（乘胜追击叠层 / 怨念积攒 / 以牙还牙记录）。"""
    if dmg <= 0 or enemy.hp <= 0:
        return
    for sdef, eff in actor.skills:
        if eff.get("type") != "streak_bonus":
            continue
        if actor.hit_streak >= int(eff.get("cap", 0)):
            continue
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        actor.hit_streak += 1
        ev("streak_up", {
            "a": actor.name, "stacks": actor.hit_streak,
            "mult": format_pct(1.0 + float(eff.get("value", 0.0)) * actor.hit_streak)})
        break
    for sdef, eff in enemy.skills:
        if eff.get("type") != "grudge":
            continue
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        proc = _proc_eff_of(enemy, actor, eff, game)
        enemy.grudge_value = float(proc.get("value", 0.0))
        enemy.grudge_exp.append(tick + max(1, int(proc.get("ticks", 1))))
        stacks = _grudge_stacks(enemy, tick)
        ev("grudge_stack", {"a": enemy.name, "stacks": stacks,
                            "mult": format_pct(1.0 + enemy.grudge_value * stacks)})
        break
    for sdef, eff in enemy.skills:
        if eff.get("type") != "retribution":
            continue
        if len(enemy.ret_records) >= int(eff.get("cap", 0)):
            continue
        if rng.next_float() > float(eff.get("chance", 1.0)):
            continue
        proc = _proc_eff_of(enemy, actor, eff, game)
        enemy.ret_ratio = float(proc.get("ratio", 1.0))
        enemy.ret_cap = int(proc.get("cap", 0))
        enemy.ret_records.append(dmg)
        ev("retribution_record", {"a": enemy.name, "damage": format_num(dmg),
                                  "stacks": len(enemy.ret_records)})
        break


def _defend(enemy, actor, dmg, game, rng, ev, tick):
    """防守方结算：锻痕减伤 -> 坚守壁垒（免疫/减伤）-> 荆棘反甲（免伤+反弹）
    -> 锻痕叠层。返回最终伤害（反弹伤害直接作用于攻击方）。"""
    bc = game.battle
    guard_n = _guard_stacks(enemy, tick)
    if guard_n > 0 and dmg > 0:
        ratio = min(0.75, enemy.guard_value * guard_n)
        dmg = max(float(bc.min_damage), dmg * (1.0 - ratio))
        ev("effect_reduction", {"b": enemy.name, "ratio": format_pct(ratio)})
    for sdef, eff in enemy.skills:
        if sdef.trigger != "on_defense":
            continue
        t = eff.get("type")
        if t in _REACTIVE_TYPES or t == "damage_reduction":
            continue
        if t == "bulwark":
            proc = _proc_eff_of(enemy, actor, eff, game)
            if enemy.hp < enemy.max_hp * float(proc.get("threshold", 0.0)):
                continue
            if dmg > 0 and rng.next_float() < float(proc.get("immune", 0.0)):
                ev("immune", {"b": enemy.name})
                return 0.0
            ratio = float(proc.get("value", 0.0))
            if dmg > 0 and ratio > 0:
                dmg = max(float(bc.min_damage), dmg * (1.0 - ratio))
                ev("effect_bulwark", {"b": enemy.name, "ratio": format_pct(ratio)})
        elif t == "reflect":
            if dmg <= 0 or rng.next_float() > float(eff.get("chance", 1.0)):
                continue
            proc = _proc_eff_of(enemy, actor, eff, game)
            ev("skill_proc", {"a": enemy.name, "skill": {"ref": "skill", "id": sdef.id}})
            split = min(0.9, float(proc.get("value", 0.0)))
            avoided = dmg * split
            reflected = avoided * float(proc.get("ratio", 1.0))
            dmg -= avoided
            enemy.damage_dealt += reflected
            _hurt(actor, reflected, ev, rng)
            ev("effect_reflect", {"a": actor.name, "b": enemy.name,
                                  "damage": format_num(reflected)})
    if dmg > 0:
        for sdef, eff in enemy.skills:
            if (sdef.trigger != "on_defense"
                    or eff.get("type") != "damage_reduction"):
                continue
            if rng.next_float() > float(eff.get("chance", 1.0)):
                continue
            proc = _proc_eff_of(enemy, actor, eff, game)
            ev("skill_proc", {"a": enemy.name, "skill": {"ref": "skill", "id": sdef.id}})
            enemy.guard_value = float(proc.get("value", 0.0))
            enemy.guard_exp.append(tick + max(1, int(proc.get("ticks", 1))))
            stacks = _guard_stacks(enemy, tick)
            ev("guard_stack", {"b": enemy.name, "stacks": stacks,
                               "value": format_pct(min(0.75, enemy.guard_value * stacks))})
            break
    return dmg


def _apply_lifesteal(actor, dmg, ev):
    """命中后的吸血结算（嗜血增益 + 血契献祭共享同一口吸血）。"""
    steal = 0.0
    if actor.ls_turns > 0:
        steal += actor.ls_value
    if actor.pact_active:
        steal += actor.pact_steal
    if steal <= 0 or dmg <= 0 or actor.hp <= 0:
        return
    gained = min(dmg * steal, actor.max_hp - actor.hp)
    if gained > 0:
        actor.hp += gained
        actor.pact_stolen += gained
        ev("effect_lifesteal", {"a": actor.name, "heal": format_num(gained)})


def _quick_strike(attacker, victim, mult, game, rng, ev, tick):
    """斩断反击：一次小倍率的普通打击（可闪避可暴击，不吃攻击技能链）。"""
    if rng.next_float() < victim.dodge / 100.0:
        ev("attack_miss", {"a": attacker.name, "b": victim.name})
        return
    crit = rng.next_float() < attacker.crit / 100.0
    if crit:
        ev("attack_crit", {})
    dmg = _compute_damage(attacker, victim, mult, crit, game, rng)
    dmg = _defend(victim, attacker, dmg, game, rng, ev, tick)
    if dmg > 0:
        _hurt(victim, dmg, ev, rng)
        attacker.damage_dealt += dmg
        ev("attack_hit", {"a": attacker.name, "b": victim.name,
                          "damage": format_num(dmg),
                          "hp": format_num(max(0, victim.hp))})
        _apply_lifesteal(attacker, dmg, ev)
        _on_hit_reactions(attacker, victim, dmg, crit, game, rng, ev, tick)


def _attack(actor, enemy, game: GameCfg, rng, ev, tick: int):
    bc = game.battle

    # ---- 蓄力释放：必定命中、暴击率提升的巨大一击（替换常规攻击） ----
    if actor.charging:
        actor.charging = False
        eff = actor.charge_eff or {}
        proc = _proc_eff_of(actor, enemy, eff, game)
        actor.charge_eff = None
        mult = float(proc.get("value", 3.0))
        crit_bonus = float(proc.get("crit", 0.0))
        ev("charge_release", {"a": actor.name, "mult": format_pct(mult),
                              "crit": format_num(crit_bonus)})
        crit = rng.next_float() < min(bc.crit_cap, actor.crit + crit_bonus) / 100.0
        if crit:
            ev("attack_crit", {})
        dmg = _compute_damage(actor, enemy, mult, crit, game, rng)
        dmg = _defend(enemy, actor, dmg, game, rng, ev, tick)
        if dmg > 0:
            _hurt(enemy, dmg, ev, rng)
            actor.damage_dealt += dmg
            ev("attack_hit", {"a": actor.name, "b": enemy.name,
                              "damage": format_num(dmg),
                              "hp": format_num(max(0, enemy.hp))})
            _apply_lifesteal(actor, dmg, ev)
            _on_hit_reactions(actor, enemy, dmg, crit, game, rng, ev, tick)
        return

    # ---- 攻击技能链 ----
    mult = 1.0
    pen = 0.0
    crit_flat = 0.0
    poison_data = None
    bleed_data = None
    shred_data = None
    stun_ticks = 0
    surge_proc = None
    thunder_proc = None
    for sdef, eff in actor.skills:
        if sdef.trigger != "on_attack":
            continue
        proc = _proc_eff_of(actor, enemy, eff, game)
        if rng.next_float() > float(proc.get("chance", eff.get("chance", 1.0))):
            continue
        t = eff.get("type")
        if t in _PROC_LOGGED:
            ev("skill_proc", {"a": actor.name, "skill": {"ref": "skill", "id": sdef.id}})
        _emit_link_events(actor, eff, proc, ev)
        if t == "charge":
            actor.charging = True
            actor.charge_eff = eff
            ev("charge_start", {"a": actor.name,
                                "mult": format_pct(float(proc.get("value", 3.0)))})
            return  # 本次行动用于蓄力
        if t == "thunder":
            thunder_proc = proc
            break  # 雷罚替换攻击，其余攻击技能不再结算
        if t == "damage_multiplier":
            threshold = float(proc.get("threshold", 0.0))
            if enemy.hp <= enemy.max_hp * threshold:
                mult *= float(proc.get("value", 1.0))
                ev("effect_execution", {"mult": format_pct(mult)})
        elif t == "overload":
            boost = float(proc.get("value", 1.0))
            mult *= boost
            cost = actor.max_hp * float(proc.get("cost", 0.0))
            actor.hp = max(1.0, actor.hp - cost)
            ev("overload_cost", {"a": actor.name, "cost": format_num(cost),
                                 "mult": format_pct(boost)})
        elif t == "gamble":
            if rng.next_float() < float(proc.get("chance", 0.5)):
                boost = float(proc.get("value", 1.0))
                mult *= boost
                ev("gamble_win", {"mult": format_pct(boost)})
            else:
                drop = float(proc.get("penalty", 1.0))
                mult *= drop
                ev("gamble_lose", {"mult": format_pct(drop)})
        elif t == "streak_bonus":
            mult *= 1.0 + float(proc.get("value", 0.0)) * min(
                actor.hit_streak, int(proc.get("cap", 0)))
        elif t == "lifesteal":
            actor.ls_value = float(proc.get("value", 0.0))
            actor.ls_turns = max(1, int(proc.get("turns", 1)))
            ev("lifesteal_buff", {"a": actor.name,
                                  "value": format_pct(actor.ls_value),
                                  "turns": actor.ls_turns})
        elif t == "poison":
            poison_data = (float(proc.get("damage", 0.0)),
                           max(1, int(proc.get("ticks", 1))))
        elif t == "bleed":
            bleed_data = (float(proc.get("value", 0.0)),
                          max(1, int(proc.get("ticks", 1))))
        elif t == "armor_shred":
            shred_data = (float(proc.get("value", 0.0)),
                          max(1, int(proc.get("ticks", 1))),
                          int(proc.get("max_stacks", 1)))
        elif t == "concussive":
            pen = max(pen, float(proc.get("value", 0.0)))
            stun_ticks = max(1, int(proc.get("ticks", 1)))
        elif t == "armor_pen":
            pen = 1.0
            crit_flat += float(proc.get("crit", 0.0))
        elif t == "gauge_surge":
            surge_proc = proc

    # 怨念：挨打积累的层数转化为本次攻击伤害加成（无上限，按到期刻计算）
    grudge_n = _grudge_stacks(actor, tick)
    if grudge_n > 0 and actor.grudge_value > 0:
        mult *= 1.0 + actor.grudge_value * grudge_n

    # ---- 雷罚：连续真实伤害替换本次攻击 ----
    if thunder_proc is not None:
        value = float(thunder_proc.get("value", 0.3))
        decay = float(thunder_proc.get("decay", 0.9))
        chance = float(thunder_proc.get("chance", 0.8))
        max_hits = max(1, int(thunder_proc.get("max_hits", 1)))
        ev("thunder_cast", {"a": actor.name, "value": format_pct(value),
                            "max": max_hits})
        landed = 0
        for i in range(1, max_hits + 1):
            if i > 1 and rng.next_float() >= chance * (decay ** (i - 1)):
                break
            dmg = max(float(bc.min_damage),
                      _eff_atk(actor) * bc.atk_factor * value
                      * rng.next_gaussian(bc.variance_lo, bc.variance_hi))
            _hurt(enemy, dmg, ev, rng)
            actor.damage_dealt += dmg
            landed += 1
            ev("thunder_hit", {"a": actor.name, "b": enemy.name,
                               "damage": format_num(dmg), "hit": i,
                               "hp": format_num(max(0, enemy.hp))})
            _on_hit_reactions(actor, enemy, dmg, False, game, rng, ev, tick)
            if enemy.hp <= 0:
                break
        return  # 攻击已被替换：不结算吸血/上毒/眩晕等

    # ---- 闪避判定（落空时乘胜追击清零） ----
    if rng.next_float() < enemy.dodge / 100.0:
        ev("attack_miss", {"a": actor.name, "b": enemy.name})
        for sdef, eff in actor.skills:
            if eff.get("type") == "streak_bonus":
                actor.hit_streak = 0
                break
        return

    crit = rng.next_float() < min(bc.crit_cap, actor.crit + crit_flat) / 100.0
    if crit:
        ev("attack_crit", {})
    dmg = _compute_damage(actor, enemy, mult, crit, game, rng, pen=pen)
    dmg = _defend(enemy, actor, dmg, game, rng, ev, tick)

    # 以牙还牙：下次攻击时把记录的伤害按倍率追加打出
    if actor.ret_records:
        bonus = sum(actor.ret_records) * actor.ret_ratio
        actor.ret_records = []
        if bonus > 0:
            dmg += bonus
            ev("retribution_release", {"a": actor.name,
                                       "value": format_num(bonus),
                                       "ratio": format_pct(actor.ret_ratio)})

    if dmg > 0:
        _hurt(enemy, dmg, ev, rng)
        actor.damage_dealt += dmg
    ev("attack_hit", {"a": actor.name, "b": enemy.name,
                      "damage": format_num(dmg),
                      "hp": format_num(max(0, enemy.hp))})
    _apply_lifesteal(actor, dmg, ev)
    _on_hit_reactions(actor, enemy, dmg, crit, game, rng, ev, tick)

    if shred_data is not None and enemy.hp > 0:
        value, sticks, max_stacks = shred_data
        if enemy.shred_stacks < max_stacks:
            enemy.shred_stacks += 1
        enemy.shred_value = max(enemy.shred_value, value)
        enemy.shred_until = tick + sticks
        ev("shred_apply", {"b": enemy.name,
                           "value": format_num(enemy.shred_value * enemy.shred_stacks),
                           "def": format_num(_eff_def(enemy))})
    if poison_data is not None and enemy.hp > 0 and poison_data[0] > 0:
        enemy.poison_damage, enemy.poison_until = (poison_data[0], tick + poison_data[1])
        ev("effect_poison", {"b": enemy.name, "damage": format_num(poison_data[0]),
                             "turns": poison_data[1]})
    if bleed_data is not None and enemy.hp > 0 and bleed_data[0] > 0:
        enemy.bleed_damage = _eff_atk(actor) * bc.atk_factor * bleed_data[0]
        enemy.bleed_until = tick + bleed_data[1]
        ev("bleed_apply", {"b": enemy.name, "damage": format_num(enemy.bleed_damage),
                           "turns": bleed_data[1]})
    if stun_ticks > 0 and enemy.hp > 0:
        enemy.stun_until = tick + stun_ticks
        ev("effect_stun", {"b": enemy.name})
    if surge_proc is not None and enemy.hp > 0 and actor.hp > 0:
        actor.gauge += float(surge_proc.get("crit_value" if crit else "value", 0.0))


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
