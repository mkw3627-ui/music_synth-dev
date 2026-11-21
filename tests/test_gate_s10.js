// S10 未安装门控 FeatureGate 纯函数自测（node 直接运行）：python -m pytest tests/test_gate_s10.py 会调用本脚本。
// 从 static/index.html 提取 S10_PURE 块 eval，验证：就绪判定、卡片形态状态机、体积文案、
// 分段进度汇总、页面×功能映射、inline 变体判定、T2 进度聚合。
"use strict";
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "static", "index.html"), "utf8");

function extractS10(src) {
  const m = src.match(/\/\* S10_PURE_BEGIN \*\/([\s\S]*?)\/\* S10_PURE_END \*\//);
  if (!m) throw new Error("找不到 S10_PURE 块");
  return m[1];
}
// S10.t2Keys 依赖 S8_FEATURE_ITEMS，提取 S8 块一起 eval
function extractS8(src) {
  const m = src.match(/\/\* S8_PURE_BEGIN \*\/([\s\S]*?)\/\* S8_PURE_END \*\//);
  if (!m) throw new Error("找不到 S8_PURE 块");
  return m[1];
}

const S10 = new Function(extractS8(html) + "\n" + extractS10(html) + "\nreturn S10;")();

let failures = 0;
function ok(name, cond, detail) {
  if (cond) { console.log("[PASS] " + name); }
  else { failures += 1; console.log("[FAIL] " + name + (detail ? "  <- " + detail : "")); }
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// 构造 ms_feature_status.features[f] 的字段
function feat(over) {
  return Object.assign({
    label: "原创歌曲", ready: false, t0_ready: false, t1_ready: false, t2_ready: false,
    missing: [], fg_gb: 0.3, bg_gb: 6.0, total_gb: 6.3, gpu_required: true,
    running: [], failed: [], downloads: {}
  }, over || {});
}

// 1. 页面 × 功能映射
ok("featureForPage sing→cover", S10.featureForPage("sing") === "cover");
ok("featureForPage materials→train", S10.featureForPage("materials") === "train");
ok("featureForPage train→train", S10.featureForPage("train") === "train");
ok("featureForPage create→create", S10.featureForPage("create") === "create");
ok("featureForPage 不门控页返回空", S10.featureForPage("start") === "" && S10.featureForPage("models") === "" && S10.featureForPage("settings") === "" && S10.featureForPage("stage") === "");
ok("isGatedPage", S10.isGatedPage("sing") && S10.isGatedPage("materials") && !S10.isGatedPage("start"));

// 2. 就绪判定 / 卡片形态状态机
ok("gateState ready=true → 不门控", S10.gateState("create", feat({ ready: true })).ready === true);
ok("gateState 下载中 → downloading", S10.gateState("create", feat({ running: ["ace_model"] })).downloading === true);
ok("gateState 失败 → failed", S10.gateState("create", feat({ failed: ["ace_model"] })).failed === true);
ok("gateState 未装 → notInstalled", S10.gateState("create", feat({})).notInstalled === true);
ok("gateState baseReady = t0&&t1", S10.gateState("create", feat({ t0_ready: true, t1_ready: true })).baseReady === true);
ok("cardShape 状态机", S10.cardShape(S10.gateState("create", feat({ ready: true }))) === "off"
  && S10.cardShape(S10.gateState("create", feat({ running: ["x"] }))) === "downloading"
  && S10.cardShape(S10.gateState("create", feat({ failed: ["x"] }))) === "failed"
  && S10.cardShape(S10.gateState("create", feat({}))) === "not-installed");

// 3. 体积文案
ok("needText 完整", S10.needText(S10.gateState("create", feat({}))) === "「原创歌曲」还没安装 · 需要下载约 6.3 GB（环境 0.3 GB + 模型 6 GB）");
ok("needText 无模型体积", S10.needText(S10.gateState("capture", feat({ label: "听歌采集", fg_gb: 0.02, bg_gb: 0, total_gb: 0.02 }))) === "「听歌采集」还没安装 · 需要下载约 0 GB（环境 0 GB）");
ok("gbStr 去尾 0", S10.gbStr(6.0) === "6" && S10.gbStr(0.3) === "0.3" && S10.gbStr(6.32) === "6.3");

// 4. T2 进度聚合
const tp = S10.t2Progress({ a: { state: "downloading", bytes_done: 100, bytes_total: 200 }, b: { state: "done", bytes_done: 300, bytes_total: 300 }, c: { state: "failed", bytes_done: 0, bytes_total: 50 } });
ok("t2Progress 聚合", tp.bytes_done === 400 && tp.bytes_total === 550 && tp.done === 1 && tp.failed === 1 && tp.running === 1 && tp.total === 3);

// 5. 分段进度
const segs = S10.tierSegments(S10.gateState("create", feat({ t0_ready: true, running: ["ace_model"] })), { installing: false, pct: 0 });
ok("tierSegments t0 done / t2 active", segs.t0 === "done" && segs.t2 === "active" && segs.t1 === "pending");
ok("tierSegments 前台安装中", S10.tierSegments(S10.gateState("create", feat({})), { installing: true, pct: 40 }).fgInstalling === true
  && S10.tierSegments(S10.gateState("create", feat({})), { installing: true, pct: 40 }).t0 === "active");
ok("segState", S10.segState(true, false) === "done" && S10.segState(false, true) === "active" && S10.segState(false, false) === "pending");
ok("segText active 带百分比", S10.segText("active", null, { bytes_done: 50, bytes_total: 100 }) === "下载中 50%");
ok("segTone", S10.segTone("done") === "success" && S10.segTone("active") === "warning" && S10.segTone("pending") === "neutral");

// 6. T2 key 列表（来自 S8_FEATURE_ITEMS）
ok("t2Keys cover 含模型不含程序", S10.t2Keys("cover").indexOf("ddsp_contentvec") >= 0 && S10.t2Keys("cover").indexOf("ddsp_repo") < 0);
ok("t2Keys create 含 ace_model", S10.t2Keys("create").indexOf("ace_model") >= 0);

// 7. 只装翻唱：sing/materials 不门控，train 仅 reflow 未就绪时门控（逻辑判定）
const coverReady = { cover: { ready: true, t0_ready: true, t1_ready: true, t2_ready: true, running: [], failed: [] } };
const trainNoReflow = { train: { ready: false, t0_ready: true, t1_ready: true, t2_ready: false, running: [], failed: [], fg_gb: 0, bg_gb: 0.21, total_gb: 0.21, label: "音色训练" } };
ok("cover 就绪 → sing 不门控", S10.gateState("cover", coverReady.cover).ready === true);
ok("train reflow 未就绪 → train 门控", S10.gateState("train", trainNoReflow.train).ready === false && S10.gateState("train", trainNoReflow.train).notInstalled === true);

if (failures) {
  console.log("SOME FAILED: " + failures);
  process.exit(1);
}

// 8. 全 shell 脚本语法校验
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
