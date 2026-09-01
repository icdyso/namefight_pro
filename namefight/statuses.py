"""状态 / 标记系统（v2.0.0）。

状态定义数据化于 battle.json 的 statuses 节：每个状态自带行为种类 kind、
可选的行为开关（timing / power）、数值参数规格（可共鸣参数在此声明 fmt 与
上下限）与展示文案（name / detail / desc，吸收原 buffs 节）。

引擎侧本模块提供：
- STATUS_KINDS：行为种类注册表（每种 kind 接受的参数名、可否驱散、
  dot 的合法 timing / power 取值）——config.py 据此校验状态定义；
- 运行时容器与工具：_Combatant.st（status_id -> 运行时字典）与
  _Combatant.markers（标记集合，标记 = 无参数的布尔状态）。

各 kind 的结算时机由 battle.py 的 tick / 攻击流程按固定顺序调用：
- dot（timing=every_tick 毒 / on_owner_action 流血）：周期伤害；
- control：眩晕，拥有者行动被消耗；
- shred：破甲，降低有效防御（叠层，取最大每层值）；
- guard：锻痕，受击按层减伤（上限 battle.guard_reduction_cap）；
- momentum：乘胜计数，攻击落空清零（reset_on_miss）；
- grudge：怨念层数，拥有者攻击时按层增伤（层随刻到期）；
- record：记仇记录，拥有者下次命中追加打出；
- lifesteal：嗜血，按行动数衰减的吸血；
- regen：回春，按间隔刻回复；
- charge：蓄力，替换下次行动为释放一击（保存触发时参数与共鸣）；
- will：不屈，致命伤害按衰减概率重生；
- tempo：渐入佳境，永久成长的累计展示；
- boost：背水一战，一次性永久攻速加成；
- pact：血契，行动开始献祭 + 吸血 + 行动结束转化。

运行时字段（battle.py 读写，见 _NEW_STATUS）：
poison {damage, until} / bleed {damage, until} / stun {until} /
shred {value, stacks, until, max_stacks} / guard {value, layers} /
momentum {value, cap, stacks} / grudge {value, layers} /
retribution {ratio, cap, records} / lifesteal {value, turns} /
regen {value, interval, next, until} / charge {params, links} /
will {chance, heal, decay, used} / tempo {stacks, spd_total, atk_total} /
boost {value, spd} / blood_pact {active, steal, convert, stolen, total_gain}
"""
from __future__ import annotations

from .effects import ParamSpec

# 行为种类注册表：params = 该种类状态可携带的参数名（状态定义可只声明子集）；
# dispellable = 净化可驱散；timings / powers = dot 的合法取值。
STATUS_KINDS = {
    "dot":        {"params": ("damage", "value", "ticks"),        # 周期伤害（毒/流血）
                   "dispellable": True,
                   "timings": ("every_tick", "on_owner_action"),  # 结算时机：每刻/拥有者行动时
                   "powers": ("", "atk")},                        # 伤害来源：固定值/施加者攻击系数
    "control":    {"params": ("ticks",), "dispellable": True},    # 眩晕：消耗拥有者行动
    "shred":      {"params": ("value", "ticks", "max_stacks"), "dispellable": True},   # 破甲叠层
    "guard":      {"params": ("value", "ticks"), "dispellable": True},                 # 锻痕减伤叠层
    "momentum":   {"params": ("value", "cap"), "dispellable": True},                   # 乘胜计数（落空清零）
    "grudge":     {"params": ("value", "ticks"), "dispellable": True},                 # 怨念增伤层
    "record":     {"params": ("ratio", "cap"), "dispellable": True},                   # 记仇伤害记录（净化可清）
    "lifesteal":  {"params": ("value", "turns"), "dispellable": True},                 # 嗜血（按行动数衰减）
    "regen":      {"params": ("value", "tick", "duration"), "dispellable": True},      # 回春（按间隔刻回复）
    "charge":     {"params": ("value", "crit"), "dispellable": True},                  # 蓄力（保存触发时参数）
    "will":       {"params": ("chance", "value", "decay"), "dispellable": False},      # 不屈（致命伤害重生）
    "tempo":      {"params": ("value", "atk"), "dispellable": False},                  # 渐入佳境成长累计
    "boost":      {"params": ("value", "spd"), "dispellable": False},                  # 背水一战一次性加成
    "pact":       {"params": ("cost", "value", "convert"), "dispellable": False},      # 血契献祭转化
}

# 引擎会写入状态快照的状态 id（测试据此校验配置都有对应文案）
STATUS_IDS = frozenset({
    "poison", "bleed", "stun", "shred", "charge", "momentum", "grudge",
    "guard", "retribution", "last_stand", "regen", "lifesteal", "will",
    "will_used", "tempo", "blood_pact",
})

