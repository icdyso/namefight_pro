/* 表达式与变量表 —— namefight/expr.py 的移植。
   手写递归下降解析器（绝不用 eval）；除零按 0；按表达式文本缓存。
   变量以 $ 开头（平表：'self.hp' / 'enemy.atk' / 'ctx.dmg' / 施加参数名）。 */
(function (root) {
  "use strict";
  var NFE = root.NFE = root.NFE || {};

  var RE_ALPHA = /\p{L}/u;
  var RE_SPACE = /\s/u;

  function isPathChar(ch) {
    if (ch >= "a" && ch <= "z") return true;
    if (ch >= "A" && ch <= "Z") return true;
    if (ch >= "0" && ch <= "9") return true;
    if (ch === "_" || ch === "." || ch === ":") return true;
    return ch >= "\u4E00" && ch <= "\u9FFF";      // 中文（标记键 / 状态名）
  }

  var FUNCS = {
    min: function (a, b) { return Math.min(a, b); },
    max: function (a, b) { return Math.max(a, b); },
    abs: function (x) { return Math.abs(x); },
    floor: function (x) { return Math.floor(x); },
    pow: function (x, y) { return Math.pow(x, y); }
  };

  function ExprError(msg) {
    var e = new Error(msg);
    e.name = "ExprError";
    return e;
  }

  function tokenize(text) {
    var tokens = [];
    var i = 0, n = text.length;
    while (i < n) {
      var ch = text[i];
      if (RE_SPACE.test(ch)) { i++; continue; }
      var isDigit = ch >= "0" && ch <= "9";
      if (isDigit || (ch === "." && i + 1 < n && text[i + 1] >= "0" && text[i + 1] <= "9")) {
        var j = i;
        while (j < n && ((text[j] >= "0" && text[j] <= "9") || text[j] === ".")) j++;
        tokens.push(["num", Number(text.slice(i, j))]);
        i = j;
        continue;
      }
      if (ch === "$") {
        var k = i + 1;
        while (k < n && isPathChar(text[k])) k++;
        if (k === i + 1) throw ExprError("变量引用 $ 后为空: " + text);
        tokens.push(["var", text.slice(i + 1, k)]);
        i = k;
        continue;
      }
      if (RE_ALPHA.test(ch)) {
        var m = i;
        while (m < n && (RE_ALPHA.test(text[m]) || text[m] === "_")) m++;
        tokens.push(["name", text.slice(i, m)]);
        i = m;
        continue;
      }
      if ("+-*/(),".indexOf(ch) >= 0) {
        tokens.push([ch, ch]);
        i++;
        continue;
      }
      throw ExprError("表达式含非法字符 " + ch + ": " + text);
    }
    return tokens;
  }

  /* 解析为闭包树：与 Python 相同的递归下降结构 */
  function parse(tokens) {
    var pos = 0;

    function peek() {
      return pos < tokens.length ? tokens[pos] : [null, null];
    }
    function take(expected) {
      var t = peek();
      if (expected !== undefined && t[0] !== expected) {
        throw ExprError("期望 " + expected + "，得到 " + t[0]);
      }
      pos++;
      return t;
    }

    function mkBin(op, a, b) {
      return function (env) { return op(a(env), b(env)); };
    }

    function parseExpr() {
      var node = parseTerm();
      while (peek()[0] === "+" || peek()[0] === "-") {
        var op = take()[0];
        var rhs = parseTerm();
        node = mkBin(op === "+" ? function (x, y) { return x + y; }
                                : function (x, y) { return x - y; }, node, rhs);
      }
      return node;
    }

    function parseTerm() {
      var node = parseFactor();
      while (peek()[0] === "*" || peek()[0] === "/") {
        var op = take()[0];
        var rhs = parseFactor();
        node = mkBin(op === "*" ? function (x, y) { return x * y; }
                                : function (x, y) { return y ? x / y : 0.0; }, node, rhs);
      }
      return node;
    }

    function parseFactor() {
      var t = peek();
      var kind = t[0], value = t[1];
      if (kind === "-") {
        take();
        var inner = parseFactor();
        return function (env) { return -inner(env); };
      }
      if (kind === "+") {
        take();
        return parseFactor();
      }
      if (kind === "num") {
        take();
        return function (env) { return value; };
      }
      if (kind === "var") {
        take();
        return function (env) {
          return env[value] === undefined ? 0.0 : Number(env[value]);   // 缺失按 0
        };
      }
      if (kind === "name") {
        take();
        var fname = String(value);
        if (!FUNCS[fname]) throw ExprError("未知函数 " + fname);
        take("(");
        var args = [parseExpr()];
        while (peek()[0] === ",") {
          take(",");
          args.push(parseExpr());
        }
        take(")");
        var fn = FUNCS[fname];
        return function (env) {
          return fn.apply(null, args.map(function (a) { return a(env); }));
        };
      }
      if (kind === "(") {
        take("(");
        var node = parseExpr();
        take(")");
        return node;
      }
      throw ExprError("表达式在 " + value + " 处无法解析");
    }

    var tree = parseExpr();
    if (pos !== tokens.length) {
      throw ExprError("表达式末尾有多余内容");
    }
    return tree;
  }

  var _cache = new Map();

  function compileExpr(text) {
    var fn = _cache.get(text);
    if (fn === undefined) {
      fn = parse(tokenize(text));
      _cache.set(text, fn);
    }
    return fn;
  }

  function evalExpr(text, env) {
    return compileExpr(text)(env);
  }

  function isExpr(value) {
    return typeof value === "string" && value.indexOf("$") >= 0;
  }

  function exprCheck(text) {
    compileExpr(text);
  }

  NFE.ExprError = ExprError;
  NFE.compileExpr = compileExpr;
  NFE.evalExpr = evalExpr;
  NFE.isExpr = isExpr;
  NFE.exprCheck = exprCheck;
})(typeof window !== "undefined" ? window : globalThis);
