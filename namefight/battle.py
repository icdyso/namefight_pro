"""确定性对战引擎（核心不变量见 AGENTS.md 2.1）。

v2.0.0 起技能逻辑完全数据化，battle.py 只保留**调度器**角色：

- 执行时机（钩子）、触发条件、参数规格声明于 effects.py 注册表，
  技能图存于 skills.json（nodes + edges，见 effects.py 模块说明）；
- 效果原语（op）在本模块以 _OP_IMPL 注册表实现，与 effects.OPS 一一对应；
  复合原语已拆解为正交原语（壁垒 = damage_reduce + immune_chance、
  血契 = pact_cost + pact_convert），携带 status 参数的原语显式声明
  写入哪条状态定义；
- 状态按**行为种类**结算（statuses.by_kind 按施加顺序遍历）：施加行为在
  _STATUS_APPLY 注册表（dot 依据状态定义的 timing/power 区分每刻毒 /
  行动流血），事件模板 id 由状态定义的 event 字段决定——自建同类状态
  无需改引擎即可参与战斗、驱散与快照展示。

tick 战斗模型（不变）：
- 每个 tick 双方行动槽（gauge）累加自身有效速度，达到阈值（10000，
  速度 ~1000 ≈ 每 10 刻一动）即可行动一次并扣回阈值；
  同刻多人行动按（gauge 余量降序、内部序）执行；内部序 = 速度降序、
  规范化名字升序，与输入顺序无关；
- 行动结算顺序：斩断打断 -> 流血 -> 行动开始钩子 -> 眩晕 -> 攻击前钩子
  -> 攻击（蓄力释放优先）；行动后：成长钩子、嗜血递减；
- 数值量纲与取整政策不变：全程浮点、最终应用时取整一次（_r）；
  伤害 raw = 有效ATK × 三角浮动 × 暴击倍率 × 技能倍率，
  免伤率 = 有效DEF / (有效DEF + defense_constant) × (1 − 穿透)，
  dmg = max(min_damage, raw × (1 − 免伤))；
- 随机数消耗顺序 = （技能派生顺序 × 触发节点数组顺序 × 边数组顺序），
  条件失败即跳过其下游子树（改变即 breaking，见 AGENTS.md 2.1.4）；
- 战报协议不变：模板 id + 参数 + rich 段 + 双方状态快照；角色名一律为
  「【称号】名字」（_Combatant.name）；record=False 时不记录战报（真战力
  批量模拟），随机数消耗与胜负完全一致。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from . import effects, statuses
from .config import GameCfg
from .fighter import (Fighter, apply_resonance, compose_title_name,
                      format_resonance_final, personalized_effects,
                      resonance_coeff)
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 引擎固定输出的战报模板 id（状态施加/叠层类事件由状态定义的 event 字段
# 决定，见 _STATUS_APPLY；测试据此与状态定义共同校验文案齐全；v2.0.0
# 删除了从未使用的 effect_damage_up 死模板）
TEMPLATES_USED = frozenset({
    "battle_start", "tick_marker", "turn_stun", "poison_tick", "poison_death",
    "bleed_tick", "bleed_death", "regen_tick", "skill_proc", "attack_start",
    "effect_execution", "effect_lifesteal",
    "effect_heal", "effect_reduction", "effect_link", "low_hp_trigger",
    "attack_crit", "attack_miss", "attack_hit", "death", "victory", "draw",
    "timeout",
    "charge_start", "charge_release", "thunder_cast", "thunder_hit",
    "sever_proc", "will_trigger", "purify_cleanse", "pact_proc", "pact_gain",
    "immune", "tempo_stack", "overload_cost", "gamble_win", "gamble_lose",
    "effect_bulwark", "retribution_release",
})

# 「使用了技能」行的输出挂点（其余挂点的效果以自身专属事件表达）
_PROC_HOOKS = frozenset({"on_attack", "on_defend", "action_start",
                         "action_interrupt"})

# 富文本段种类：阵营名（红/蓝加粗）、技能名（各自配色加粗）、伤害（红）、治疗（绿）
_RICH_NAME_KEYS = frozenset({"a", "b", "winner"})
_RICH_DAMAGE_KEYS = frozenset({"damage", "cost"})
_RICH_HEAL_KEYS = frozenset({"heal"})
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@dataclass
class _Combatant:
    fighter: Fighter
    pos: int                 # 在输入中的位置 0/1（快照键 a/b 与此对应）
    name: str                # 战报显示名：【称号】名字（v1.2.1）
    plain_name: str          # 原始名字（胜负方等 API 字段用）
    max_hp: float
    hp: float
    atk: float
    defense: float
    spd: float
    dodge: float             # 百分数
    crit: float              # 百分数
    skills: list             # [(SkillDef, 个性化技能图, 编译计划), ...] 按派生顺序
    gauge: float = 0.0       # 行动槽
    seq: int = 0             # 内部序（速度降序、名字升序）
    st: dict = field(default_factory=dict)      # 状态容器：状态id -> 运行时dict
    markers: set = field(default_factory=set)   # 标记集合（一次性/姿态开关）
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


def _r(x: float) -> int:
    """计算结果取整（v0.10.0）：多步浮点计算只在最终应用时取整一次。
    Python 的 round 为银行家舍入（0.5 -> 0），对战双方同规则，确定性不受影响。"""
    return int(round(float(x)))


def _eff_atk(c: _Combatant, game: GameCfg) -> float:
    """有效攻击：叠加全部背水类（boost）状态的乘区加成。"""
    mult = 1.0
    for _sid, s, _sdef in statuses.by_kind(c, game, "boost"):
        mult *= 1.0 + s["value"]
    return c.atk * mult


def _eff_def(c: _Combatant, game: GameCfg, tick: int = 0) -> float:
    """有效防御：依次扣减全部破甲状态的叠层总量（不低于 0）。
    v2.0.0 修复：破甲随到期刻失效（v1.x 仅快照展示到期、实际永续）。"""
    shred = 0.0
    for _sid, s, _sdef in statuses.by_kind(c, game, "shred"):
        if s["until"] > tick:
            shred += s["value"] * s["stacks"]
    return max(0.0, c.defense - shred)


def _eff_spd(c: _Combatant, game: GameCfg) -> float:
    """有效速度：叠加全部 boost 状态的速度乘区加成。"""
    mult = 1.0
    for _sid, s, _sdef in statuses.by_kind(c, game, "boost"):
        mult *= 1.0 + s["spd"]
    return c.spd * mult


def _live_value(c: _Combatant, vid: str, game: GameCfg, tick: int = 0) -> float:
    """共鸣取用的「当前值」：hp 为当前生命，atk/spd 含 boost 加成，
    def 为破甲后的有效防御。"""
    if vid == "hp":
        return float(max(0, c.hp))
    if vid == "atk":
        return _eff_atk(c, game)
    if vid == "def":
        return _eff_def(c, game, tick)
    if vid == "spd":
        return _eff_spd(c, game)
    if vid == "crit":
        return float(c.crit)
    if vid == "dodge":
        return float(c.dodge)
    return 0.0


def _hurt(c: _Combatant, amount: float, ev, rng, game: GameCfg) -> None:
    """扣除生命；若致命且任一不屈类（will）状态仍可触发，按施加顺序取
    第一个判定：成功则回复一定百分比最大生命，且该状态概率指数衰减。"""
    c.hp -= amount
    for _sid, will, _sdef in statuses.by_kind(c, game, "will"):
        if c.hp > 0 or will["chance"] <= 0:
            continue
        if rng.next_float() < will["chance"]:
            c.hp = _r(c.max_hp * will["heal"])
            will["chance"] *= will["decay"]
            will["used"] += 1
            ev("will_trigger", {"a": c.name, "heal": format_num(c.hp)})
        return


def _make_combatant(f: Fighter, pos: int, game: GameCfg) -> _Combatant:
    bc = game.battle
    skills = []
    for sdef, pgraph in personalized_effects(f, game):
        plan = effects.compile_graph(pgraph, validate=False)
        skills.append((sdef, pgraph, plan))
    c = _Combatant(
        fighter=f, pos=pos,
        name="【%s】%s" % (compose_title_name(f, game), f.name),
        plain_name=f.name,
        max_hp=float(f.attrs["hp"]), hp=float(f.attrs["hp"]),
        atk=float(f.attrs["atk"]), defense=float(f.attrs["def"]),
        spd=float(f.attrs["spd"]), dodge=float(f.attrs["dodge"]),
        crit=float(f.attrs["crit"]),
        skills=skills,
    )
    c.dodge = min(c.dodge, bc.dodge_cap)   # 闪避上限（创建时钳制）
    c.crit = min(c.crit, bc.crit_cap)      # 暴击上限（创建时钳制）
    return c


def _compute_damage(actor, enemy, mult, crit, game: GameCfg, rng,
                    pen=0.0, tick: int = 0) -> float:
    """伤害公式（v0.10.0）：防御为倒数百分比免伤。

    raw      = 有效ATK × atk_factor × 三角形浮动 × 暴击倍率 × 技能倍率
    免伤率    = 有效DEF / (有效DEF + defense_constant) × (1 − 穿透率)
    dmg      = max( min_damage, raw × (1 − 免伤率) )
    """
    bc = game.battle
    variance = rng.next_triangular(bc.variance_lo, bc.variance_hi)  # 三角浮动
    crit_mult = bc.crit_multiplier if crit else 1.0                 # 暴击倍率
    raw = _eff_atk(actor, game) * bc.atk_factor * variance * crit_mult * mult
    armor = _eff_def(enemy, game, tick)
    reduction = armor / (armor + bc.defense_constant) * (1.0 - pen)  # 免伤率
    return max(float(bc.min_damage), raw * (1.0 - reduction))


def _snapshot(combatants, threshold: float, tick: int, game: GameCfg) -> dict:
    """双方状态快照（按输入位置 a/b），状态以 id+params 存储、渲染时查
    statuses 配置文案。数值均为引擎真实值（v0.10.0 起）；血契类标记只显示
    累计转化攻击。"""
    def one(c):
        buffs = statuses.status_display(c, tick, game.battle.guard_reduction_cap,
                                        game)
        spd = _eff_spd(c, game)
        gauge_pct = max(0.0, min(100.0, c.gauge * 100.0 / threshold))
        return {
            "hp": round(max(0.0, float(c.hp)), 2),
            "max_hp": round(float(c.max_hp), 2),
            "atk": round(_eff_atk(c, game), 2),
            "def": round(_eff_def(c, game, tick), 2),
            "spd": round(spd, 2),
            "crit": round(float(c.crit), 2),
            "dodge": round(float(c.dodge), 2),
            "gauge": round(float(c.gauge), 2),
            "gauge_pct": round(gauge_pct, 2),
            "gauge_gain": round(spd, 2),
            "gauge_pct_gain": round(spd * 100.0 / threshold, 2),
            "gauge_threshold": round(float(threshold), 2),
            "buffs": buffs,
        }
    return {"a": one(combatants[0]), "b": one(combatants[1])}


# ---- 链执行器：条件判定 + 共鸣参数计算 + 效果原语分发 ----

class _Ctx:
    """一次挂点执行的上下文。owner = 技能归属方，opponent = 对手；
    ac = 攻击链累加器；dc = 防御链累加器；dmg = 命中反应时的本次伤害。"""

    __slots__ = ("game", "rng", "ev", "tick", "owner", "opponent",
                 "combatants", "ac", "dc", "dmg", "skill", "node",
                 "proc_logged", "executed", "defer", "hook_name", "crit_hit")

    def __init__(self, game, rng, ev, tick, owner, opponent, combatants):
        self.game = game              # GameCfg 配置快照
        self.rng = rng                # 对战主随机源
        self.ev = ev                  # 战报发射器
        self.tick = tick              # 当前刻
        self.owner = owner            # 技能归属方
        self.opponent = opponent      # 对手
        self.combatants = combatants  # 双方列表（净化等全体效果用）
        self.ac = None                # 攻击链累加器 {mult, pen, crit_flat, crit}
        self.dc = None                # 防御链累加器 {dmg, guard_done}
        self.dmg = 0.0                # 命中反应钩子下的本次伤害
        self.skill = None             # 当前技能 SkillDef
        self.node = None              # 当前节点 dict
        self.proc_logged = False      # 本技能本次挂点是否已宣告
        self.executed = False         # 本挂点是否有原语实际执行（打断判定用）
        self.defer = None             # 攻击链的延迟施加队列
        self.hook_name = ""           # 当前挂点名
        self.crit_hit = False         # 命中反应时的暴击标记


def _node_specs(ctx: _Ctx, node: dict):
    """节点参数规格表（apply_status 按状态定义展开）。"""
    if node["kind"] == "op" and node["type"] == "apply_status":
        sid = node.get("params", {}).get("status")
        return effects.param_specs("op", "apply_status",
                                   lambda: ctx.game.status_specs.get(sid))
    return effects.param_specs(node["kind"], node["type"])


def _res_spec(ctx: _Ctx, node: dict, param: str):
    """单参数的共鸣规格 (fmt, lo, hi)；未声明回落默认规格。"""
    ps = _node_specs(ctx, node).get(param)
    if ps is not None and (ps.fmt or ps.clamp):
        lo, hi = (ps.clamp or (None, None))
        return (ps.fmt, lo, hi)
    return effects.DEFAULT_RESONANCE_SPEC


def _proc_params(node: dict, ctx: _Ctx) -> dict:
    """按双方当前值应用该节点的全部共鸣变数，返回本次实际参数。"""
    params = node.get("params", {})
    links = node.get("links")
    if not links:
        return params
    proc = dict(params)
    owner, opp = ctx.owner, ctx.opponent
    for link in links:
        param = str(link.get("param"))
        if param not in proc:
            continue
        coeff = resonance_coeff(
            lambda vid: _live_value(owner, vid, ctx.game, ctx.tick),
            lambda vid: _live_value(opp, vid, ctx.game, ctx.tick),
            link, ctx.game)
        proc = apply_resonance(proc, coeff, param, _res_spec(ctx, node, param))
    return proc


def _emit_link_events(ctx: _Ctx, node: dict, proc: dict):
    """输出共鸣变动事件（模板 effect_link，字段词与最终值直显）。"""
    for link in node.get("links") or ():
        param = str(link.get("param"))
        if param not in proc:
            continue
        mode = str(link.get("mode", "own"))
        ctx.ev("effect_link", {
            "a": ctx.owner.name,
            "stat": {"ref": "attr", "id": link.get("variable")},
            "scope": {"ref": "stat_word", "id": "scope_" + mode},
            "field": {"ref": "stat_word", "id": "field_" + param},
            "final": format_resonance_final(float(proc[param]),
                                            _res_spec(ctx, node, param)[0]),
        })


def _cond_pass(node: dict, ctx: _Ctx, proc: dict) -> bool:
    """条件判定：失败返回 False（其下游子树整枝跳过）。"""
    t = node["type"]
    owner, opp = ctx.owner, ctx.opponent
    if t == "chance":
        return not (ctx.rng.next_float() > float(proc.get("chance", 1.0)))
    if t == "self_hp_below":
        return owner.hp < owner.max_hp * float(proc.get("threshold", 0.0))
    if t == "self_hp_above":
        return owner.hp >= owner.max_hp * float(proc.get("threshold", 0.0))
    if t == "target_hp_below":
        return opp.hp <= opp.max_hp * float(proc.get("threshold", 0.0))
    if t == "target_hp_above":
        return opp.hp > opp.max_hp * float(proc.get("threshold", 0.0))
    if t == "has_marker":
        return str(proc.get("marker")) in owner.markers
    if t == "no_marker":
        return str(proc.get("marker")) not in owner.markers
    if t == "once_per_battle":
        marker = "once:" + str(proc.get("marker"))
        if marker in owner.markers:
            return False
        owner.markers.add(marker)
        return True
    return True


def _run_tree(tree, ctx: _Ctx):
    """递归执行一棵链树；返回子树传出的信号
    （'consume' 占用行动 / 'replace' 替换攻击 / 'immune' 免疫终止防御）。"""
    node, children = tree
    ctx.node = node
    if node["kind"] == "condition":
        proc = _proc_params(node, ctx)
        if not _cond_pass(node, ctx, proc):
            return None
    elif node["kind"] == "op":
        proc = _proc_params(node, ctx)
        ctx.executed = True
        _announce(ctx, node, proc)
        sig = _OP_IMPL[node["type"]](ctx, proc)
        if sig:
            return sig
    for child in children:
        sig = _run_tree(child, ctx)
        if sig:
            return sig
    return None


def _announce(ctx: _Ctx, node: dict, proc: dict):
    """技能宣告：首次执行的需要宣告的原语输出「使用了技能」行（每技能
    每次挂点至多一条，与 v1.x 一致），随后输出共鸣事件。"""
    hook = ctx.hook_name
    logged = effects.OPS[node["type"]]["logged"]
    if node["type"] == "apply_status":
        sdef = ctx.game.statuses.get(str(proc.get("status")), {})
        logged = bool(sdef.get("logged", logged))
    if logged and hook in _PROC_HOOKS and not ctx.proc_logged:
        ctx.proc_logged = True
        ctx.ev("skill_proc", {"a": ctx.owner.name,
                              "skill": {"ref": "skill", "id": ctx.skill.id}})
    _emit_link_events(ctx, node, proc)


def _run_hook(skills, hook: str, ctx: _Ctx):
    """执行某挂点的全部技能链（技能按派生顺序、链按触发节点数组顺序）。
    返回子树传出的信号。"""
    ctx.hook_name = hook
    for sdef, _pgraph, plan in skills:
        trees = plan.get(hook)
        if not trees:
            continue
        ctx.skill = sdef
        ctx.proc_logged = False
        for tree in trees:
            sig = _run_tree(tree, ctx)
            if sig:
                return sig
    return None


# ---- 状态施加行为（按行为种类注册；事件模板 id 来自状态定义 event 字段） ----

def _apply_dot(ctx, sid, sdef, proc, target):
    """dot：timing=every_tick 每刻结算伤害（毒）；timing=on_owner_action
    拥有者行动时结算（流血）。power=atk 时伤害按施加者攻击系数折算。"""
    power = str(sdef.get("power", ""))     # 伤害来源："" 固定值 / "atk" 攻击系数
    if power == "atk":
        key = "value"
        amount = _eff_atk(ctx.owner, ctx.game) * ctx.game.battle.atk_factor \
            * float(proc.get("value", 0.0))
    else:
        key = "damage"
        amount = float(proc.get("damage", 0.0))
    ticks = max(1, int(proc.get("ticks", 1)))   # 持续刻数
    if target.hp > 0 and float(proc.get(key, 0.0)) > 0:
        s = statuses.ensure(target, sid)
        s["damage"] = _r(amount)
        s["until"] = ctx.tick + ticks
        ctx.ev(str(sdef.get("event", "")), {"b": target.name,
                                            "damage": format_num(s["damage"]),
                                            "turns": ticks})


def _apply_control(ctx, sid, sdef, proc, target):
    """control：眩晕——拥有者在持续期间行动被消耗。"""
    if target.hp > 0:
        s = statuses.ensure(target, sid)
        s["until"] = ctx.tick + max(1, int(proc.get("ticks", 1)))
        ctx.ev(str(sdef.get("event", "")), {"b": target.name})


def _apply_shred(ctx, sid, sdef, proc, target):
    """shred：破甲叠层（每层取历史最大值，至多 max_stacks 层）。"""
    if target.hp > 0:
        s = statuses.ensure(target, sid)
        max_stacks = max(1, int(proc.get("max_stacks", 1)))  # 层数上限
        if s["stacks"] < max_stacks:
            s["stacks"] += 1
        s["max_stacks"] = max_stacks
        s["value"] = max(s["value"], _r(float(proc.get("value", 0.0))))
        s["until"] = ctx.tick + max(1, int(proc.get("ticks", 1)))
        ctx.ev(str(sdef.get("event", "")),
               {"b": target.name,
                "value": format_num(s["value"] * s["stacks"]),
                "def": format_num(_eff_def(target, ctx.game, ctx.tick))})


def _apply_guard(ctx, sid, sdef, proc, target):
    """guard：锻痕叠层（每次受击至多叠一层，层随刻到期）。"""
    if ctx.dc is None or ctx.dc["guard_done"]:
        return
    ctx.dc["guard_done"] = True   # 一次受击只叠一层（与 v1.x 一致）
    s = statuses.ensure(target, sid)
    s["value"] = float(proc.get("value", 0.0))
    s["layers"].append(ctx.tick + max(1, int(proc.get("ticks", 1))))
    n = sum(1 for t in s["layers"] if t > ctx.tick)
    ctx.ev(str(sdef.get("event", "")),
           {"b": target.name, "stacks": n,
            "value": format_pct(min(ctx.game.battle.guard_reduction_cap,
                                    s["value"] * n))})


def _apply_momentum(ctx, sid, sdef, proc, target):
    """momentum：乘胜计数（命中叠层、攻击落空清零，至多 cap 层）。"""
    s = statuses.ensure(target, sid)
    s["value"] = float(proc.get("value", 0.0))
    s["cap"] = int(proc.get("cap", 0))
    if s["stacks"] < s["cap"]:
        s["stacks"] += 1
        ctx.ev(str(sdef.get("event", "")),
               {"a": target.name, "stacks": s["stacks"],
                "mult": format_pct(1.0 + s["value"] * s["stacks"])})


def _apply_grudge(ctx, sid, sdef, proc, target):
    """grudge：怨念层（被命中积攒，拥有者攻击按层增伤，层随刻到期）。"""
    s = statuses.ensure(target, sid)
    s["value"] = float(proc.get("value", 0.0))
    s["layers"].append(ctx.tick + max(1, int(proc.get("ticks", 1))))
    n = sum(1 for t in s["layers"] if t > ctx.tick)
    ctx.ev(str(sdef.get("event", "")),
           {"a": target.name, "stacks": n,
            "mult": format_pct(1.0 + s["value"] * n)})


def _apply_lifesteal(ctx, sid, sdef, proc, target):
    """lifesteal：嗜血（按行动数衰减的吸血比例）。"""
    s = statuses.ensure(target, sid)
    s["value"] = float(proc.get("value", 0.0))
    s["turns"] = max(1, int(proc.get("turns", 1)))
    ctx.ev(str(sdef.get("event", "")),
           {"a": target.name, "value": format_pct(s["value"]),
            "turns": s["turns"]})


def _apply_regen(ctx, sid, sdef, proc, target):
    """regen：回春印记（每 interval 刻回复 value，持续 duration 刻）。"""
    s = statuses.ensure(target, sid)
    s["value"] = _r(float(proc.get("value", 0.0)))
    s["interval"] = max(1, int(proc.get("tick", 1)))   # 回复间隔（刻）
    s["next"] = ctx.tick + s["interval"]               # 下次回复刻
    s["until"] = ctx.tick + max(1, int(proc.get("duration", 1)))  # 到期刻
    ctx.ev(str(sdef.get("event", "")),
           {"a": target.name, "value": format_num(s["value"]),
            "tick": s["interval"], "turns": s["until"] - ctx.tick})


# 施加行为注册表：行为种类 -> 施加函数（与 statuses.APPLY_KINDS 一一对应）
_STATUS_APPLY = {
    "dot": _apply_dot,
    "control": _apply_control,
    "shred": _apply_shred,
    "guard": _apply_guard,
    "momentum": _apply_momentum,
    "grudge": _apply_grudge,
    "lifesteal": _apply_lifesteal,
    "regen": _apply_regen,
}

assert set(_STATUS_APPLY) == set(statuses.APPLY_KINDS), "施加行为注册表与可施加种类不一致"


# ---- 效果原语实现（与 effects.OPS 一一对应） ----

def _op_prepare_charge(ctx, proc):
    """蓄力：本次行动用于蓄力，下次行动释放（必定命中 + 暴击加成）；
    触发时的参数与共鸣存入 charge 状态，释放时按当前值重算。"""
    sid = str(proc.get("status"))
    st = statuses.ensure(ctx.owner, sid)
    st["params"] = dict(ctx.node.get("params", {}))   # 存原始参数（释放时重算共鸣）
    st["links"] = list(ctx.node.get("links") or [])
    ctx.ev("charge_start", {"a": ctx.owner.name,
                            "mult": format_pct(float(proc.get("value", 3.0)))})
    return "consume"


def _op_thunder_strike(ctx, proc):
    """雷罚：连续真实伤害替换本次攻击（首道必中，后续按 chain×decay^i 衰减；
    真实伤害无视防御与暴击，但吃防守减免并触发受击反应）。"""
    game, rng, ev = ctx.game, ctx.rng, ctx.ev
    bc = game.battle
    owner, opp = ctx.owner, ctx.opponent
    value = float(proc.get("value", 0.3))              # 每道伤害占攻击的比例
    decay = float(proc.get("decay", 0.9))              # 后续每道的概率衰减
    chain = float(proc.get("chain", 0.8))              # 首道之后的续链概率
    max_hits = max(1, int(proc.get("max_hits", 1)))    # 至多道数
    ev("thunder_cast", {"a": owner.name, "value": format_pct(value), "max": max_hits})
    for i in range(1, max_hits + 1):
        if i > 1 and rng.next_float() >= chain * (decay ** (i - 1)):
            break
        raw = max(float(bc.min_damage),
                  _eff_atk(owner, game) * bc.atk_factor * value
                  * rng.next_triangular(bc.variance_lo, bc.variance_hi))
        dmg = _defend(opp, owner, raw, game, rng, ev, ctx.tick)
        dmg = _r(dmg)
        if dmg > 0:
            _hurt(opp, dmg, ev, rng, game)
            owner.damage_dealt += dmg
            ev("thunder_hit", {"a": owner.name, "b": opp.name,
                               "damage": format_num(dmg), "hit": i})
            _hit_reactions(owner, opp, dmg, False, game, rng, ev, ctx.tick)
        if opp.hp <= 0 or owner.hp <= 0:
            break
    return "replace"


def _op_attack_mult(ctx, proc):
    """倍率修正（斩杀 / 燃血等）；announce=false 时不单独宣告。"""
    ctx.ac["mult"] *= float(proc.get("value", 1.0))
    if proc.get("announce", True):
        ctx.ev("effect_execution", {"mult": format_pct(ctx.ac["mult"])})
    return None


def _op_random_mult(ctx, proc):
    """豪赌：独立 win 概率决定提升或降低（v2.0.0 修复 v1.x 触发率双用）。"""
    if ctx.rng.next_float() < float(proc.get("win", 0.5)):
        boost = float(proc.get("value", 1.0))
        ctx.ac["mult"] *= boost
        ctx.ev("gamble_win", {"mult": format_pct(boost)})
    else:
        drop = float(proc.get("penalty", 1.0))
        ctx.ac["mult"] *= drop
        ctx.ev("gamble_lose", {"mult": format_pct(drop)})
    return None


def _op_momentum_mult(ctx, proc):
    """乘胜消耗：按当前连击层数提升本次伤害（每层 value，至多 cap 层）。"""
    value = float(proc.get("value", 0.0))
    cap = int(proc.get("cap", 0))
    for _sid, s, _sdef in statuses.by_kind(ctx.owner, ctx.game, "momentum"):
        if s["stacks"] > 0:
            ctx.ac["mult"] *= 1.0 + value * min(s["stacks"], cap)
    return None


def _op_armor_pen_flat(ctx, proc):
    """重击穿透：本次攻击按比例抵消灭伤率。"""
    ctx.ac["pen"] = max(ctx.ac["pen"], float(proc.get("value", 0.0)))
    return None


def _op_armor_pen_full(ctx, proc):
    """洞悉：本次攻击无视全部防御，并提升暴击率（分数口径，0.06 = +6%）。"""
    ctx.ac["pen"] = 1.0
    ctx.ac["crit_flat"] += float(proc.get("crit", 0.0))
    return None


def _op_self_cost(ctx, proc):
    """燃血：消耗自身最大生命的一部分（不会自灭，保底 1 点）。"""
    cost = _r(ctx.owner.max_hp * float(proc.get("cost", 0.0)))
    ctx.owner.hp = max(1.0, ctx.owner.hp - cost)
    ctx.ev("overload_cost", {"a": ctx.owner.name, "cost": format_num(cost),
                             "mult": format_pct(ctx.ac["mult"])})
    return None


def _op_apply_status(ctx, proc):
    """施加状态：行为按状态定义的 kind 分发（_STATUS_APPLY）。攻击链上对
    敌方施加的状态延迟到命中后生效（与 v1.x 一致）。"""
    sid = str(proc.get("status"))
    target = ctx.opponent if str(proc.get("target")) == "enemy" else ctx.owner

    if ctx.hook_name == "on_attack" and target is ctx.opponent:
        # 延迟施加：命中结算后、目标仍存活才生效
        snapshot_proc = dict(proc)
        ctx.defer.append(lambda: _apply_status_now(
            ctx, sid, snapshot_proc, target))
        return None
    _apply_status_now(ctx, sid, proc, target)
    return None


def _apply_status_now(ctx, sid, proc, target):
    """立即施加状态（查注册表分发；未知种类静默跳过——配置层已校验）。"""
    sdef = ctx.game.statuses.get(sid, {})
    kind = str(sdef.get("kind", ""))
    handler = _STATUS_APPLY.get(kind)
    if handler is not None:
        handler(ctx, sid, sdef, proc, target)


def _op_heal(ctx, proc):
    """即时回复（不溢出上限）。"""
    gained = _r(min(float(proc.get("value", 0.0)),
                    ctx.owner.max_hp - ctx.owner.hp))
    if gained > 0:
        ctx.owner.hp += gained
        ctx.ev("effect_heal", {"a": ctx.owner.name, "heal": format_num(gained)})
    return None


def _op_cleanse(ctx, proc):
    """驱散双方可驱散状态，并按驱散种数回复生命。"""
    count = statuses.dispel_all(ctx.combatants, ctx.tick, ctx.game)
    healed = _r(min(float(proc.get("value", 0.0))
                    + float(proc.get("per", 0.0)) * count,
                    ctx.owner.max_hp - ctx.owner.hp))
    if healed > 0:
        ctx.owner.hp += healed
    ctx.ev("purify_cleanse", {"a": ctx.owner.name, "count": count,
                              "heal": format_num(healed)})
    return None


def _op_gauge_add(ctx, proc):
    """疾影：命中结算后行动槽前进（暴击取 crit_value）——延迟到命中后生效。"""
    snapshot_proc = dict(proc)
    ctx.defer.append(lambda: _gauge_add_now(ctx, snapshot_proc))
    return None


def _gauge_add_now(ctx, proc):
    owner, opp = ctx.owner, ctx.opponent
    if opp.hp > 0 and owner.hp > 0:
        key = "crit_value" if ctx.ac and ctx.ac.get("crit") else "value"
        owner.gauge += _r(float(proc.get(key, 0.0)))


def _op_gauge_delay(ctx, proc):
    """斩断退条：使对方行动槽倒退（打断其行动）。"""
    ctx.opponent.gauge = max(0.0, ctx.opponent.gauge
                             - _r(float(proc.get("delay", 0.0))))
    return None


def _op_quick_strike(ctx, proc):
    """斩断抢攻：一次可闪避可暴击的小倍率打击（不吃攻击技能链）。"""
    ctx.ev("sever_proc", {"a": ctx.owner.name, "b": ctx.opponent.name})
    _quick_strike(ctx.owner, ctx.opponent, float(proc.get("value", 0.5)),
                  ctx.game, ctx.rng, ctx.ev, ctx.tick)
    return None


def _op_stat_gain(ctx, proc):
    """永久成长（渐入佳境类）：速度 / 攻击永久增加，层数与累计记入状态。"""
    owner = ctx.owner
    sid = str(proc.get("status"))
    gain = _r(float(proc.get("value", 0.0)))       # 本次速度增量
    gain_atk = _r(float(proc.get("atk", 0.0)))     # 本次攻击增量
    owner.spd += gain
    owner.atk += gain_atk
    s = statuses.ensure(owner, sid)
    s["stacks"] += 1
    s["spd_total"] += gain
    s["atk_total"] += gain_atk
    ctx.ev("tempo_stack", {"a": owner.name, "stacks": s["stacks"],
                           "spd": format_num(owner.spd),
                           "atk": format_num(owner.atk)})
    return None


def _op_stat_boost_once(ctx, proc):
    """一次性爆发（背水一战类）：生命过低时永久攻速乘区加成，
    每条状态每场一次。"""
    sid = str(proc.get("status"))
    marker = "boost:" + sid
    if marker in ctx.owner.markers:
        return None
    ctx.owner.markers.add(marker)
    s = statuses.ensure(ctx.owner, sid)
    s["value"] = float(proc.get("value", 0.5))
    s["spd"] = float(proc.get("spd", 0.0))
    ctx.ev("low_hp_trigger", {"a": ctx.owner.name,
                              "value": format_pct(s["value"]),
                              "spd": format_pct(s["spd"])})
    return None


def _op_will_register(ctx, proc):
    """注册不屈类状态：致命伤害按衰减概率重生。"""
    s = statuses.ensure(ctx.owner, str(proc.get("status")))
    s["chance"] = float(proc.get("chance", 0.0))
    s["heal"] = float(proc.get("value", 0.0))
    s["decay"] = float(proc.get("decay", 0.0))
    return None


def _op_pact_cost(ctx, proc):
    """血契献祭：行动开始扣血（不会自灭），本次攻击附带吸血；
    转化比例存入状态，供 pact_convert 在行动后结算。"""
    owner = ctx.owner
    s = statuses.ensure(owner, str(proc.get("status")))
    cost = _r(owner.max_hp * float(proc.get("cost", 0.0)))
    owner.hp = max(1.0, owner.hp - cost)
    s["active"] = True
    s["steal"] = float(proc.get("value", 0.0))
    s["convert"] = float(proc.get("convert", 0.0))
    s["stolen"] = 0.0
    ctx.ev("pact_proc", {"a": owner.name, "cost": format_num(cost),
                         "value": format_pct(s["steal"])})
    return None


def _op_pact_convert(ctx, proc):
    """血契转化：行动结束后把本次吸血按存入的转化比例永久化为攻击。"""
    owner = ctx.owner
    s = owner.st.get(str(proc.get("status")))
    if not s or not s["active"]:
        return None
    s["active"] = False
    gain = _r(s["stolen"] * s["convert"])
    if gain > 0:
        owner.atk += gain
        s["total_gain"] += gain
        ctx.ev("pact_gain", {"a": owner.name,
                             "value": format_num(gain),
                             "atk": format_num(owner.atk)})
    return None


def _op_record_damage(ctx, proc):
    """记仇：记录本次所受伤害（至多 cap 条），拥有者下次命中追加打出。"""
    s = statuses.ensure(ctx.owner, str(proc.get("status")))
    if len(s["records"]) >= int(proc.get("cap", 0)):
        return None
    s["ratio"] = float(proc.get("ratio", 1.0))
    s["cap"] = int(proc.get("cap", 0))
    s["records"].append(float(ctx.dmg))
    ctx.ev("retribution_record", {"a": ctx.owner.name,
                                  "damage": format_num(ctx.dmg),
                                  "stacks": len(s["records"])})
    return None


def _op_reflect_damage(ctx, proc):
    """荆棘反甲：免除一部分伤害并按倍率反弹（反弹可触发不屈）。"""
    bc = ctx.game.battle
    defender, attacker = ctx.owner, ctx.opponent
    if ctx.dc["dmg"] <= 0:
        return None
    split = min(bc.reflect_split_cap, float(proc.get("value", 0.0)))
    avoided = ctx.dc["dmg"] * split                          # 被免除的部分
    reflected = _r(avoided * float(proc.get("ratio", 1.0)))  # 反弹伤害
    ctx.dc["dmg"] -= avoided
    defender.damage_dealt += reflected
    _hurt(attacker, reflected, ctx.ev, ctx.rng, ctx.game)
    ctx.ev("effect_reflect", {"a": attacker.name, "b": defender.name,
                              "damage": format_num(reflected)})
    return None


def _op_damage_reduce(ctx, proc):
    """减伤：按比例降低本次所受伤害（下限 min_damage）。"""
    bc = ctx.game.battle
    ratio = float(proc.get("value", 0.0))
    if ctx.dc["dmg"] > 0 and ratio > 0:
        ctx.dc["dmg"] = max(float(bc.min_damage), ctx.dc["dmg"] * (1.0 - ratio))
        ctx.ev("effect_bulwark", {"b": ctx.owner.name, "ratio": format_pct(ratio)})
    return None


def _op_immune_chance(ctx, proc):
    """免疫：概率完全免除本次伤害（终止防御链，不触发后续防御效果）。"""
    if ctx.dc["dmg"] > 0 and ctx.rng.next_float() < float(proc.get("immune", 0.0)):
        ctx.ev("immune", {"b": ctx.owner.name})
        return "immune"
    return None


_OP_IMPL = {
    "prepare_charge": _op_prepare_charge,
    "thunder_strike": _op_thunder_strike,
    "attack_mult": _op_attack_mult,
    "random_mult": _op_random_mult,
    "momentum_mult": _op_momentum_mult,
    "armor_pen_flat": _op_armor_pen_flat,
    "armor_pen_full": _op_armor_pen_full,
    "self_cost": _op_self_cost,
    "apply_status": _op_apply_status,
    "heal": _op_heal,
    "cleanse": _op_cleanse,
    "gauge_add": _op_gauge_add,
    "gauge_delay": _op_gauge_delay,
    "quick_strike": _op_quick_strike,
    "stat_gain": _op_stat_gain,
    "stat_boost_once": _op_stat_boost_once,
    "will_register": _op_will_register,
    "pact_cost": _op_pact_cost,
    "pact_convert": _op_pact_convert,
    "record_damage": _op_record_damage,
    "reflect_damage": _op_reflect_damage,
    "damage_reduce": _op_damage_reduce,
    "immune_chance": _op_immune_chance,
}

assert set(_OP_IMPL) == set(effects.OPS), "效果原语实现与注册表不一致"


# ---- 结算流程 ----

def _defend(defender, attacker, dmg, game, rng, ev, tick):
    """防守方结算：锻痕叠层减伤 -> 防御钩子链（免疫终止 / 减伤 / 反甲 /
    锻痕叠层）。返回最终伤害（反弹伤害直接作用于攻击方）。"""
    bc = game.battle
    for _sid, s, _sdef in statuses.by_kind(defender, game, "guard"):
        n = sum(1 for t in s["layers"] if t > tick)   # 在场层数
        if n > 0 and dmg > 0:
            ratio = min(bc.guard_reduction_cap, s["value"] * n)
            dmg = max(float(bc.min_damage), dmg * (1.0 - ratio))
            ev("effect_reduction", {"b": defender.name, "ratio": format_pct(ratio)})
    dc = {"dmg": dmg, "guard_done": False}   # 防御链累加器
    ctx = _Ctx(game, rng, ev, tick, defender, attacker, [defender, attacker])
    ctx.dc = dc
    if _run_hook(defender.skills, "on_defend", ctx) == "immune":
        return 0.0
    return dc["dmg"]


def _apply_lifesteal(actor, dmg, ev, game):
    """命中后的吸血结算：全部嗜血状态 + 生效中的血契共享同一口吸血。"""
    steal = 0.0
    for _sid, s, _sdef in statuses.by_kind(actor, game, "lifesteal"):
        if s["turns"] > 0:
            steal += s["value"]
    pacts = []                                # 本次参与的血契（记录吸血量）
    for _sid, s, _sdef in statuses.by_kind(actor, game, "pact"):
        if s["active"]:
            steal += s["steal"]
            pacts.append(s)
    if steal <= 0 or dmg <= 0 or actor.hp <= 0:
        return
    gained = _r(min(dmg * steal, actor.max_hp - actor.hp))
    if gained > 0:
        actor.hp += gained
        for s in pacts:
            s["stolen"] += gained
        ev("effect_lifesteal", {"a": actor.name, "heal": format_num(gained)})


def _quick_strike(attacker, victim, mult, game, rng, ev, tick):
    """斩断反击：一次小倍率的普通打击（可闪避可暴击，不吃攻击技能链）。"""
    if rng.next_float() < victim.dodge / 100.0:
        ev("attack_miss", {"a": attacker.name, "b": victim.name})
        return
    crit = rng.next_float() < attacker.crit / 100.0
    if crit:
        ev("attack_crit", {})
    dmg = _compute_damage(attacker, victim, mult, crit, game, rng, tick=tick)
    dmg = _defend(victim, attacker, dmg, game, rng, ev, tick)
    dmg = _r(dmg)
    if dmg > 0:
        _hurt(victim, dmg, ev, rng, game)
        attacker.damage_dealt += dmg
        ev("attack_hit", {"a": attacker.name, "b": victim.name,
                          "damage": format_num(dmg)})
        _apply_lifesteal(attacker, dmg, ev, game)
        _hit_reactions(attacker, victim, dmg, crit, game, rng, ev, tick)


def _hit_reactions(actor, enemy, dmg, crit, game, rng, ev, tick):
    """一次命中后的反应结算：攻击方的命中钩子（乘胜叠层）与
    受击方的被命中钩子（怨念积攒 / 记仇记录）。"""
    if dmg <= 0 or enemy.hp <= 0:
        return
    ctx = _Ctx(game, rng, ev, tick, actor, enemy, [actor, enemy])
    ctx.dmg = dmg
    ctx.crit_hit = crit
    _run_hook(actor.skills, "on_hit_landed", ctx)
    ctx2 = _Ctx(game, rng, ev, tick, enemy, actor, [actor, enemy])
    ctx2.dmg = dmg
    ctx2.crit_hit = crit
    _run_hook(enemy.skills, "on_hit_taken", ctx2)


def _first_of_kind(c, game, kind):
    """取某种类第一条在场状态（蓄力等单例语义用）。"""
    items = statuses.by_kind(c, game, kind)
    return items[0] if items else None


def _attack(actor, enemy, game: GameCfg, rng, ev, tick: int):
    bc = game.battle

    # ---- 普通攻击宣告：让普攻在战报中同样可见（v1.0.0） ----
    ev("attack_start", {"a": actor.name})

    # ---- 蓄力释放：必定命中、暴击率提升的巨大一击（替换常规攻击） ----
    charging = _first_of_kind(actor, game, "charge")
    if charging is not None:
        sid, st, _sdef = charging
        del actor.st[sid]
        ctx = _Ctx(game, rng, ev, tick, actor, enemy, [actor, enemy])
        fake_node = {"kind": "op", "type": "prepare_charge",
                     "params": st["params"], "links": st["links"]}
        proc = _proc_params(fake_node, ctx)
        mult = float(proc.get("value", 3.0))
        crit_bonus = float(proc.get("crit", 0.0))
        ev("charge_release", {"a": actor.name, "mult": format_pct(mult),
                              "crit": format_pct(crit_bonus)})
        crit = rng.next_float() < min(bc.crit_cap / 100.0,
                                      actor.crit / 100.0 + crit_bonus)
        if crit:
            ev("attack_crit", {})
        dmg = _compute_damage(actor, enemy, mult, crit, game, rng, tick=tick)
        dmg = _defend(enemy, actor, dmg, game, rng, ev, tick)
        dmg = _r(dmg)
        if dmg > 0:
            _hurt(enemy, dmg, ev, rng, game)
            actor.damage_dealt += dmg
            ev("attack_hit", {"a": actor.name, "b": enemy.name,
                              "damage": format_num(dmg)})
            _apply_lifesteal(actor, dmg, ev, game)
            _hit_reactions(actor, enemy, dmg, crit, game, rng, ev, tick)
        return

    # ---- 攻击钩子链（技能按派生顺序） ----
    ac = {"mult": 1.0, "pen": 0.0, "crit_flat": 0.0, "crit": False}
    ctx = _Ctx(game, rng, ev, tick, actor, enemy, [actor, enemy])
    ctx.ac = ac
    ctx.defer = []
    sig = _run_hook(actor.skills, "on_attack", ctx)
    if sig in ("consume", "replace"):
        return  # 蓄力占用本次行动 / 雷罚已替换攻击

    # 怨念：挨打积累的层数转化为本次攻击伤害加成（每条状态独立乘区）
    for _sid, s, _sdef in statuses.by_kind(actor, game, "grudge"):
        n = sum(1 for t in s["layers"] if t > tick)
        if n > 0 and s["value"] > 0:
            ac["mult"] *= 1.0 + s["value"] * n

    # ---- 闪避判定（落空时乘胜清零） ----
    if rng.next_float() < enemy.dodge / 100.0:
        ev("attack_miss", {"a": actor.name, "b": enemy.name})
        for _sid, s, _sdef in statuses.by_kind(actor, game, "momentum"):
            s["stacks"] = 0
        return

    crit = rng.next_float() < min(bc.crit_cap / 100.0,
                                  actor.crit / 100.0 + ac["crit_flat"])
    ac["crit"] = crit
    if crit:
        ev("attack_crit", {})
    dmg = _compute_damage(actor, enemy, ac["mult"], crit, game, rng,
                          pen=ac["pen"], tick=tick)
    dmg = _defend(enemy, actor, dmg, game, rng, ev, tick)

    # 记仇：下次命中把各条记录的伤害按倍率追加打出（分量独立取整）
    for _sid, ret, _sdef in statuses.by_kind(actor, game, "record"):
        if not ret["records"]:
            continue
        bonus = _r(sum(ret["records"]) * ret["ratio"])
        ret["records"] = []
        if bonus > 0:
            dmg += bonus
            ev("retribution_release", {"a": actor.name,
                                       "value": format_num(bonus),
                                       "ratio": format_pct(ret["ratio"])})

    dmg = _r(dmg)
    if dmg > 0:
        _hurt(enemy, dmg, ev, rng, game)
        actor.damage_dealt += dmg
    ev("attack_hit", {"a": actor.name, "b": enemy.name,
                      "damage": format_num(dmg)})
    _apply_lifesteal(actor, dmg, ev, game)
    _hit_reactions(actor, enemy, dmg, crit, game, rng, ev, tick)

    # ---- 延迟施加（命中结算后：敌方需仍存活） ----
    for fn in ctx.defer:
        fn()


def run_battle(fighter_a: Fighter, fighter_b: Fighter, game: GameCfg,
               snapshots: bool = True, record: bool = True) -> BattleOutcome:
    """运行一场对战。snapshots=False 时不为战报条目附带状态快照（极速模式，
    供 /api/battle/fast 使用）；record=False 时不记录任何战报条目（真战力
    批量模拟使用，随机数消耗、胜负与事件外的全部结算完全一致）。"""
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
    last_logged_tick = 0    # 上次输出刻标记事件的刻（刻去重用）

    def ev(template, params=None):
        nonlocal last_logged_tick
        if not record:
            return
        state = _snapshot(combatants, bc.gauge_threshold, tick, game) if snapshots else None
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
    ev("battle_start", {"a": first.name, "b": second.name})

    # ---- 战斗开始钩子（不屈意志注册） ----
    boot = _Ctx(game, rng, ev, 0, first, second, combatants)
    _run_hook(first.skills, "battle_start", boot)
    boot2 = _Ctx(game, rng, ev, 0, second, first, combatants)
    _run_hook(second.skills, "battle_start", boot2)

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
            for _sid, regen, _sdef in statuses.by_kind(c, game, "regen"):
                if regen["until"] > tick - 1 and tick >= regen["next"]:
                    gained = min(regen["value"], c.max_hp - c.hp)
                    if gained > 0:
                        c.hp += gained
                        ev("regen_tick", {"a": c.name, "heal": format_num(gained)})
                    regen["next"] += max(1, regen["interval"])
            for kind in ("grudge", "guard"):
                for _sid, s, _sdef in statuses.by_kind(c, game, kind):
                    s["layers"] = [t for t in s["layers"] if t > tick - 1]
        # ---- 每刻毒发（timing=every_tick 的 dot：每刻结算伤害） ----
        for c in internal:
            if c.hp <= 0:
                continue
            for _sid, s, sdef in statuses.by_kind(c, game, "dot"):
                if sdef.get("timing") != "every_tick" or s["until"] <= tick:
                    continue
                _hurt(c, s["damage"], ev, rng, game)
                ev("poison_tick", {"a": c.name, "damage": format_num(s["damage"])})
                if c.hp <= 0:
                    ev("poison_death", {"a": c.name})
                    winner = internal[1] if c is internal[0] else internal[0]
                    break
            if winner is not None:
                break
        if winner is not None:
            break
        # ---- 行动槽推进 ----
        for c in combatants:
            if c.hp > 0:
                c.gauge += _eff_spd(c, game)
        ready = [c for c in internal if c.hp > 0 and c.gauge >= bc.gauge_threshold]
        ready.sort(key=lambda c: (-c.gauge, c.seq))
        for actor in ready:
            enemy = internal[1] if actor is internal[0] else internal[0]
            actor.gauge -= bc.gauge_threshold
            if actor.hp <= 0 or enemy.hp <= 0:
                break
            # ---- 打断钩子：敌方即将行动时（斩断退条 + 抢攻，消耗其行动） ----
            ictx = _Ctx(game, rng, ev, tick, enemy, actor, combatants)
            _run_hook(enemy.skills, "action_interrupt", ictx)
            if ictx.executed:
                if _settle_deaths(enemy, actor):
                    break
                continue
            # ---- 流血（timing=on_owner_action 的 dot：拥有者行动时结算） ----
            bled_out = False
            for _sid, bleed, sdef in statuses.by_kind(actor, game, "dot"):
                if sdef.get("timing") != "on_owner_action":
                    continue
                if bleed["until"] > tick:
                    _hurt(actor, bleed["damage"], ev, rng, game)
                    ev("bleed_tick", {"a": actor.name,
                                      "damage": format_num(bleed["damage"])})
                    if actor.hp <= 0:
                        ev("bleed_death", {"a": actor.name})
                        winner = enemy
                        bled_out = True
                        break
            if bled_out or winner is not None:
                break
            # ---- 行动开始钩子（血契 / 回春 / 净化） ----
            sctx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
            sctx.defer = []
            _run_hook(actor.skills, "action_start", sctx)
            for fn in sctx.defer:
                fn()
            if winner is not None:
                break
            # ---- 眩晕：消耗本次行动 ----
            stunned = any(s["until"] > tick
                          for _sid, s, _sdef in statuses.by_kind(actor, game, "control"))
            if stunned:
                ev("turn_stun", {"a": actor.name})
                continue
            # ---- 攻击前钩子（背水一战等一次性判定） ----
            bctx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
            _run_hook(actor.skills, "before_attack", bctx)
            _attack(actor, enemy, game, rng, ev, tick)
            if _settle_deaths(actor, enemy):
                break
            # ---- 行动后：成长钩子 / 嗜血递减 ----
            if actor.hp > 0:
                actx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
                _run_hook(actor.skills, "after_action", actx)
            for _sid, ls, _sdef in statuses.by_kind(actor, game, "lifesteal"):
                if ls["turns"] > 0:
                    ls["turns"] -= 1
                    if ls["turns"] <= 0:
                        ls["value"] = 0.0

    if winner is None and not draw:
        ev("timeout", {})
        ratio_a = combatants[0].hp / combatants[0].max_hp   # 剩余生命比例（甲）
        ratio_b = combatants[1].hp / combatants[1].max_hp   # 剩余生命比例（乙）
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
        winner_name=None if draw else winner.plain_name,
        draw=draw,
        ticks=tick,
        damage={0: combatants[0].damage_dealt, 1: combatants[1].damage_dealt},
        seed=seed_hex,
        events=events,
    )


def render_events(events, game) -> list:
    """把结构化事件渲染为文本列表。"""
    return [render_template(game.battle_log.get(e["template"], e["template"]),
                            e.get("params"), game) for e in events]


def _rich_segments(template, params, game: GameCfg, side_of_name: dict) -> list:
    """把模板渲染为富文本段列表（v0.10.0）：[{t: 文本, k: 种类, id: 技能id}]。

    种类：plain / name-a / name-b（阵营名，前端红/蓝加粗）/ skill（技能名，
    前端按技能 id 配色加粗）/ dmg（伤害/生命消耗，红）/ heal（治疗，绿）。
    各段拼接后与纯文本渲染结果完全一致（有测试保障）。"""
    out = []
    text = str(template or "")
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.start() > pos:
            out.append({"t": text[pos:m.start()], "k": "plain"})
        key = m.group(1)
        value = (params or {}).get(key)
        if isinstance(value, dict) and "ref" in value and "id" in value:
            name = game.ref_name(value["ref"], value["id"]) or str(value["id"])
            if value["ref"] == "skill":
                out.append({"t": name, "k": "skill", "id": str(value["id"])})
            else:
                out.append({"t": name, "k": "plain"})
        elif key in _RICH_NAME_KEYS and isinstance(value, str) and value:
            side = side_of_name.get(value)
            out.append({"t": value, "k": "name-" + side if side else "plain"})
        elif key in _RICH_DAMAGE_KEYS:
            out.append({"t": str(value), "k": "dmg"})
        elif key in _RICH_HEAL_KEYS:
            out.append({"t": str(value), "k": "heal"})
        else:
            out.append({"t": str(value), "k": "plain"})
        pos = m.end()
    if pos < len(text):
        out.append({"t": text[pos:], "k": "plain"})
    return out


def _render_state(state, game) -> dict:
    """把快照中的状态（id+params）渲染为带名称/说明的条目。"""
    out = {}
    for side, snap in (state or {}).items():
        buffs = []
        for b in snap.get("buffs", []):
            entry = game.statuses.get(b["id"], {})
            buffs.append({
                "id": b["id"],
                "name": entry.get("name", b["id"]),
                "detail": render_template(entry.get("detail", ""), b.get("params"), game),
                "desc": entry.get("desc", ""),
            })
        out[side] = dict(snap, buffs=buffs)
    return out


def battle_to_api(outcome: BattleOutcome, fighters_api: list,
                  game: GameCfg) -> dict:
    """战报的对外表示：战报文本 + 富文本段 + 渲染后的快照 + 结果汇总。
    v0.10.0 起全部数值为引擎真实值；每条战报附带 rich 段供前端着色。"""
    side_of_name = {}   # 战报显示名 -> 阵营 a/b（前端头顶血条取侧向用）
    for i, f in enumerate(fighters_api or []):
        # 战报中的角色名为「【称号】名字」（v1.2.1），与 _Combatant.name 口径一致
        title = str((f.get("title") or {}).get("name") or "")
        key = ("【%s】%s" % (title, f.get("name", ""))) if title else str(f.get("name", ""))
        side_of_name[key] = "a" if i == 0 else "b"
    texts = render_events(outcome.events, game)
    log = []
    for e, text in zip(outcome.events, texts):
        entry = dict(e)
        entry["text"] = text
        entry["rich"] = _rich_segments(
            game.battle_log.get(e["template"], e["template"]),
            e.get("params"), game, side_of_name)
        if "state" in entry:
            entry["state"] = _render_state(entry["state"], game)
        log.append(entry)
    return {
        "fighters": fighters_api,
        "result": {
            "winner": outcome.winner_name,
            "winner_pos": outcome.winner_pos,
            "draw": outcome.draw,
            "ticks": outcome.ticks,
            "damage": {"a": round(outcome.damage[0], 2),
                       "b": round(outcome.damage[1], 2)},
        },
        "seed": outcome.seed,
        "log": log,
    }
