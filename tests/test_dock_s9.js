// S9 常驻下载条 DownloadDock 纯函数自测（node 直接运行）：python -m pytest tests/test_dock_s9.py 会调用本脚本。
// 从 static/index.html 提取 S9_PURE 块 eval，验证：轮询节流、文案拼装、eta、字节格式化、
// 状态→颜色/动作映射、完成 8 秒收起判定、失败保持。
"use strict";
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "static", "index.html"), "utf8");

function extractS9(src) {
  const m = src.match(/\/\* S9_PURE_BEGIN \*\/([\s\S]*?)\/\* S9_PURE_END \*\//);
  if (!m) throw new Error("找不到 S9_PURE 块");
  return m[1];
}

const S9 = new Function(extractS9(html) + "\nreturn S9;")();

let failures = 0;
function ok(name, cond, detail) {
  if (cond) {
    console.log("[PASS] " + name);
  } else {
    failures += 1;
    console.log("[FAIL] " + name + (detail ? "  <- " + detail : ""));
  }
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// 构造 state.json 结构（计划 B.2）
function mkState(items, totals) {
  return { version: 1, daemon: { pid: 1, running: true }, totals: totals || {}, items: items || {} };
}
function item(state, bytes_done, bytes_total, extra) {
  return Object.assign({
    key: "x", feature: "cover", tier: "T2", label: "模型", state, bytes_done, bytes_total,
    speed_bps: 0, source: "", source_id: "", switches: 0, error: ""
  }, extra || {});
}

// 1. 状态分类
ok("itemStates 统计 busy/done/failed/total", eq(
  S9.itemStates({
    a: item("downloading", 1, 10), b: item("done", 10, 10), c: item("failed", 0, 10),
    d: item("queued", 0, 10), e: item("paused", 5, 10), f: item("canceled", 0, 10)
  }),
  { active: 3, running: 1, queued: 1, paused: 1, done: 1, failed: 1, total: 6 }  // busy: downloading+queued+paused=3
));
ok("isBusy 判定", S9.isBusy("downloading") && S9.isBusy("paused") && S9.isBusy("paused_disk_full")
  && !S9.isBusy("done") && !S9.isBusy("failed") && !S9.isBusy("canceled"));

// 2. 字节/速度/eta 格式化
ok("fmtBytes", S9.fmtBytes(0) === "0 B" && S9.fmtBytes(500) === "500 B"
  && S9.fmtBytes(2048) === "2 KB" && S9.fmtBytes(1.2 * 1024 * 1024 * 1024) === "1.2 GB");
ok("fmtSpeed", S9.fmtSpeed(3.4 * 1024 * 1024) === "3.4 MB/s" && S9.fmtSpeed(820 * 1024) === "820 KB/s");
ok("fmtEta", S9.fmtEta(30) === "30 秒" && S9.fmtEta(1500) === "25 分钟" && S9.fmtEta(7200) === "2.0 小时" && S9.fmtEta(-1) === "—");

// 3. 源 host 提取
ok("hostOf 提取主机", S9.hostOf("https://hf-mirror.net/a/b") === "hf-mirror.net");
ok("sourceLabel 优先 source_id", S9.sourceLabel("https://hf-mirror.net/a/b", "hf-mirror.net") === "hf-mirror.net");
ok("sourceLabel 回退 host 并去 www", S9.sourceLabel("https://www.huggingface.co/x", "") === "huggingface.co");

// 4. summary + lineText 拼装
const sum = S9.summary(mkState(
  { a: item("downloading", 1 * 1024 * 1024 * 1024, 6 * 1024 * 1024 * 1024, { source: "https://hf-mirror.net/x", source_id: "hf-mirror.net" }), b: item("done", 0, 0) },
  { bytes_done: 1.2 * 1024 * 1024 * 1024, bytes_total: 6.4 * 1024 * 1024 * 1024, speed_bps: 3.4 * 1024 * 1024, eta_s: 1500 }
));
ok("summary 汇总", sum.active === 1 && sum.total === 2 && sum.sourceLabel === "hf-mirror.net");
ok("lineText 含 下载中/字节/速度/源/eta",
  /后台下载中/.test(S9.lineText(sum)) && /1\.2 GB\/6\.4 GB/.test(S9.lineText(sum))
  && /3\.4 MB\/s/.test(S9.lineText(sum)) && /hf-mirror\.net/.test(S9.lineText(sum)) && /约 25 分钟/.test(S9.lineText(sum)));
// S13 D-H：排队/暂停不算「下载中」
const qSum = S9.summary(mkState({ a: item("downloading", 1, 10), b: item("queued", 0, 10) }));
ok("1 下载中 + 1 排队 → 「后台下载中 1 · 排队 1 / 共 2」", S9.lineText(qSum).indexOf("后台下载中 1 · 排队 1 / 共 2") === 0, S9.lineText(qSum));
ok("不再出现「2/2」", S9.lineText(qSum).indexOf("2/2") < 0);
const pSum = S9.summary(mkState({ a: item("paused", 1, 10), b: item("queued", 0, 10), c: item("done", 10, 10) }));
ok("全暂停/排队时不显示下载中", S9.lineText(pSum).indexOf("排队 1 · 暂停 1 / 共 3") === 0 && S9.lineText(pSum).indexOf("下载中") < 0, S9.lineText(pSum));
ok("pollInterval 仍按 active 计（排队也快轮询）", S9.pollInterval(pSum) === 1500);
ok("lineText 全部完成", S9.lineText(S9.summary(mkState({ a: item("done", 1, 1) }))) === "全部下载完成");
ok("lineText 空", S9.lineText(S9.summary(mkState({}))) === "");
ok("lineText 失败项显示", S9.lineText(S9.summary(mkState({ a: item("failed", 0, 10) }))).indexOf("下载失败") >= 0);

// 5. 状态 → 展示
ok("stateMeta done/failed/paused", eq(S9.stateMeta("done"), { text: "完成", tone: "success" })
  && eq(S9.stateMeta("failed"), { text: "失败", tone: "error" })
  && eq(S9.stateMeta("paused_disk_full"), { text: "磁盘满·暂停", tone: "warning" }));

// 6. 单项动作映射
ok("itemActions 下载中→暂停+取消", eq(S9.itemActions(item("downloading", 1, 10)), { retry: false, pause: true, resume: false, cancel: true }));
ok("itemActions 暂停→继续", S9.itemActions(item("paused", 5, 10)).resume === true);
ok("itemActions 失败→重试", S9.itemActions(item("failed", 0, 10)).retry === true);
ok("itemActions 完成→无动作", eq(S9.itemActions(item("done", 10, 10)), { retry: false, pause: false, resume: false, cancel: false }));

// 7. 轮询节流
ok("pollInterval 活跃 1500ms", S9.pollInterval({ active: 1 }) === 1500);
ok("pollInterval 空闲 10000ms", S9.pollInterval({ active: 0 }) === 10000);

// 8. 进度百分比
ok("pct 按字节", Math.abs(S9.pct({ bytesDone: 2, bytesTotal: 8, total: 2, done: 0 }) - 25) < 1e-9);
ok("pct 按数量兜底", S9.pct({ bytesTotal: 0, total: 4, done: 2 }) === 50);

// 9. 完成 8 秒收起（逻辑判定）——完成态 only
const doneSum = S9.summary(mkState({ a: item("done", 1, 1) }));
const allDone = doneSum.total > 0 && doneSum.active === 0 && doneSum.failed === 0 && doneSum.done >= doneSum.total;
ok("完成态判定 allDone=true", allDone === true);
const busySum = S9.summary(mkState({ a: item("downloading", 1, 1) }));
ok("有活动时 allDone=false", !(busySum.total > 0 && busySum.active === 0 && busySum.failed === 0 && busySum.done >= busySum.total));
// 失败保持
const failedSum = S9.summary(mkState({ a: item("failed", 0, 1) }));
ok("失败项保持显示（hasError）", failedSum.hasError === true && failedSum.failed === 1);

// 10. sortedItems 排序：失败最前，其次下载中，done 最后
const sorted = S9.sortedItems({ d: item("done", 0, 0), f: item("failed", 0, 0), x: item("downloading", 0, 0) });
ok("sortedItems 失败在前", sorted[0][0] === "f" && sorted[2][0] === "d");

if (failures) {
  console.log("SOME FAILED: " + failures);
  process.exit(1);
}

// 11. 全 shell 脚本语法校验
function extractShell(src) {
  const lines = src.split(/\r?\n/);
  let s = -1, e = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].indexOf("<script data-shell>") >= 0) s = i;
    if (s >= 0 && lines[i].indexOf("</script>") >= 0) { e = i; break; }
  }
  const parts = [];
  for (let i = s; i < e; i++) {
    let l = lines[i];
    if (i === s) l = l.replace(/<script data-shell>/, "");
    parts.push(l);
  }
  return parts.join("\n");
}
try {
  new Function(extractShell(html));
  console.log("[PASS] 全 shell 脚本语法校验通过");
} catch (e) {
  console.log("[FAIL] 全 shell 脚本语法错误：" + e.message);
  process.exit(1);
}

console.log("ALL PASS");
