"""模板渲染：把「模板 id + 参数」变成当前语言的文本。

参数值若为 {"ref": 注册名, "id": 条目id}，会被解析成对应 locale 的显示名，
保证战报结构（事件与参数）与语言无关（见 AGENTS.md 2.2.4）。
"""
from __future__ import annotations


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def resolve_params(params, locale):
    """把参数中的 ref 引用解析为 locale 显示名，其余原样返回。"""
    resolved = {}
    for key, value in (params or {}).items():
        if isinstance(value, dict) and "ref" in value and "id" in value:
            name = locale.ref_name(value["ref"], value["id"])
            resolved[key] = name if name is not None else value["id"]
        else:
            resolved[key] = value
    return resolved


def render_template(template, params, locale) -> str:
    """安全渲染：缺失的占位符原样保留，不抛异常。"""
    if template is None:
        return ""
    return str(template).format_map(_SafeFormatDict(**resolve_params(params, locale)))
