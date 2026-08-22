/* NF：自编写微型前端框架（零依赖、零构建，见 AGENTS.md 1/2.3）。
 * 提供：
 *   NF.h(tag, attrs, ...children)  声明式创建 DOM；children 支持数组/文本/null，
 *                                  用户输入一律走文本节点，天然防注入；
 *   NF.clear(el)                    清空容器；
 *   NF.qs(sel)                      document.querySelector；
 *   NF.fetchJSON(url, options)      fetch 封装，非 2xx 时抛出带 code 的 Error。 */
(function () {
  "use strict";

  function append(el, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) {
      child.forEach(function (c) { append(el, c); });
      return;
    }
    if (child instanceof Node) {
      el.appendChild(child);
      return;
    }
    el.appendChild(document.createTextNode(String(child)));
  }

  function h(tag, attrs) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null) return;
      if (k === "class") {
        el.className = v;
      } else if (k === "style" && typeof v === "object") {
        Object.keys(v).forEach(function (prop) { el.style[prop] = v[prop]; });
      } else if (k === "dataset") {
        Object.keys(v).forEach(function (prop) { el.dataset[prop] = v[prop]; });
      } else if (k.slice(0, 2) === "on" && typeof v === "function") {
        el.addEventListener(k.slice(2).toLowerCase(), v);
      } else {
        el.setAttribute(k, v);
      }
    });
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }

  function clear(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function qs(sel) {
    return document.querySelector(sel);
  }

  function fetchJSON(url, options) {
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var code = data && data.error ? data.error : "http_" + res.status;
          var err = new Error(code);
          err.code = code;
          throw err;
        }
        return data;
      });
    });
  }

  window.NF = { h: h, clear: clear, qs: qs, fetchJSON: fetchJSON };
})();
