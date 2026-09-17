/* 模板渲染与数值展示 —— namefight/text.py 的移植。
   模板占位符均为 {word} 形态（配置已核验）；缺失的占位符原样保留；
   {"ref","id"} 参数解析为对应条目显示名。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};
  var pyFix = NFE.pyFix, pyStr = NFE.pyStr;

  var PLACEHOLDER = /\{(\w+)\}/g;

  /* "%.2f%%" % (float(x) * 100.0) —— 0.2131 -> '21.31%' */
  function formatPct(x) {
    return pyFix(Number(x) * 100.0, 2) + "%";
  }

  /* "%.0f" % float(x) —— 7.82 -> '8' */
  function formatNum(x) {
    return pyFix(Number(x), 0);
  }

  function resolveParams(params, game) {
    var resolved = {};
    var src = params || {};
    for (var key in src) {
      if (!Object.prototype.hasOwnProperty.call(src, key)) continue;
      var value = src[key];
      if (value && typeof value === "object" && "ref" in value && "id" in value) {
        var name = game.refName(value.ref, value.id);
        resolved[key] = name !== null && name !== undefined ? name : value.id;
      } else {
        resolved[key] = value;
      }
    }
    return resolved;
  }

  function renderTemplate(template, params, game) {
    if (template === null || template === undefined) return "";
    var resolved = resolveParams(params, game);
    return String(template).replace(PLACEHOLDER, function (whole, key) {
      if (!Object.prototype.hasOwnProperty.call(resolved, key)) return whole;  // 缺失原样保留
      return pyStr(resolved[key]);
    });
  }

  NFE.formatPct = formatPct;
  NFE.formatNum = formatNum;
  NFE.resolveParams = resolveParams;
  NFE.renderTemplate = renderTemplate;
})(typeof window !== "undefined" ? window : globalThis);