# 可经 apply_status 施加的状态行为种类（battle.py 的 _STATUS_APPLY 键与此
# 一致；charge / boost / will / tempo / record / pact 由专属原语创建，不在此列）
APPLY_KINDS = frozenset({
    "dot", "control", "shred", "guard", "momentum", "grudge",
    "lifesteal", "regen",
})


def by_kind(c, game, kind):
    """遍历战斗者身上某行为种类的全部状态实例，返回 [(状态id, 运行时dict,
    状态定义dict), ...]；施加顺序（= dict 插入顺序）即结算顺序，确定性保证。
    v2.0.0：结算点按种类遍历而非固定 id——自建同类状态（如第二条怨念、
    新的 dot）无需改引擎即可在战斗中生效。"""
    out = []
    for sid, st in c.st.items():
        sdef = game.statuses.get(sid)
        if sdef is not None and sdef.get("kind") == kind:
            out.append((sid, st, sdef))
    return out

# 各状态的运行时初始字段（battle.py 使用；_Combatant.st 的条目骨架）。
# 字段含义见各条目行尾注释；until/layers/records 均以「刻」为期。
_NEW_STATUS = {
    "poison":     lambda: {"damage": 0.0, "until": 0},                        # 每刻伤害 / 到期刻
    "bleed":      lambda: {"damage": 0.0, "until": 0},                        # 行动时伤害 / 到期刻
    "stun":       lambda: {"until": 0},                                       # 眩晕到期刻
    "shred":      lambda: {"value": 0.0, "stacks": 0, "until": 0,
                           "max_stacks": 1},                                  # 每层破甲量/层数/到期刻/层数上限
    "guard":      lambda: {"value": 0.0, "layers": []},                       # 每层减免 / 各层到期刻列表
    "momentum":   lambda: {"value": 0.0, "cap": 0, "stacks": 0},              # 每层增伤/层数上限/当前层数
    "grudge":     lambda: {"value": 0.0, "layers": []},                       # 每层增伤 / 各层到期刻列表
    "retribution": lambda: {"ratio": 0.0, "cap": 0, "records": []},           # 追加倍率/条数上限/记录的伤害
    "lifesteal":  lambda: {"value": 0.0, "turns": 0},                         # 吸血比例 / 剩余行动数
    "regen":      lambda: {"value": 0.0, "interval": 1, "next": 0,
                           "until": 0},                                       # 每次回复量/间隔刻/下次刻/到期刻
    "charge":     lambda: {"params": {}, "links": []},                        # 蓄力触发时的原语参数与共鸣
    "will":       lambda: {"chance": 0.0, "heal": 0.0, "decay": 0.0,
                           "used": 0},                                        # 当前概率/回复比例/衰减系数/已触发次数
    "tempo":      lambda: {"stacks": 0, "spd_total": 0, "atk_total": 0},      # 层数/累计速度/累计攻击
    "last_stand": lambda: {"value": 0.0, "spd": 0.0},                         # 攻击加成 / 速度加成（背水类）
    "blood_pact": lambda: {"active": False, "steal": 0.0, "convert": 0.0,
                           "stolen": 0.0, "total_gain": 0.0},                 # 本行动生效/吸血比例/转化比例/本次吸血/累计转化攻击
}

