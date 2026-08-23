"""模板渲染：把「模板 id + 参数」变成文本。

参数值若为 {"ref": 注册名, "id": 条目id}，会被解析成对应条目的显示名
（注册名：skill / attr / stat_word），保证战报结构（事件与参数）与展示解耦
（见 AGENTS.md 2.2.4）。
"""
from __future__ import annotations


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def format_pct(x: float) -> str:
    """0.2131 -> '21.31%'；展示层百分数保留 2 位小数（后台数值保持全浮点）。"""
    return "%.2f%%" % (float(x) * 100.0)


def format_num(x: float) -> str:
    """7.82 -> '8'；95.18 -> '95'。展示层非百分数保留整数（v0.8.0 起）。"""
    return "%.0f" % float(x)


def resolve_params(params, game):
    """把参数中的 ref 引用解析为显示名，其余原样返回。"""
    resolved = {}
    for key, value in (params or {}).items():
        if isinstance(value, dict) and "ref" in value and "id" in value:
            name = game.ref_name(value["ref"], value["id"])
            resolved[key] = name if name is not None else value["id"]
        else:
            resolved[key] = value
    return resolved


def render_template(template, params, game) -> str:
    """安全渲染：缺失的占位符原样保留，不抛异常。"""
    if template is None:
        return ""
    return str(template).format_map(_SafeFormatDict(**resolve_params(params, game)))
