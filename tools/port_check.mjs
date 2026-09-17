/* 差分验证：JS 静态引擎（web/js/engine/*）vs Python 服务器逐字节对比。
   用法：node tools/port_check.mjs [--quick]
   - fighter：GET /api/fighter 与 NFE.createApi().fighter 深比较
   - battle：POST /api/battle 全量战报（含 rich / state / 文本）深比较
   - power：POST /api/power?count=30 胜场与结果比较
   任何数值 / 文本 / 结构差异都以 JSON 路径报出。供引擎改动后回归使用。 */
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve as pathResolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = pathResolve(dirname(fileURLToPath(import.meta.url)), "..");
const PORT = 18123;
const BASE = `http://127.0.0.1:${PORT}`;

const ENGINE_FILES = ["md5.js", "pyfmt.js", "rng.js", "text.js", "expr.js",
  "effects.js", "statuses.js", "config.js", "fighter.js", "battle.js", "api.js"];
const CONFIG_KEYS = ["system", "attributes", "skills", "titles", "battle", "ui"];

/* ---- 载入 JS 引擎到独立上下文 ---- */
function loadEngine() {
  const ctx = vm.createContext({ console });
  for (const f of ENGINE_FILES) {
    vm.runInContext(readFileSync(pathResolve(ROOT, "web/js/engine", f), "utf8"), ctx,
      { filename: f });
  }
  const data = {};
  for (const key of CONFIG_KEYS) {
    data[key] = JSON.parse(readFileSync(pathResolve(ROOT, "config/game", key + ".json"), "utf8"));
  }
  return vm.runInContext("NFE", ctx).createApi(data);
}

/* ---- 深比较：返回差异路径列表 ---- */
function diff(a, b, path, out) {
  if (typeof a === "number" && typeof b === "number") {
    if (a !== b) out.push(`${path}: ${a} !== ${b}`);
    return;
  }
  if (typeof a === "string" || typeof b === "string" || a === null || b === null ||
      typeof a === "boolean" || typeof b === "boolean" || a === undefined || b === undefined) {
    if (a !== b) out.push(`${path}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`);
    return;
  }
  const ka = Object.keys(a).sort();
  const kb = Object.keys(b).sort();
  if (ka.join(",") !== kb.join(",")) {
    const onlyA = ka.filter(k => !kb.includes(k));
    const onlyB = kb.filter(k => !ka.includes(k));
    out.push(`${path}: 键差异 only-in-a=${onlyA} only-in-b=${onlyB}`);
    return;
  }
  for (const k of ka) diff(a[k], b[k], `${path}.${k}`, out);
}

async function waitServer() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`${BASE}/api/health`);
      if (r.ok) return;
    } catch (e) { /* 未就绪 */ }
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error("Python 服务器启动超时");
}

async function getJSON(url) {
  const r = await fetch(url);
  return { status: r.status, body: await r.json() };
}
async function postJSON(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  return { status: r.status, body: await r.json() };
}

const NAMES = [
  "张三", "李四", "王五", "诸葛亮", "名字竞技场", "测试甲", "测试乙",
  "Alice", "Bob", "a", "Z9_x", "Player一号", "A B C",
  "café", "Ωμέγα", "日本語のなまえ", "中文English混合123",
  "😀大王", "𝕏𝕐𝕏", "🎮游戏人", "  张三  ", "ALICE", "alice",
  "深蓝之界的守望者", "阿", "0", "00", "123456789",
  "战神吕布方天画戟", "极强的名字", "最弱的名字", "名字名字名字名字名字"
];

function pairs() {
  const out = [];
  const pick = ["张三", "Alice", "😀大王", "𝕏𝕐𝕏", "café", "诸葛亮", "a", "Player一号"];
  for (let i = 0; i < pick.length; i++) {
    for (let j = i + 1; j < pick.length; j++) {
      out.push([pick[i], pick[j]]);
      out.push([pick[j], pick[i]]);           // 输入顺序无关性
    }
  }
  out.push(["张三", "张三"]);                  // 镜像对战
  out.push(["Alice", "alice"]);               // 大小写不同（case_sensitive）
  out.push(["名字竞技场", "名字角斗场"]);
  return out;
}

async function main() {
  const quick = process.argv.includes("--quick");
  const api = loadEngine();

  const proc = spawn("python", ["server.py", "--port", String(PORT)], { cwd: ROOT });
  let stderr = "";
  proc.stderr.on("data", d => { stderr += d; });
  try {
    await waitServer();
    let fails = 0, checks = 0;
    const problems = [];

    function report(label, diffs) {
      checks++;
      if (diffs.length) {
        fails++;
        problems.push(`[${label}] ${diffs.slice(0, 6).join(" | ")}`);
      }
    }

    // ---- fighter 差分 ----
    const names = quick ? NAMES.slice(0, 8) : NAMES;
    for (const name of names) {
      const py = await getJSON(`${BASE}/api/fighter?name=${encodeURIComponent(name)}`);
      let js = null;
      try { js = api.fighter(name); } catch (e) { js = { error: e.code }; }
      const d = [];
      diff(py.body, js, `fighter(${name})`, d);
      report(`fighter ${name}`, d);
    }

    // ---- battle 差分（全量战报） ----
    const prs = quick ? pairs().slice(0, 6) : pairs();
    for (const [a, b] of prs) {
      const py = await postJSON(`${BASE}/api/battle`, { a, b });
      let js = null;
      try { js = api.battle(a, b); } catch (e) { js = { error: e.code }; }
      const d = [];
      diff(py.body, js, `battle(${a} vs ${b})`, d);
      report(`battle ${a} vs ${b}`, d);
    }

    // ---- power 差分（小样本；elapsed_ms 为各自实测耗时，非语义，排除） ----
    if (!quick) {
      for (const name of ["张三", "Alice"]) {
        const py = await postJSON(`${BASE}/api/power`, { name, count: 40 });
        const js = await api.measurePower(name, 40);
        delete py.body.elapsed_ms;
        delete js.elapsed_ms;
        const d = [];
        diff(py.body, js, `power(${name})`, d);
        report(`power ${name}`, d);
      }
    }

    // ---- 错误路径 ----
    {
      const pyEmpty = await getJSON(`${BASE}/api/fighter?name=`);
      let jsCode = null;
      try { api.fighter(""); } catch (e) { jsCode = e.code; }
      const ok = pyEmpty.body.error === jsCode;
      checks++;
      if (!ok) { fails++; problems.push(`[error empty] py=${pyEmpty.body.error} js=${jsCode}`); }
      const longName = "超".repeat(33);
      const pyLong = await getJSON(`${BASE}/api/fighter?name=${encodeURIComponent(longName)}`);
      jsCode = null;
      try { api.fighter(longName); } catch (e) { jsCode = e.code; }
      checks++;
      if (pyLong.body.error !== jsCode) {
        fails++;
        problems.push(`[error long] py=${pyLong.body.error} js=${jsCode}`);
      }
    }

    console.log(`差分完成：${checks} 组检查，${fails} 组不一致`);
    for (const p of problems.slice(0, 20)) console.log("  ✗ " + p);
    process.exitCode = fails ? 1 : 0;
  } finally {
    proc.kill();
    if (stderr.trim()) console.error("[server stderr]\n" + stderr.trim().slice(0, 2000));
  }
}

main().catch(e => { console.error(e); process.exit(1); });
