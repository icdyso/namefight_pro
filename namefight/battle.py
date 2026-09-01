"""确定性对战引擎（核心不变量见 AGENTS.md 2.1）。

v3.0.0 起引擎只认识**最小原子规则集**（effects.OPS，12 个）：攻击方式
（strike / hit_mod / taken_mod / grant_immune）、基础变动（stat_mod 属性 /
hp_mod 体力 / gauge_mod 行动槽）、施加与驱散（apply_status / cleanse）、
记录与控制流（record / skip_action / loop）。技能逻辑（skills.json）与
状态逻辑（battle.json statuses 的 effects 图）全部是原子在图上的组合，
battle.py 只保留**调度器**角色——tick 循环、攻击 / 防御结算流程、原子
行为实现（_OP_IMPL，与 effects.OPS 一一对应）与状态图调度。

图的执行语言（确定性契约）：节点按（技能派生顺序 × 触发节点数组顺序 ×
边数组顺序）执行；条件节点按判定走 pass / fail 分支（判断与分支）；
loop 结构节点反复执行子树（循环，第 i 轮按 decay^(i-1) 续链）；条件失败
即跳过其 pass 子树。改变任一顺序 = breaking。

tick 战斗模型（不变）：
- 每个 tick 双方行动槽（gauge）累加自身有效速度，达到阈值（10000，
  速度 ~1000 ≈ 每 10 刻一动）即可行动一次并扣回；同刻多人行动按
  （gauge 余量降序、内部序）执行；内部序 = 速度降序、规范化名字升序；
- 行动结算顺序：斩断打断 -> 拥有者行动开始状态图（流血 / 眩晕）->
  行动开始钩子 -> 眩晕吞行动 -> 攻击前钩子 -> 攻击（蓄力释放优先）；
  行动后：成长钩子、按行动衰减类到期、吸血记录清零；
- 数值量纲与取整政策不变：全程浮点、最终应用时取整一次（_r）；
- 伤害 raw = 有效ATK × 三角浮动 × 暴击倍率 × 技能倍率，
  免伤率 = 有效DEF / (有效DEF + defense_constant) × (1 − 穿透)，
  dmg = max(min_damage, raw × (1 − 免伤))；
- 被动属性 / 伤害修饰由状态定义的 mods 表聚合（statuses.sum_mod）：
  攻 / 速乘区与加值、防御减值（破甲）、增伤（乘胜 / 怨念）、减伤（锻痕）、
  吸血比例——聚合点全部按状态施加顺序遍历，确定性保证；
- 战报协议不变：模板 id + 参数 + rich 段 + 双方状态快照；原子自带的
  event 参数声明战报模板 id（配置层校验其存在）；
- record=False 时不记录战报（真战力批量模拟），随机数消耗与胜负完全一致。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from . import effects, statuses
from .config import GameCfg
from .effects import OPS  # noqa: F401  （测试/Schema 引用）
from .fighter import (Fighter, apply_resonance, compose_title_name,
                      format_resonance_final, personalized_effects,
                      resonance_coeff)
from .rng import DetRng
from .text import format_num, format_pct, render_template

# 引擎会输出的战报模板 id（测试据此校验配置都有对应文案；原子通过 event
# 参数引用的模板 id 由 config.py 校验存在于 battle_log，不在此列）
TEMPLATES_USED = frozenset({
    "battle_start", "tick_marker", "poison_death", "bleed_death",
    "skill_proc", "attack_start", "attack_crit", "attack_miss", "attack_hit",
    "poison_tick", "bleed_tick", "regen_tick", "effect_heal",
    "effect_lifesteal", "effect_poison", "effect_stun", "regen_mark",
    "effect_reduction", "effect_reflect", "effect_link", "low_hp_trigger",
    "death", "victory", "draw", "timeout",
    "charge_start", "charge_release", "thunder_hit", "sever_proc",
    "will_trigger", "purify_cleanse", "pact_proc", "pact_gain",
    "immune", "guard_stack", "grudge_stack", "tempo_stack", "lifesteal_buff",
    "shred_apply", "bleed_apply", "overload_cost", "gamble_win", "gamble_lose",
    "streak_up", "effect_bulwark", "retribution_record", "retribution_release",
    "effect_execution", "turn_stun", "shield_absorb",
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
    markers: set = field(default_factory=set)   # 标记集合（一次性 / 不屈已触发）
    damage_dealt: float = 0.0
    steal_rec: float = 0.0   # 本次行动的吸血累计（血契转化基准，行动末清零）


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


# ---- 被动修饰聚合（状态定义 mods 表；聚合点见各调用处） ----

def _eff_atk(c: _Combatant, game: GameCfg, tick: int = 0) -> float:
    """有效攻击：乘区（atk_pct，背水一战）+ 加值（atk_flat，渐入佳境）。"""
    return (c.atk * (1.0 + statuses.sum_mod(c, game, tick, "atk_pct"))
            + statuses.sum_mod(c, game, tick, "atk_flat"))


def _eff_def(c: _Combatant, game: GameCfg, tick: int = 0) -> float:
    """有效防御：扣减全部破甲类修饰（def_break × 在场层数，不低于 0）。"""
    return max(0.0, c.defense - statuses.sum_mod(c, game, tick, "def_break"))


def _eff_spd(c: _Combatant, game: GameCfg, tick: int = 0) -> float:
    """有效速度：乘区（spd_pct）+ 加值（spd_flat）。"""
    return (c.spd * (1.0 + statuses.sum_mod(c, game, tick, "spd_pct"))
            + statuses.sum_mod(c, game, tick, "spd_flat"))


def _live_value(c: _Combatant, vid: str, game: GameCfg, tick: int = 0) -> float:
    """共鸣取用的「当前值」：hp 为当前生命，atk/spd 含修饰，def 为破甲后。"""
    if vid == "hp":
        return float(max(0, c.hp))
    if vid == "atk":
        return _eff_atk(c, game, tick)
    if vid == "def":
        return _eff_def(c, game, tick)
    if vid == "spd":
        return _eff_spd(c, game, tick)
    if vid == "crit":
        return float(c.crit)
    if vid == "dodge":
        return float(c.dodge)
    return 0.0


def _hurt(c: _Combatant, amount: float, ev, rng, game: GameCfg, tick: int = 0) -> None:
    """扣除生命（先按施加顺序消耗护盾类修饰的余量池）；若致命且任一含
    lethal 块的状态（不屈类）仍可触发，按施加顺序取第一个判定：成功则
    回复一定百分比最大生命，且概率指数衰减。"""
    if amount > 0:
        absorbed = 0.0
        for sid, st, sdef in statuses.each_live(c, game, tick):
            for m in sdef.get("mods") or ():
                if m.get("kind") != "shield":
                    continue
                pool_key = m.get("pool", "value")
                pool = float(st["params"].get(pool_key, 0.0) or 0.0)
                if pool <= 0 or amount <= 0:
                    continue
                take = min(amount, pool)
                st["params"][pool_key] = pool - take
                amount -= take
                absorbed += take
        if absorbed > 0:
            ev("shield_absorb", {"a": c.name, "damage": format_num(_r(absorbed)),
                                 "value": format_num(_r(absorbed))})
        if amount <= 0:
            return
    c.hp -= amount
    for sid, st, sdef in statuses.each_live(c, game, tick):
        lethal = sdef.get("lethal")
        if not lethal or c.hp > 0:
            continue
        chance = float(statuses.resolve(lethal.get("chance", 0.0), st["params"]))
        if chance <= 0:
            continue
        if rng.next_float() < chance:
            heal_pct = float(statuses.resolve(lethal.get("value", 0.0),
                                              st["params"]))
            decay = float(statuses.resolve(lethal.get("decay", 1.0), st["params"]))
            c.hp = _r(c.max_hp * heal_pct)
            st["params"]["chance"] = chance * decay   # 概率乘算衰减
            c.markers.add("will_used:" + sid)
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
    raw = _eff_atk(actor, game, tick) * bc.atk_factor * variance * crit_mult * mult
    armor = _eff_def(enemy, game, tick)
    reduction = armor / (armor + bc.defense_constant) * (1.0 - pen)  # 免伤率
    return max(float(bc.min_damage), raw * (1.0 - reduction))


def _snapshot(combatants, threshold: float, tick: int, game: GameCfg) -> dict:
    """双方状态快照（按输入位置 a/b），状态以 id+params 存储、渲染时查
    statuses 配置文案。数值均为引擎真实值（v0.10.0 起）。"""
    def one(c):
        buffs = statuses.status_display(c, tick, game.battle.guard_reduction_cap,
                                        game)
        spd = _eff_spd(c, game, tick)
        gauge_pct = max(0.0, min(100.0, c.gauge * 100.0 / threshold))
        return {
            "hp": round(max(0.0, float(c.hp)), 2),
            "max_hp": round(float(c.max_hp), 2),
            "atk": round(_eff_atk(c, game, tick), 2),
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


# ---- 链执行器：条件分支 + 循环 + 共鸣参数计算 + 原子分发 ----

class _Ctx:
    """一次挂点执行的上下文。owner = 图归属方（技能归属者 / 状态拥有者），
    opponent = 对手；ac = 攻击链累加器；dc = 防御链累加器；dmg = 命中反应时
    的本次伤害；status = 状态图执行时的 (状态id, 运行时dict)（"$参数"
    引用与施加者折算的来源）。"""

    __slots__ = ("game", "rng", "ev", "tick", "owner", "opponent",
                 "combatants", "ac", "dc", "dmg", "skill", "node",
                 "proc_logged", "executed", "defer", "hook_name", "crit_hit",
                 "loop_i", "status")

    def __init__(self, game, rng, ev, tick, owner, opponent, combatants):
        self.game = game              # GameCfg 配置快照
        self.rng = rng                # 对战主随机源
        self.ev = ev                  # 战报发射器
        self.tick = tick              # 当前刻
        self.owner = owner            # 图归属方
        self.opponent = opponent      # 对手
        self.combatants = combatants  # 双方列表（净化等全体效果用）
        self.ac = None                # 攻击链累加器 {mult, pen, crit_flat, must_hit, crit, replaced}
        self.dc = None                # 防御链累加器 {dmg, absorbed}
        self.dmg = 0.0                # 命中反应钩子下的本次伤害
        self.skill = None             # 当前技能 SkillDef（状态图为 None）
        self.node = None              # 当前节点 dict
        self.proc_logged = False      # 本技能本次挂点是否已宣告
        self.executed = 0             # 本挂点已执行的原子计数（打断判定 / 一次性回滚用）
        self.defer = None             # 攻击链的延迟施加队列
        self.hook_name = ""           # 当前挂点名
        self.crit_hit = False         # 命中反应时的暴击标记
        self.loop_i = 0               # loop 结构节点的当前轮次（战报序号用）
        self.status = None            # 状态图上下文 (状态id, 运行时dict)


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
    """按双方当前值应用该节点的全部共鸣变数，再把 "$参数名" 引用解析为
    状态运行时参数（状态图执行时），返回本次实际参数。"""
    params = node.get("params", {})
    links = node.get("links")
    proc = dict(params)
    if links:
        owner, opp = ctx.owner, ctx.opponent
        for link in links:
            param = str(link.get("param"))
            if param not in proc:
                continue
            coeff = resonance_coeff(
                lambda vid: _live_value(owner, vid, ctx.game, ctx.tick),
                lambda vid: _live_value(opp, vid, ctx.game, ctx.tick),
                link, ctx.game)
            proc = apply_resonance(proc, coeff, param,
                                   _res_spec(ctx, node, param))
    if ctx.status is not None:
        _sid, st = ctx.status
        for key, value in list(proc.items()):
            if isinstance(value, str) and value.startswith("$"):
                proc[key] = st["params"].get(value[1:], 0.0)
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


def _cmp_source(ctx: _Ctx, source: str) -> float:
    """compare 条件的值源取值：id 形如 "self.hp_pct" / "enemy.atk"。
    比例类（hp_pct / gauge_pct / crit / dodge）为 0~1 分数，
    绝对值类（atk / def / spd）为引擎真实值（含被动修饰）。"""
    who, what = str(source).split(".", 1)
    c = ctx.owner if who == "self" else ctx.opponent
    if what == "hp_pct":
        return max(0.0, c.hp) / c.max_hp
    if what == "gauge_pct":
        return c.gauge / ctx.game.battle.gauge_threshold
    if what == "crit":
        return c.crit / 100.0
    if what == "dodge":
        return c.dodge / 100.0
    return float(_live_value(c, what, ctx.game, ctx.tick))


def _cmp(op: str, a: float, b: float) -> bool:
    """四则比较运算（lt / le / gt / ge）。"""
    if op == "lt":
        return a < b
    if op == "le":
        return a <= b
    if op == "gt":
        return a > b
    return a >= b


def _cond_pass(node: dict, ctx: _Ctx, proc: dict) -> bool:
    """条件判定（判断）：返回走哪一组成员（pass / fail 分支）。"""
    t = node["type"]
    owner, opp = ctx.owner, ctx.opponent
    game, tick = ctx.game, ctx.tick
    if t == "chance":
        return not (ctx.rng.next_float() > float(proc.get("chance", 1.0)))
    if t == "compare":
        left = _cmp_source(ctx, proc.get("left", "self.hp_pct"))
        right = (float(proc.get("value", 0.0))
                 if str(proc.get("right")) == "const"
                 else _cmp_source(ctx, proc.get("right", "enemy.hp_pct")))
        return _cmp(str(proc.get("op", "ge")), left, right)
    if t == "stacks_cmp":
        target = owner if str(proc.get("target", "self")) == "self" else opp
        sid = str(proc.get("status"))
        n = statuses.live_stacks(target, sid, tick, game.statuses.get(sid, {}))
        return _cmp(str(proc.get("op", "ge")), float(n),
                    float(proc.get("value", 0.0)))
    if t == "has_status":
        sid = str(proc.get("status"))
        sdef = game.statuses.get(sid, {})
        return statuses.live(owner, sid, tick, sdef)
    if t == "no_status":
        sid = str(proc.get("status"))
        sdef = game.statuses.get(sid, {})
        return not statuses.live(owner, sid, tick, sdef)
    if t == "has_marker":
        return ("mk:" + str(proc.get("key"))) in owner.markers
    if t == "no_marker":
        return ("mk:" + str(proc.get("key"))) not in owner.markers
    if t == "once_per_battle":
        marker = "once:" + str(proc.get("key"))
        if marker in owner.markers:
            return False
        owner.markers.add(marker)
        return True
    if t == "last_crit":
        return bool(ctx.crit_hit)
    return True


def _run_tree(tree, ctx: _Ctx):
    """递归执行一棵链树；返回子树传出的信号
    （'consume' 吞掉行动 / 'immune' 免疫终止防御链）。
    条件节点按判定结果走 pass / fail 分支（分支）；loop 结构节点反复执行
    子树（循环：第 1 轮必定执行，第 i 轮按 decay^(i-1) 续链，至多 max 轮）。"""
    node, children = tree
    ctx.node = node
    if node["kind"] == "trigger":
        # 触发节点本身只是时机声明：直通执行其子树
        for _gate, child in children:
            sig = _run_tree(child, ctx)
            if sig:
                return sig
        return None
    if node["kind"] == "condition":
        proc = _proc_params(node, ctx)
        want = "pass" if _cond_pass(node, ctx, proc) else "fail"
        once_marker = None
        if node["type"] == "once_per_battle" and want == "pass":
            # 一次性条件：仅当下游子树真的执行了原子才消耗标记，
            # 否则回滚（如「生命过低时，每场一次」——未到时机不占次数）
            once_marker = "once:" + str(proc.get("key"))
        executed_before = ctx.executed
        for gate, child in children:
            if gate == want:
                sig = _run_tree(child, ctx)
                if sig:
                    return sig
        if once_marker is not None and ctx.executed == executed_before:
            ctx.owner.markers.discard(once_marker)
        return None
    if node["kind"] == "struct":                    # 循环结构（loop）
        proc = _proc_params(node, ctx)
        max_rounds = max(1, int(proc.get("max", 1)))
        mode = str(proc.get("mode", "chain"))
        decay = float(proc.get("decay", 0.9))
        for i in range(1, max_rounds + 1):
            # chain：首轮必中、后续按 decay^(i-1) 续链；count：固定轮数不掷骰
            if mode == "chain" and i > 1 \
                    and ctx.rng.next_float() >= decay ** (i - 1):
                break
            ctx.loop_i = i
            for _gate, child in children:
                sig = _run_tree(child, ctx)
                if sig:
                    return sig
            if ctx.owner.hp <= 0 or ctx.opponent.hp <= 0:
                break
        ctx.loop_i = 0
        return None
    # 原子节点
    proc = _proc_params(node, ctx)
    ctx.executed += 1
    _announce(ctx, node, proc)
    sig = _OP_IMPL[node["type"]](ctx, proc)
    if sig:
        return sig
    for gate, child in children:
        sig = _run_tree(child, ctx)
        if sig:
            return sig
    return None


def _announce(ctx: _Ctx, node: dict, proc: dict):
    """技能宣告：首次执行的需要宣告的原子输出「使用了技能」行（每技能
    每次挂点至多一条，与 v1.x 一致；状态图无技能归属，不宣告），
    随后输出共鸣事件。"""
    hook = ctx.hook_name
    logged = OPS[node["type"]]["logged"]
    if node["type"] == "apply_status":
        sdef = ctx.game.statuses.get(str(proc.get("status")), {})
        logged = bool(sdef.get("logged", logged))
    if logged and hook in _PROC_HOOKS and not ctx.proc_logged \
            and ctx.skill is not None:
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


def _run_status_hooks(c, opponent, combatants, hook: str, game, rng, ev,
                      tick: int, only=None):
    """执行战斗者在场状态的某钩子效果图（按施加顺序，确定性保证）。
    only = (状态id, 运行时dict) 时只执行该状态（tick 到期调度用）。
    返回子树传出的信号（如眩晕的 consume）。"""
    ctx = _Ctx(game, rng, ev, tick, c, opponent, combatants)
    ctx.hook_name = hook
    for sid, st, _sdef in statuses.each_live(c, game, tick):
        if only is not None and sid != only[0]:
            continue
        plan = game.status_plans.get(sid, {}).get(hook)
        if not plan:
            continue
        ctx.status = (sid, st)
        for tree in plan:
            sig = _run_tree(tree, ctx)
            if sig:
                return sig
    return None


# ---- 原子实现（与 effects.OPS 一一对应；12 个最小原子） ----

def _strike_target(ctx, proc):
    """攻击原子的目标：enemy 对手 / self 自己。"""
    return ctx.opponent if str(proc.get("target", "enemy")) == "enemy" \
        else ctx.owner


def _op_strike(ctx, proc):
    """strike 攻击原子。mode=extra 追加打击（完整攻击管线：闪避 / 暴击 /
    防御 / 受击反应 / 吸血）；mode=replace 亦为完整打击并标记替换本次攻击
    （雷罚连击、蓄力释放）；basis != none 时为附加伤害（记仇释放 / 反甲
    反弹：不闪避不暴击不防御，直接结算）。real=真伤（无视防御 / 闪避 /
    暴击，但吃减免并触发受击反应）。"""
    game, rng, ev = ctx.game, ctx.rng, ctx.ev
    bc = game.battle
    owner = ctx.owner
    target = _strike_target(ctx, proc)
    basis = str(proc.get("basis", "none"))
    mode = str(proc.get("mode", "extra"))
    value = float(proc.get("value", 1.0))
    event = proc.get("event")

    if basis != "none":
        # 附加伤害：recorded_sum 记录总和（记仇，消耗全部记录）/
        # taken_absorbed 本次被减免量（反甲反弹）
        if basis == "recorded_sum":
            total = 0.0
            for _sid, st, _sdef in statuses.each_live(owner, game, ctx.tick):
                if st["records"]:
                    total += sum(st["records"])
                    st["records"] = []
            amount = _r(total * value)
            event = event or "retribution_release"
        else:
            absorbed = ctx.dc["absorbed"] if ctx.dc else 0.0
            amount = _r(absorbed * value)
            event = event or "effect_reflect"
        if amount <= 0 or target.hp <= 0:
            return None
        _hurt(target, amount, ev, rng, game, ctx.tick)
        owner.damage_dealt += amount
        ev(str(event), {"a": owner.name, "b": target.name,
                        "damage": format_num(amount),
                        "value": format_num(amount),
                        "ratio": format_pct(value), "hit": ctx.loop_i})
        return None

    mult = float(proc.get("mult", 1.0))
    real = bool(proc.get("real", False))
    must_hit = bool(proc.get("must_hit", False))
    pen = float(proc.get("pen", 0.0))
    crit_bonus = float(proc.get("crit_bonus", 0.0))
    if target.hp <= 0 or owner.hp <= 0:
        return None
    # 闪避判定（真实伤害 / 必中跳过）
    if not real and not must_hit:
        if rng.next_float() < target.dodge / 100.0:
            ev("attack_miss", {"a": owner.name, "b": target.name})
            return None
    crit = False
    if not real:
        crit = rng.next_float() < min(bc.crit_cap / 100.0,
                                      owner.crit / 100.0 + crit_bonus)
        if crit:
            ev("attack_crit", {})
    if real:
        # 真实伤害：无视防御与暴击，仅三角浮动（吃防守减免、触发受击反应）
        variance = rng.next_triangular(bc.variance_lo, bc.variance_hi)
        dmg = max(float(bc.min_damage),
                  _eff_atk(owner, game, ctx.tick) * bc.atk_factor * mult * variance)
    else:
        dmg = _compute_damage(owner, target, mult, crit, game, rng,
                              pen=pen, tick=ctx.tick)
    dmg = _defend(target, owner, dmg, game, rng, ev, ctx.tick)
    dmg = _r(dmg)
    if dmg > 0:
        _hurt(target, dmg, ev, rng, game, ctx.tick)
        owner.damage_dealt += dmg
        ev(str(event or "attack_hit"),
           {"a": owner.name, "b": target.name, "damage": format_num(dmg),
            "mult": format_pct(mult), "crit": format_pct(crit_bonus),
            "hit": ctx.loop_i})
        _apply_lifesteal(owner, dmg, ev, game, ctx.tick)
        lifesteal = float(proc.get("lifesteal", 0.0))   # 本次攻击的专属吸血
        if lifesteal > 0 and owner.hp > 0:
            gained = _r(min(dmg * lifesteal, owner.max_hp - owner.hp))
            if gained > 0:
                owner.hp += gained
                owner.steal_rec += gained
                ev("effect_lifesteal", {"a": owner.name,
                                        "heal": format_num(gained)})
        _hit_reactions(owner, target, dmg, crit, game, rng, ev, ctx.tick)
    if mode == "replace" and ctx.ac is not None:
        ctx.ac["replaced"] = True     # 标记替换本次攻击（攻击管线查验）
    return None


def _op_hit_mod(ctx, proc):
    """hit_mod：修饰本次攻击（倍率乘区 / 穿透取大 / 暴击加成 / 必中）。"""
    ac = ctx.ac
    mult = float(proc.get("mult", 1.0))
    if mult != 1.0:
        ac["mult"] *= mult
    ac["pen"] = max(ac["pen"], float(proc.get("pen", 0.0)))
    ac["crit_flat"] += float(proc.get("crit_bonus", 0.0))
    if proc.get("must_hit"):
        ac["must_hit"] = True
    event = proc.get("event")
    if event:
        ctx.ev(str(event), {"mult": format_pct(mult)})
    elif proc.get("announce", False):
        ctx.ev("effect_execution", {"mult": format_pct(ac["mult"])})
    return None


def _op_taken_mod(ctx, proc):
    """taken_mod：减免本次所受伤害的一部分（被减免量计入 absorbed，
    供反甲的 taken_absorbed 基准反弹）。"""
    bc = ctx.game.battle
    ratio = float(proc.get("cut", 0.0))
    if ctx.dc["dmg"] > 0 and ratio > 0:
        avoided = ctx.dc["dmg"] * ratio
        ctx.dc["absorbed"] += avoided
        ctx.dc["dmg"] = max(float(bc.min_damage), ctx.dc["dmg"] - avoided)
        event = proc.get("event")
        if event:
            ctx.ev(str(event), {"b": ctx.owner.name, "ratio": format_pct(ratio)})
    return None


def _op_grant_immune(ctx, proc):
    """grant_immune：完全免疫本次伤害（清零并终止防御链）。
    触发概率由上游 chance 条件表达（分支）。"""
    if ctx.dc["dmg"] > 0:
        ctx.dc["dmg"] = 0.0
        ctx.dc["absorbed"] = 0.0
        ctx.ev(str(proc.get("event") or "immune"), {"b": ctx.owner.name})
        return "immune"
    return None


def _op_stat_mod(ctx, proc):
    """stat_mod：属性变动原子（永久 ±）。basis=flat 直接增量；
    basis=recorded_lifesteal 时增量 = value × 本次行动记录的吸血总量
    （血契转化）。status 参数（可选）把增量累计到某状态的 total 供展示。"""
    owner = ctx.owner if str(proc.get("target", "self")) == "self" \
        else ctx.opponent
    gain = float(proc.get("gain", 0.0))
    if str(proc.get("basis", "flat")) == "recorded_lifesteal":
        gain = float(proc.get("value", 0.0)) * owner.steal_rec
    gain = _r(gain)
    if gain == 0:
        return None
    stat = str(proc.get("stat", "atk"))
    if stat == "hp":
        owner.max_hp += gain
        owner.hp = max(1.0, owner.hp + gain)
    elif stat == "def":
        owner.defense = max(0.0, owner.defense + gain)
    else:
        setattr(owner, stat, max(0.0, getattr(owner, stat) + gain))
    sid = proc.get("status")
    if sid:
        st = statuses.ensure(owner, str(sid))
        st["total"] += gain
        st["params"].setdefault("value", 0.0)
    event = proc.get("event")
    if event:
        ctx.ev(str(event), {"a": owner.name, "value": format_num(gain),
                            "atk": format_num(_eff_atk(owner, ctx.game,
                                                       ctx.tick))})
    return None


def _op_hp_mod(ctx, proc):
    """hp_mod：体力变动原子。type=heal 治疗（不溢出）；type=loss 流失
    （不触发受击反应与不屈；can_kill=true 可致死——毒 / 流血，由调用方在
    状态结算后检查死亡并输出 death_event；floor1=true 保底 1 点——燃血 /
    血契献祭）。basis：flat 固定量 / maxhp 最大生命比例 / applier_atk
    施加者攻击 × value（撕裂，需状态图上下文）/ dealt 本次造成伤害 × value
    （吸血）。"""
    game = ctx.game
    target = ctx.owner if str(proc.get("target", "self")) == "self" \
        else ctx.opponent
    basis = str(proc.get("basis", "flat"))
    value = float(proc.get("value", 0.0))          # flat 基准的固定量
    ratio = float(proc.get("ratio", 0.0))          # 比例基准的系数
    if basis == "maxhp":
        amount = target.max_hp * ratio
    elif basis == "curhp":
        amount = target.hp * ratio
    elif basis == "applier_atk":
        applier = ctx.status[1]["applier"] if ctx.status else ctx.owner
        amount = _eff_atk(applier, game, ctx.tick) * game.battle.atk_factor * ratio
    elif basis == "dealt":
        amount = ctx.dmg * ratio
    else:
        amount = value
    if str(proc.get("type", "heal")) == "heal":
        gained = _r(min(amount, target.max_hp - target.hp))
        if gained > 0:
            target.hp += gained
            ctx.ev(str(proc.get("event") or "effect_heal"),
                   {"a": target.name, "heal": format_num(gained),
                    "value": format_num(gained)})
        return None
    # 体力流失（取整契约：最终应用时取整一次）
    if proc.get("floor1"):
        cost = _r(amount)
        target.hp = max(1.0, target.hp - cost)
        ctx.ev(str(proc.get("event") or "overload_cost"),
               {"a": target.name, "cost": format_num(cost),
                "value": format_num(cost)})
    else:
        loss = _r(amount)            # can_kill：可能致死（毒 / 流血）
        target.hp -= loss
        event = proc.get("event")
        if event:
            ctx.ev(str(event), {"a": target.name, "damage": format_num(loss),
                                "value": format_num(loss)})
    return None


def _op_gauge_mod(ctx, proc):
    """gauge_mod：行动槽推进 / 倒退（×100 量纲；斩断倒退 / 疾影前进）。"""
    target = ctx.owner if str(proc.get("target", "self")) == "self" \
        else ctx.opponent
    target.gauge = max(0.0, target.gauge + _r(float(proc.get("gain", 0.0))))
    return None


def _op_apply_status(ctx, proc):
    """apply_status：施加状态（唯一状态入口）。数值参数覆盖定义默认值
    （个性化 / 共鸣即作用于此）；叠层 / 到期 / 间隔策略由状态定义声明。
    攻击链上对敌方施加的状态延迟到命中后生效（与 v1.x 一致）。"""
    sid = str(proc.get("status"))
    target = ctx.opponent if str(proc.get("target")) == "enemy" else ctx.owner
    if ctx.hook_name == "on_attack" and target is ctx.opponent:
        snapshot_proc = dict(proc)
        ctx.defer.append(lambda: _apply_status_now(ctx, sid, snapshot_proc,
                                                   target))
        return None
    _apply_status_now(ctx, sid, proc, target)
    return None


def _apply_status_now(ctx: _Ctx, sid: str, proc: dict, target):
    """立即施加状态：合并参数（定义默认 <- 施加覆盖 <- 旧值保持）、按
    stack 策略叠层、按 expire 策略持续，随后输出定义声明的施加事件。"""
    game = ctx.game
    sdef = game.statuses.get(sid)
    if sdef is None:
        return
    st = statuses.ensure(target, sid)
    merged = dict(statuses.status_defaults(sdef))
    merged.update({k: v for k, v in proc.items()
                   if k not in ("status", "target")})
    st["params"].update(merged)
    st["applier"] = ctx.owner
    st["links"] = list(ctx.node.get("links") or [])
    turns = max(1, int(st["params"].get("turns", 1)))
    stack = sdef.get("stack", "refresh")
    if stack == "layers":
        st["layers"].append(ctx.tick + turns)
    elif stack == "count":
        cap = int(st["params"].get("max_stacks", sdef.get("max_stacks", 0)) or 0)
        if st["stacks"] >= cap > 0:
            return                      # 已满层：不再叠加也不刷新（乘胜到顶）
        st["stacks"] += 1
        st["expires"] = ctx.tick + turns
    else:                               # refresh：刷新持续与数值
        if sdef.get("expire") == "actions":
            st["actions"] = turns
        elif sdef.get("expire") != "none":
            st["expires"] = ctx.tick + turns
        interval = statuses.resolve(sdef.get("interval", 0), st["params"])
        if interval:
            st["next"] = ctx.tick + max(1, int(interval))
    # 施加事件（定义 event 字段；参数为通用集，模板按需引用）
    event = sdef.get("event")
    if event:
        n = statuses.live_stacks(target, sid, ctx.tick, sdef)
        params = {"a": ctx.owner.name, "b": target.name,
                  "turns": turns, "stacks": n, "hit": ctx.loop_i}
        for key, val in st["params"].items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                fmt = ((sdef.get("params") or {}).get(key) or {}).get("fmt", "num")
                params[key] = (format_pct(float(val)) if fmt == "pct"
                               else format_num(float(val)))
        for m in sdef.get("mods") or ():
            if m.get("kind") == "dmg_out_pct":
                v = float(statuses.resolve(m.get("value", 0.0), st["params"]))
                per = n if m.get("per_stack") else 1
                params["mult"] = format_pct(1.0 + v * per)
        ctx.ev(str(event), params)
    # 施加钩子效果图（on_status_apply）
    plan = game.status_plans.get(sid, {}).get("on_status_apply")
    if plan:
        sctx = _Ctx(ctx.game, ctx.rng, ctx.ev, ctx.tick,
                    target, ctx.opponent if target is ctx.owner else ctx.owner,
                    ctx.combatants)
        sctx.hook_name = "on_status_apply"
        sctx.status = (sid, st)
        for tree in plan:
            _run_tree(tree, sctx)


def _op_cleanse(ctx, proc):
    """cleanse：驱散状态（scope=both 双方 / self / enemy），并按驱散种数
    回复生命。"""
    scope = str(proc.get("scope", "both"))
    targets = list(ctx.combatants)
    if scope == "self":
        targets = [ctx.owner]
    elif scope == "enemy":
        targets = [ctx.opponent]
    count = statuses.dispel_all(targets, ctx.tick, ctx.game)
    healed = _r(min(float(proc.get("value", 0.0))
                    + float(proc.get("per", 0.0)) * count,
                    ctx.owner.max_hp - ctx.owner.hp))
    if healed > 0:
        ctx.owner.hp += healed
    ctx.ev("purify_cleanse", {"a": ctx.owner.name, "count": count,
                              "heal": format_num(healed)})
    return None


def _op_skip_action(ctx, proc):
    """skip_action：吞掉拥有者的本次行动（眩晕；返回 consume 信号）。"""
    ctx.ev(str(proc.get("event") or "turn_stun"), {"a": ctx.owner.name})
    return "consume"


def _op_record(ctx, proc):
    """record：记录。what=damage_taken 把本次所受伤害记入 status 参数指向
    的状态（记仇，至多 cap 条，下次命中以 strike basis=recorded_sum 释放）；
    what=lifesteal 把本次吸血量累计到战斗者（血契转化的基准）。"""
    if str(proc.get("what", "damage_taken")) == "lifesteal":
        ctx.owner.steal_rec += ctx.dmg
        return None
    sid = str(proc.get("status"))
    st = statuses.ensure(ctx.owner, sid)
    cap = int(proc.get("cap", 0) or 0)
    if cap and len(st["records"]) >= cap:
        return None
    st["records"].append(float(ctx.dmg))
    ctx.ev("retribution_record", {"a": ctx.owner.name,
                                  "damage": format_num(ctx.dmg),
                                  "value": format_num(ctx.dmg),
                                  "stacks": len(st["records"])})
    return None


def _op_marker(ctx, proc):
    """marker：标记设置 / 清除（has_marker / no_marker 条件的判据；
    前缀 mk: 与一次性 / 不屈的内部标记隔离）。"""
    key = "mk:" + str(proc.get("key"))
    if str(proc.get("action", "set")) == "clear":
        ctx.owner.markers.discard(key)
    else:
        ctx.owner.markers.add(key)
    return None


def _op_status_ctl(ctx, proc):
    """status_ctl：状态操控——extend / shorten 延长或缩短在场刻数
    （layers 模式平移各层到期），stacks 增减层数（count 模式，可负），
    clear 强制清除（无视 dispellable）。"""
    game = ctx.game
    target = ctx.owner if str(proc.get("target", "self")) == "self" \
        else ctx.opponent
    sid = str(proc.get("status"))
    sdef = game.statuses.get(sid, {})
    st = target.st.get(sid)
    if st is None or not statuses.live(target, sid, ctx.tick, sdef):
        return None                      # 不在场：无可操控
    op = str(proc.get("op", "extend"))
    value = _r(float(proc.get("value", 0.0)))
    if op == "extend":
        if sdef.get("stack") == "layers":
            st["layers"] = [t + max(0, int(value)) for t in st["layers"]]
        else:
            st["expires"] += max(0, int(value))
    elif op == "shorten":
        if sdef.get("stack") == "layers":
            st["layers"] = [t - max(0, int(value)) for t in st["layers"]]
        else:
            st["expires"] -= max(0, int(value))
    elif op == "stacks":
        cap = int(st["params"].get("max_stacks", sdef.get("max_stacks", 0)) or 0)
        st["stacks"] = max(0, st["stacks"] + int(value))
        if cap > 0:
            st["stacks"] = min(cap, st["stacks"])
    elif op == "clear":
        st["expires"] = 0
        st["layers"] = []
        st["stacks"] = 0
        st["actions"] = 0
        st["records"] = []
    event = proc.get("event")
    if event:
        ctx.ev(str(event), {"a": ctx.owner.name, "b": target.name,
                            "value": format_num(value)})
    return None


_OP_IMPL = {
    "strike": _op_strike,
    "hit_mod": _op_hit_mod,
    "taken_mod": _op_taken_mod,
    "grant_immune": _op_grant_immune,
    "stat_mod": _op_stat_mod,
    "hp_mod": _op_hp_mod,
    "gauge_mod": _op_gauge_mod,
    "apply_status": _op_apply_status,
    "cleanse": _op_cleanse,
    "skip_action": _op_skip_action,
    "record": _op_record,
    "marker": _op_marker,
    "status_ctl": _op_status_ctl,
}

_STRUCT_IMPL = ("loop",)   # 结构节点（loop）在 _run_tree 内联执行

assert set(_OP_IMPL) == set(effects.OP_TYPES), "原子实现与注册表不一致"
assert set(_STRUCT_IMPL) == set(effects.STRUCTS), "结构注册表不一致"


# ---- 结算流程 ----

def _defend(defender, attacker, dmg, game, rng, ev, tick):
    """防守方结算：锻痕类减伤修饰（mods 聚合）-> 防御钩子链（taken_mod
    减免 / grant_immune 免疫终止 / 反甲反弹 / 锻痕叠层施加）。
    返回最终伤害（反弹伤害直接作用于攻击方）。"""
    bc = game.battle
    for sid, st, sdef in statuses.each_live(defender, game, tick):
        for m in sdef.get("mods") or ():
            if m.get("kind") != "dmg_in_cut_pct" or dmg <= 0:
                continue
            n = statuses.live_stacks(defender, sid, tick, sdef)
            v = float(statuses.resolve(m.get("value", 0.0), st["params"]))
            ratio = min(bc.guard_reduction_cap, v * (n if m.get("per_stack") else 1))
            dmg = max(float(bc.min_damage), dmg * (1.0 - ratio))
            ev("effect_reduction", {"b": defender.name,
                                    "ratio": format_pct(ratio)})
    dc = {"dmg": dmg, "absorbed": 0.0}   # 防御链累加器
    ctx = _Ctx(game, rng, ev, tick, defender, attacker, [defender, attacker])
    ctx.dc = dc
    if _run_hook(defender.skills, "on_defend", ctx) == "immune":
        return 0.0
    return dc["dmg"]


def _apply_lifesteal(actor, dmg, ev, game, tick):
    """命中后的吸血结算：聚合全部在场状态的 lifesteal_pct 修饰（嗜血 /
    血契共享同一口吸血），带 record=lifesteal 标志的记录到本次行动累计。"""
    if dmg <= 0 or actor.hp <= 0:
        return
    steal = 0.0
    record_sids = []
    for sid, st, sdef in statuses.each_live(actor, game, tick):
        for m in sdef.get("mods") or ():
            if m.get("kind") != "lifesteal_pct":
                continue
            n = statuses.live_stacks(actor, sid, tick, sdef)
            v = float(statuses.resolve(m.get("value", 0.0), st["params"]))
            steal += v * (n if m.get("per_stack") else 1)
            if m.get("record") == "lifesteal":
                record_sids.append(sid)
    if steal <= 0:
        return
    gained = _r(min(dmg * steal, actor.max_hp - actor.hp))
    if gained <= 0:
        return
    actor.hp += gained
    actor.steal_rec += gained       # 全部吸血都记入本次行动累计（转化基准）
    ev("effect_lifesteal", {"a": actor.name, "heal": format_num(gained)})
    # 带 record 标志的吸血状态单独累计（供其状态展示；血契）
    for sid in record_sids:
        st = actor.st.get(sid)
        if st is not None:
            st["total"] += gained


def _hit_reactions(actor, enemy, dmg, crit, game, rng, ev, tick):
    """一次命中后的反应结算：攻击方的命中钩子（乘胜叠层 / 疾影推进 /
    记仇释放）与受击方的被命中钩子（怨念积攒 / 记仇记录）。"""
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


def _attack(actor, enemy, game: GameCfg, rng, ev, tick: int):
    bc = game.battle
    combatants = [actor, enemy]

    # ---- 普通攻击宣告：让普攻在战报中同样可见（v1.0.0） ----
    ev("attack_start", {"a": actor.name})

    # ---- 蓄力释放：首个带 on_owner_action_consume 效果图的状态替换本次行动 ----
    for sid, st, sdef in statuses.each_live(actor, game, tick):
        plan = game.status_plans.get(sid, {}).get("on_owner_action_consume")
        if not plan:
            continue
        del actor.st[sid]           # 蓄力为一次性：释放即移除
        ctx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
        ctx.hook_name = "on_owner_action_consume"
        # 释放前按当前值重算蓄力参数的共鸣（与 v2 一致；伪节点按
        # apply_status 形态构建以复用状态参数规格查询）
        if st["links"]:
            fake = {"kind": "op", "type": "apply_status",
                    "params": dict(st["params"]), "links": st["links"]}
            proc = _proc_params(fake, ctx)
            st["params"] = {k: v for k, v in proc.items()
                            if not isinstance(v, str)}
        ctx.status = (sid, st)
        for tree in plan:
            sig = _run_tree(tree, ctx)
            if sig:
                return
        return

    # ---- 攻击钩子链（技能按派生顺序） ----
    ac = {"mult": 1.0, "pen": 0.0, "crit_flat": 0.0, "must_hit": False,
          "crit": False, "replaced": False}
    ctx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
    ctx.ac = ac
    ctx.defer = []
    if _run_hook(actor.skills, "on_attack", ctx) == "consume":
        return                       # 蓄力占用了本次行动（skip_action 信号）
    if ac["replaced"]:
        return                       # 雷罚等 strike(mode=replace) 已替换攻击

    # 增伤修饰聚合（乘胜 / 怨念：dmg_out_pct × 在场层数）
    out_pct = statuses.sum_mod(actor, game, tick, "dmg_out_pct")
    if out_pct > 0:
        ac["mult"] *= 1.0 + out_pct

    # ---- 闪避判定（落空时乘胜类清零；必中跳过） ----
    if not ac["must_hit"] and rng.next_float() < enemy.dodge / 100.0:
        ev("attack_miss", {"a": actor.name, "b": enemy.name})
        statuses.clear_stacks(actor, game, tick)
        return

    crit = rng.next_float() < min(bc.crit_cap / 100.0,
                                  actor.crit / 100.0 + ac["crit_flat"])
    ac["crit"] = crit
    if crit:
        ev("attack_crit", {})
    dmg = _compute_damage(actor, enemy, ac["mult"], crit, game, rng,
                          pen=ac["pen"], tick=tick)
    dmg = _defend(enemy, actor, dmg, game, rng, ev, tick)

    dmg = _r(dmg)
    if dmg > 0:
        _hurt(enemy, dmg, ev, rng, game, tick)
        actor.damage_dealt += dmg
    ev("attack_hit", {"a": actor.name, "b": enemy.name,
                      "damage": format_num(dmg)})
    _apply_lifesteal(actor, dmg, ev, game, tick)
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
    joined = bc.seed_separator.join(sorted((fighter_a.normalized,
                                            fighter_b.normalized)))
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

    def _status_death(c):
        """状态结算致死（毒 / 流血）：输出定义的 death_event 并结束战斗。
        返回 True 表示战斗结束。"""
        nonlocal winner
        if c.hp > 0:
            return False
        for sid, st, sdef in statuses.each_live(c, game, tick):
            death_event = sdef.get("death_event")
            if death_event:
                ev(str(death_event), {"a": c.name})
                break
        winner = internal[1] if c is internal[0] else internal[0]
        return True

    while tick < bc.max_ticks and winner is None and not draw:
        tick += 1
        # ---- 每刻开始：状态 tick 图（毒发 / 回春回复，按施加顺序） ----
        for c in internal:
            if c.hp <= 0:
                continue
            enemy = internal[1] if c is internal[0] else internal[0]
            for sid, st, sdef in statuses.each_live(c, game, tick):
                interval = statuses.resolve(sdef.get("interval", 0),
                                            st["params"])
                if not interval or tick < st["next"]:
                    continue
                st["next"] += max(1, int(interval))
                _run_status_hooks(c, enemy, combatants, "on_status_tick",
                                  game, rng, ev, tick, only=(sid, st))
                if _status_death(c):
                    break
            if winner is not None:
                break
        if winner is not None:
            break
        # ---- 行动槽推进 ----
        for c in combatants:
            if c.hp > 0:
                c.gauge += _eff_spd(c, game, tick)
        ready = [c for c in internal if c.hp > 0 and c.gauge >= bc.gauge_threshold]
        ready.sort(key=lambda c: (-c.gauge, c.seq))
        for actor in ready:
            enemy = internal[1] if actor is internal[0] else internal[0]
            actor.gauge -= bc.gauge_threshold
            if actor.hp <= 0 or enemy.hp <= 0:
                break
            # ---- 打断钩子：敌方即将行动时（斩断退条 + 抢攻，消耗其行动） ----
            ictx = _Ctx(game, rng, ev, tick, enemy, actor, combatants)
            ictx.defer = []
            _run_hook(enemy.skills, "action_interrupt", ictx)
            for fn in ictx.defer or ():
                fn()
            if ictx.executed:
                if _settle_deaths(enemy, actor):
                    break
                continue
            # ---- 拥有者行动开始状态图（流血损失 / 眩晕吞行动） ----
            sig = _run_status_hooks(actor, enemy, combatants, "on_owner_action",
                                    game, rng, ev, tick)
            if _status_death(actor):
                break
            if sig == "consume":
                continue
            # ---- 行动开始钩子（血契 / 回春 / 净化） ----
            sctx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
            sctx.defer = []
            _run_hook(actor.skills, "action_start", sctx)
            for fn in sctx.defer or ():
                fn()
            if _settle_deaths(actor, enemy):
                break
            # ---- 攻击前钩子（背水一战等一次性判定） ----
            bctx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
            _run_hook(actor.skills, "before_attack", bctx)
            _attack(actor, enemy, game, rng, ev, tick)
            if _settle_deaths(actor, enemy):
                break
            # ---- 行动后：成长钩子 / 按行动衰减到期 / 吸血累计清零 ----
            if actor.hp > 0:
                actx = _Ctx(game, rng, ev, tick, actor, enemy, combatants)
                _run_hook(actor.skills, "after_action", actx)
                for sid, st, sdef in statuses.each_live(actor, game, tick):
                    if sdef.get("expire") == "actions":
                        st["actions"] -= 1
                actor.steal_rec = 0.0

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
