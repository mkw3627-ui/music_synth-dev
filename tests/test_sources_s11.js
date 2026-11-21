// S11 下载速度与来源设置 纯函数自测（node 直接运行）：python -m pytest tests/test_sources_s11.py 会调用本脚本。
// 从 static/index.html 提取 S11_PURE 块 eval，验证：来源排序、开关/禁用判定、推荐标注、
// 测速聚合、偏好映射、说明文案、速度格式化。
"use strict";
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "static", "index.html"), "utf8");

function extractS11(src) {
  const m = src.match(/\/\* S11_PURE_BEGIN \*\/([\s\S]*?)\/\* S11_PURE_END \*\//);
  if (!m) throw new Error("找不到 S11_PURE 块");
  return m[1];
}

const S11 = new Function(extractS11(html) + "\nreturn S11;")();

let failures = 0;
function ok(name, cond, detail) {
  if (cond) { console.log("[PASS] " + name); }
  else { failures += 1; console.log("[FAIL] " + name + (detail ? "  <- " + detail : "")); }
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// 1. host 提取与归一化
ok("hostOfUrl 提取主机并去 www", S11.hostOfUrl("https://www.huggingface.co/a/b") === "huggingface.co"
  && S11.hostOfUrl("https://mirrors.aliyun.com/pypi/simple/") === "mirrors.aliyun.com");
ok("hostOfUrl 空", S11.hostOfUrl("") === "");

// 2. 速度档与推荐
ok("rankHost ModelScope 最快", S11.rankHost("www.modelscope.cn") === 0);
ok("rankHost 阿里云/NJU 第二档", S11.rankHost("mirrors.aliyun.com") === 1 && S11.rankHost("mirror.nju.edu.cn") === 1);
ok("rankHost HF 官方兜底最慢", S11.rankHost("huggingface.co") === 5);
ok("rankHost 未知兜底 99", S11.rankHost("unknown.example.com") === 99);
ok("isRecommended 只推荐前三档", S11.isRecommended("www.modelscope.cn") && S11.isRecommended("mirrors.aliyun.com")
  && !S11.isRecommended("huggingface.co") && !S11.isRecommended("gh-proxy.com"));

// 3. 禁用判定
ok("isRejected 死源", S11.isRejected("hf-mirror.com") && S11.isRejected("ghfast.top")
  && S11.isRejected("gh-proxy.net") && !S11.isRejected("gh-proxy.com") && !S11.isRejected("ghproxy.net"));

// 4. 默认排序
const order = S11.defaultOrder();
ok("defaultOrder 首位是 ModelScope", order[0] === "modelscope.cn");
ok("defaultOrder 不含被禁源", order.every((h) => !S11.isRejected(h)));
ok("defaultOrder 含 HF 官方兜底", order.indexOf("huggingface.co") >= 0);
ok("defaultOrder 稳定（sortHosts）", eq(S11.sortHosts(["huggingface.co", "www.modelscope.cn", "mirrors.aliyun.com"]),
  ["www.modelscope.cn", "mirrors.aliyun.com", "huggingface.co"]));

// 5. 测速聚合
const items = {
  "sep_karaoke": [
    { url: "https://www.modelscope.cn/x", host: "www.modelscope.cn", ok: true, bps: 30e6 },
    { url: "https://huggingface.co/x", host: "huggingface.co", ok: true, bps: 0.5e6 }
  ],
  "ace_model": [
    { url: "https://www.modelscope.cn/y", host: "www.modelscope.cn", ok: true, bps: 12e6 },
    { url: "https://hf-mirror.com/y", host: "hf-mirror.com", ok: false, bps: 0 }
  ]
};
const agg = S11.aggregateByHost(items);
const ms = agg.filter((r) => r.host === "modelscope.cn")[0];
const hfc = agg.filter((r) => r.host === "hf-mirror.com")[0];
ok("aggregateByHost ModelScope 聚合", ms.ok === true && ms.okCount === 2 && ms.avgBps === 21e6 && ms.artifacts === 2);
ok("aggregateByHost 死源计为不可用", hfc.ok === false && hfc.failCount === 1);
ok("effectiveOrder 可用在前、死源在后", S11.effectiveOrder(agg)[0].host === "modelscope.cn"
  && S11.effectiveOrder(agg).filter((r) => r.host === "hf-mirror.com")[0].ok === false);

// 6. 偏好映射
ok("prefOptions 三项", S11.prefOptions().map((o) => o.value).join(",") === "auto,mirror,huggingface");
ok("prefLabel", S11.prefLabel("auto") === "自动（测速选最快）" && S11.prefLabel("mirror") === "国内源优先"
  && S11.prefLabel("huggingface") === "官方源优先" && S11.prefLabel("?") === "自动（测速选最快）");

// 7. 说明文案与速度格式化
ok("explainText 含校验哈希/末尾 64KB", S11.explainText().indexOf("sha256") >= 0 && S11.explainText().indexOf("64KB") >= 0);
ok("speedText", S11.speedText(3.4 * 1024 * 1024) === "3.4 MB/s" && S11.speedText(820 * 1024) === "820 KB/s" && S11.speedText(500) === "500 B/s");

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