# 快照中状态的在场上判据与参数渲染：按行为种类渲染（v2.0.0），自定义同类
# 状态与原版一视同仁；detail 模板占位符沿用 v1.x buffs 约定。
def status_display(c, tick: int, guard_cap: float, game):
    """把战斗者的在场状态渲染为快照 buff 条目 [{id, params}, ...]。
    展示 id = 状态定义 id（will 类已触发过时展示为 <id>_used）。"""
    out = []

    def add(sid, params):
        out.append({"id": sid, "params": params})

    for sid, s, sdef in by_kind(c, game, "dot"):
        if s["until"] > tick:
            add(sid, {"damage": _num(s["damage"]),
                      "turns": max(0, s["until"] - tick)})
    for sid, s, sdef in by_kind(c, game, "control"):
        if s["until"] > tick:
            add(sid, {"turns": s["until"] - tick})
    for sid, s, sdef in by_kind(c, game, "shred"):
        if s["until"] > tick and s["stacks"] > 0:
            add(sid, {"value": _num(s["value"] * s["stacks"]),
                      "stacks": s["stacks"], "turns": s["until"] - tick})
    for sid, s, sdef in by_kind(c, game, "charge"):
        add(sid, {})
    for sid, s, sdef in by_kind(c, game, "boost"):
        add(sid, {"value": _pct(s["value"]), "spd": _pct(s["spd"])})
    for sid, s, sdef in by_kind(c, game, "momentum"):
        if s["stacks"] > 0:
            add(sid, {"stacks": s["stacks"],
                      "mult": _pct(1.0 + s["value"] * s["stacks"])})
    for sid, s, sdef in by_kind(c, game, "grudge"):
        n = sum(1 for t in s["layers"] if t > tick)
        if n > 0:
            add(sid, {"stacks": n, "mult": _pct(1.0 + s["value"] * n)})
    for sid, s, sdef in by_kind(c, game, "guard"):
        n = sum(1 for t in s["layers"] if t > tick)
        if n > 0:
            add(sid, {"stacks": n, "value": _pct(min(guard_cap, s["value"] * n))})
    for sid, s, sdef in by_kind(c, game, "record"):
        if s["records"]:
            add(sid, {"stacks": len(s["records"]), "value": _num(sum(s["records"]))})
    for sid, s, sdef in by_kind(c, game, "regen"):
        if s["until"] > tick:
            add(sid, {"value": _num(s["value"]), "tick": s["interval"],
                      "turns": s["until"] - tick})
    for sid, s, sdef in by_kind(c, game, "lifesteal"):
        if s["turns"] > 0:
            add(sid, {"value": _pct(s["value"]), "turns": s["turns"]})
    for sid, s, sdef in by_kind(c, game, "will"):
        add(sid + "_used" if s["used"] else sid,
            {"value": _pct(s["chance"]), "heal": _pct(s["heal"])})
    for sid, s, sdef in by_kind(c, game, "tempo"):
        if s["stacks"] > 0:
            add(sid, {"stacks": s["stacks"], "value": _num(s["spd_total"]),
                      "atk": _num(s["atk_total"])})
    for sid, s, sdef in by_kind(c, game, "pact"):
        if s["total_gain"] > 0:
            add(sid, {"value": _num(s["total_gain"])})
    return out


def _num(x):
    from .text import format_num
    return format_num(x)


def _pct(x):
    from .text import format_pct
    return format_pct(x)


def ensure(c, sid: str) -> dict:
    """取（或创建）状态的运行时容器。"""
    st = c.st.get(sid)
    if st is None:
        st = _NEW_STATUS[sid]()
        c.st[sid] = st
    return st


def dispel_all(combatants, tick: int, game) -> int:
    """驱散双方所有可驱散状态（按行为种类的 dispellable 标记；永久成长 /
    已转化属性 / 姿态类不可驱散）。返回驱散的状态种数（供净化回复计算）。
    v2.0.0：按种类遍历，自定义同类状态同样可被驱散。"""
    count = 0
    for c in combatants:
        if c.hp <= 0:
            continue
        for kind, kdef in STATUS_KINDS.items():
            if not kdef["dispellable"]:
                continue
            for sid, s, _sdef in by_kind(c, game, kind):
                if _clear_status(c, sid, s, kind, tick):
                    count += 1
    return count


def _clear_status(c, sid: str, s: dict, kind: str, tick: int) -> bool:
    """按种类清空一个状态的在场效果；原本就不在场返回 False。"""
    if kind == "dot":
        if s["until"] > tick:
            s["until"] = 0
            return True
    elif kind == "control":
        if s["until"] > tick:
            s["until"] = 0
            return True
    elif kind == "shred":
        if s["until"] > tick and s["stacks"] > 0:
            s["until"], s["stacks"] = 0, 0
            return True
    elif kind == "charge":
        if sid in c.st:
            del c.st[sid]
            return True
    elif kind == "momentum":
        if s["stacks"] > 0:
            s["stacks"] = 0
            return True
    elif kind in ("guard", "grudge"):
        if s["layers"]:
            s["layers"] = []
            return True
    elif kind == "record":
        if s["records"]:
            s["records"] = []
            return True
    elif kind == "lifesteal":
        if s["turns"] > 0:
            s["turns"], s["value"] = 0, 0.0
            return True
    elif kind == "regen":
        if s["until"] > tick:
            s["until"] = 0
            return True
    return False


def status_param_specs(status_entry: dict):
    """状态定义（battle.json statuses 条目）-> {参数名: ParamSpec}。
    参数的 fmt / clamp / link / unit 由定义声明，kind 按名称约定。"""
    out = {}
    for key, spec in (status_entry.get("params") or {}).items():
        fmt = str(spec.get("fmt", "num"))
        clamp = spec.get("clamp")
        clamp = (None if clamp is None else
                 (float(clamp[0]), None if clamp[1] is None else float(clamp[1])))
        kind = "turns" if fmt == "turns" else ("float" if fmt == "num" else "pct")
        out[str(key)] = ParamSpec(str(key), kind, fmt, clamp,
                                  spec.get("unit"), bool(spec.get("link", False)))
    return out
