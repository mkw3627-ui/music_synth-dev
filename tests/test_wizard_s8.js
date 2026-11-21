// S8 向导纯函数自测（node 直接运行）：python -m pytest tests/test_wizard_s8.py 会调用本脚本。
// 从 static/index.html 提取 S8_PURE 块并 eval，验证步数映射 / 功能展开 / 清单过滤 / 体积汇总 / 就绪判定。
"use strict";
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "static", "index.html"), "utf8");

function extractS8(src) {
  const m = src.match(/\/\* S8_PURE_BEGIN \*\/([\s\S]*?)\/\* S8_PURE_END \*\//);
  if (!m) throw new Error("找不到 S8_PURE 块");
  return m[1];
}

const S8 = new Function(extractS8(html) + "\nreturn S8;")();

let failures = 0;
function ok(name, cond, detail) {
  if (cond) {
    console.log("[PASS] " + name);
  } else {
    failures += 1;
    console.log("[FAIL] " + name + (detail ? "  <- " + detail : ""));
  }
}

function eq(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// 1. 步数映射：旧 5 步 → 新 6 步
ok("mapWizardStep 1/2/3 不变", eq([1, 2, 3].map(S8.mapWizardStep), [1, 2, 3]));
ok("mapWizardStep 旧「环境」4 → 5", S8.mapWizardStep(4) === 5);
ok("mapWizardStep 旧「开始」5 → 6", S8.mapWizardStep(5) === 6);
ok("mapWizardStep 6 保持不变", S8.mapWizardStep(6) === 6);
ok("mapWizardStep 非法值回退 1", S8.mapWizardStep(0) === 1 && S8.mapWizardStep("x") === 1);
ok("mapWizardStep 超界夹到 6", S8.mapWizardStep(99) === 6);

// 1b. S13 D-F：组合路径只映射一次（入口映射 + Onboarding 只夹范围）
function landOn(startAt) { return S8.clampWizardStep(startAt || 1); }
ok("?wizard=4 落到第 5 步", landOn(S8.wizardStepFromQuery("4")) === 5);
ok("?wizard=5 落到第 6 步", landOn(S8.wizardStepFromQuery("5")) === 6);
ok("?wizard=install 落到第 5 步", landOn(S8.wizardStepFromQuery("install")) === 5);
ok("?wizard=fail 落到第 5 步", landOn(S8.wizardStepFromQuery("fail")) === 5);
ok("?wizard=2 落到第 2 步", landOn(S8.wizardStepFromQuery("2")) === 2);
ok("未知/缺省 query 返回 0", S8.wizardStepFromQuery("x") === 0 && S8.wizardStepFromQuery("") === 0 && S8.wizardStepFromQuery("7") === 0);
ok("内部回落（基础未装好 + 已看过引导）传 5 → 落到第 5 步", landOn(5) === 5);
ok("无 startAt 落到第 1 步", landOn(undefined) === 1 && landOn(0) === 1);
ok("clampWizardStep 超界回 1", S8.clampWizardStep(9) === 1 && S8.clampWizardStep("x") === 1);

// 2. 功能展开（依赖自动带上）
ok("expandFeatures cover", eq(S8.expandFeatures(["cover"]), ["cover"]));
ok("expandFeatures train 带 cover", eq(S8.expandFeatures(["train"]), ["cover", "train"]));
ok("expandFeatures create 带 cover", eq(S8.expandFeatures(["create"]), ["cover", "create"]));
ok("expandFeatures soulx 带 create+cover", eq(S8.expandFeatures(["soulx"]), ["cover", "create", "soulx"]));
ok("expandFeatures capture 带 cover", eq(S8.expandFeatures(["capture"]), ["cover", "capture"]));
ok("expandFeatures 去重", eq(S8.expandFeatures(["cover", "train"]), ["cover", "train"]));
ok("expandFeatures 空", eq(S8.expandFeatures([]), []));

// 3. featureOptions：灰置 + 体积
const fs0 = {
  cover: { label: "翻唱", fg_gb: 1.2, bg_gb: 2.3, total_gb: 3.5 },
  create: { label: "原创歌曲", fg_gb: 0.3, bg_gb: 6.0, total_gb: 6.3 }
};
const withGpu = S8.featureOptions(fs0, true);
const noGpu = S8.featureOptions(fs0, false);
ok("featureOptions 有 GPU 时 create 不置灰",
  withGpu.find((f) => f.key === "create").disabled === false);
ok("featureOptions 无 GPU 时 create 置灰且有原因",
  noGpu.find((f) => f.key === "create").disabled === true
  && noGpu.find((f) => f.key === "create").disabled_reason.indexOf("NVIDIA") >= 0);
ok("featureOptions 无 GPU 时 cover 不置灰", noGpu.find((f) => f.key === "cover").disabled === false);
ok("featureOptions 默认勾选 cover/capture",
  withGpu.find((f) => f.key === "cover").default === true
  && withGpu.find((f) => f.key === "capture").default === true
  && withGpu.find((f) => f.key === "train").default === false);
// S13 D-H：依赖显示中文名，不露内部 id
ok("featureOptions 依赖显示中文名", eq(withGpu.find((f) => f.key === "train").depends_labels, ["翻唱"])
  && eq(withGpu.find((f) => f.key === "soulx").depends_labels, ["原创歌曲"])
  && eq(withGpu.find((f) => f.key === "train").depends, ["cover"]));
ok("featureOptions 体积透传", withGpu.find((f) => f.key === "cover").fg_gb === 1.2
  && withGpu.find((f) => f.key === "cover").bg_gb === 2.3);

// 4. 前台清单过滤（只 T0/T1，不含 T2）
const coverFg = S8.fgKeysFor(["cover"]);
ok("cover 前台含 base_python/synth_env/torch/ffmpeg/ddsp_repo",
  ["base_python", "synth_env", "torch", "ffmpeg", "ddsp_repo"].every((k) => coverFg.indexOf(k) >= 0));
ok("cover 前台不含 T2 模型", ["ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke"].every((k) => coverFg.indexOf(k) < 0));
const createFg = S8.fgKeysFor(["cover", "create"]);
ok("cover+create 前台含 ace_deps", createFg.indexOf("ace_deps") >= 0);
ok("create 前台不含 ace_model（T2）", createFg.indexOf("ace_model") < 0);
ok("train 前台 = cover 前台（train 无自有 T0/T1）",
  eq(S8.fgKeysFor(["train"]), coverFg));

// 5. 体积汇总
const fs2 = {
  cover: { fg_gb: 1.0, bg_gb: 2.0 },
  create: { fg_gb: 0.3, bg_gb: 6.0 }
};
ok("bgGbFor cover+create = 8.0", S8.bgGbFor(["cover", "create"], fs2) === 8.0);
ok("fgGbFor cover+create = 1.3", Math.abs(S8.fgGbFor(["cover", "create"], fs2) - 1.3) < 1e-9);
ok("bgGbFor train（=cover 的 T2）只算 cover", S8.bgGbFor(["train"], fs2) === 2.0);

// 6. 基础就绪判定
const ready = { cover: { t0_ready: true, t1_ready: true }, create: { t0_ready: true, t1_ready: true } };
const partial = { cover: { t0_ready: true, t1_ready: true }, create: { t0_ready: false, t1_ready: true } };
ok("baseCompletedFor 全就绪", S8.baseCompletedFor(["cover", "create"], ready) === true);
ok("baseCompletedFor 部分就绪为 false", S8.baseCompletedFor(["cover", "create"], partial) === false);
ok("baseCompletedFor 空选择为 false", S8.baseCompletedFor([], ready) === false);

if (failures) {
  console.log("SOME FAILED: " + failures);
  process.exit(1);
}

// 7. 全 shell 脚本语法校验（node 语法级，不执行 DOM）：提取 <script data-shell> 并 new Function 编译
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

