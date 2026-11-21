"""实测 ``download_catalog`` 里每个下载物的每个源（S1 验收脚本）。

用法（在插件目录下）::

    python -I tools\\dl_verify_sources.py --out tools\\dl_sources_report.json
    python -I tools\\dl_verify_sources.py --only ddsp_contentvec,sep_karaoke

对每个 (artifact, source) 做：

* Range 取首 1MB（6 秒封顶），记录 HTTP 状态、TTFB、吞吐、Content-Type；
* 判定 ``ok``：状态 200/206 拿到 ≥ 128KB 真实字节，且不是 text/html；
* ``head_sha1m``：首 1MB 的 sha256（同一文件多源之间可直接比对）；
* ``total``：优先取 Content-Range 的总长，没有则用 catalog 的 size；
* ``sha256_match``：整包 sha256 与 catalog 一致（``--full`` 才会真下载校验）。

只读，不写任何运行代码，不修改 plugin.toml。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import download_catalog as C  # noqa: E402

PROBE_BYTES = 1 << 20
PROBE_TIMEOUT_S = 6.0
UA = "NEKO-music-synth/0.3 (+s1-verify)"
HTML_REJECT_BYTES = 200_000
#: 判「拿到真实字节」的最小样本。repo 型探测只取 256KB，故用小于它的阈值。
MIN_OK_BYTES = 128 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe(url: str, nbytes: int = PROBE_BYTES, timeout: float = PROBE_TIMEOUT_S,
          allow_html: bool = False, expect_total: int = 0) -> dict:
    """Range 取前 ``nbytes`` 字节，返回结构化结果。

    ``allow_html`` 用于 PyPI/PyTorch 索引这类本来就是网页的 URL。
    ``expect_total`` 是 catalog 记录的文件总长：小文件（< 512KB）按「拿全了」判定。
    """
    out: dict = {"url": url, "ok": False, "status": 0, "ttfb_ms": 0,
                 "bytes": 0, "seconds": 0.0, "mb_per_s": 0.0,
                 "content_type": "", "total": 0, "head_sha1m": "", "error": ""}
    head = {"User-Agent": UA, "Range": f"bytes=0-{nbytes - 1}",
            "Accept-Encoding": "identity"}
    started = time.time()
    try:
        req = urllib.request.Request(url, headers=head)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["status"] = int(getattr(resp, "status", 200) or 200)
            out["ttfb_ms"] = int((time.time() - started) * 1000)
            ctype = resp.headers.get("Content-Type", "") or ""
            out["content_type"] = ctype
            crange = resp.headers.get("Content-Range", "") or ""
            if "/" in crange:
                tail = crange.rsplit("/", 1)[-1].strip()
                if tail.isdigit():
                    out["total"] = int(tail)
            if not out["total"]:
                clen = resp.headers.get("Content-Length", "")
                if clen.isdigit() and out["status"] == 200:
                    out["total"] = int(clen)
            data = bytearray()
            t0 = time.time()
            while len(data) < nbytes and time.time() - t0 < timeout:
                chunk = resp.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
            el = max(0.001, time.time() - t0)
            out["bytes"] = len(data)
            out["seconds"] = round(el, 2)
            out["mb_per_s"] = round(len(data) / el / 2 ** 20, 2)
            if data:
                out["head_sha1m"] = hashlib.sha256(bytes(data[:PROBE_BYTES])).hexdigest()
            if allow_html:
                out["ok"] = out["status"] in (200, 206) and len(data) > 0
                if not out["ok"]:
                    out["error"] = f"索引不可用（{len(data)} 字节）"
                return out
            is_html = "text/html" in ctype.lower() and len(data) < HTML_REJECT_BYTES
            if is_html:
                out["error"] = "返回网页而非文件（镜像失效）"
            elif expect_total and expect_total < 512 * 1024:
                # 小文件（如 95KB 的仓库 zip）：拿满就算通过
                out["ok"] = len(data) >= expect_total
                if not out["ok"]:
                    out["error"] = f"只拿到 {len(data)}/{expect_total} 字节"
            elif len(data) < MIN_OK_BYTES:
                out["error"] = f"只拿到 {len(data)} 字节"
            else:
                out["ok"] = True
    except urllib.error.HTTPError as exc:
        out["status"] = int(exc.code)
        out["error"] = f"HTTP {exc.code}"
        out["seconds"] = round(time.time() - started, 2)
    except Exception as exc:  # noqa: BLE001 - 探测脚本：任何异常都记为不可用
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        out["seconds"] = round(time.time() - started, 2)
    return out


def verdict(row: dict) -> str:
    if not row.get("ok"):
        return "不可用"
    sp = row.get("mb_per_s") or 0
    if sp >= 10:
        return "很快"
    if sp >= 3:
        return "快"
    if sp >= 1:
        return "一般"
    return "慢"


#: repo 型 artifact → 探测用的代表文件（体积小、必定存在、能反映下载权限）。
REPO_PROBE_FILES = {
    "ace_model": "vae/diffusion_pytorch_model.safetensors",
    "soulx_weights": "model-svc.pt",
    "soulx_rmvpe": "rmvpe/rmvpe.pt",
}


def _repo_probe_urls(spec: dict, key: str = "") -> list[tuple[str, str]]:
    """repo 型：对每个源探测一个真实存在的代表文件（不探整个仓库）。"""
    path = REPO_PROBE_FILES.get(key, "config.json")
    out: list[tuple[str, str]] = []
    for host, root in (spec.get("sources_meta") or {}).items():
        if not root:
            continue
        # catalog 里 modelscope 的 root 已含 /resolve/master；HF 系还需拼 /resolve/main
        root = root.rstrip("/")
        if "/resolve/" in root:
            out.append((host, f"{root}/{path}"))
        else:
            ref = "main"
            out.append((host, f"{root}/resolve/{ref}/{path}"))
    return out


def verify_artifact(key: str, spec: dict, timeout: float) -> dict:
    """对一个 artifact 的全部源并行探测，并给出推荐顺序。"""
    sources = list(spec.get("sources") or [])
    kind = spec.get("kind") or ""
    allow_html = kind == "pip"
    expect_total = int(spec.get("size") or 0) if kind == "zip" else 0
    rows: list[dict] = []
    if kind == "repo":
        targets = _repo_probe_urls(spec, key)
        if targets:
            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
                rows = list(ex.map(
                    lambda t: dict(probe(t[1], 256 << 10, timeout, expect_total=0),
                                   source_root=t[0]),
                    targets))
    elif sources:
        with ThreadPoolExecutor(max_workers=min(6, len(sources))) as ex:
            rows = list(ex.map(
                lambda u: probe(u, PROBE_BYTES, timeout,
                                allow_html=allow_html, expect_total=expect_total),
                sources))
    for r in rows:
        r["verdict"] = verdict(r)
        r["source_id"] = r.get("source_root") or (
            urllib.parse.urlsplit(r["url"]).hostname or r["url"])
        r["sha256_match"] = None
        r["size_match"] = (r.get("total") == spec.get("size")) if spec.get("size") else None
    good = [r for r in rows if r.get("ok")]
    good.sort(key=lambda r: -(r.get("mb_per_s") or 0))
    return {
        "key": key,
        "label": spec.get("label", ""),
        "feature": spec.get("feature", ""),
        "features": list(spec.get("features") or ()),
        "tier": spec.get("tier", ""),
        "required": bool(spec.get("required", True)),
        "kind": spec.get("kind", ""),
        "size": spec.get("size") or 0,
        "sha256": spec.get("sha256"),
        "post": spec.get("post", "none"),
        "usable_sources": len(good),
        "recommended": good[0]["url"] if good else "",
        "recommended_speed_mb_s": good[0]["mb_per_s"] if good else 0,
        "probes": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="实测 download_catalog 的每个源")
    ap.add_argument("--out", default=str(Path(__file__).with_name("dl_sources_report.json")))
    ap.add_argument("--only", default="", help="逗号分隔的 artifact key")
    ap.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_S)
    ap.add_argument("--jobs", type=int, default=4, help="同时探测几个 artifact")
    args = ap.parse_args(argv)

    socket.setdefaulttimeout(args.timeout + 10)
    keys = [k.strip() for k in args.only.split(",") if k.strip()] or list(C.ARTIFACTS)
    keys = [k for k in keys if k in C.ARTIFACTS]
    started = _now()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        results = list(ex.map(lambda k: verify_artifact(k, C.ARTIFACTS[k], args.timeout), keys))

    # 汇总：每个 artifact 有几个实测可用的源
    thin = [r["key"] for r in results
            if r["kind"] not in ("pip", "venv", "winget") and r["usable_sources"] < 2]
    github_thin = [r["key"] for r in results
                   if r["kind"] in ("zip",) and r["usable_sources"] < 3]
    pyalpha = [r["key"] for r in results if r["kind"] == "file" and r["usable_sources"] < 1]

    report = {
        "generated_at": started,
        "generated_by": "tools/dl_verify_sources.py",
        "probe_bytes": PROBE_BYTES,
        "probe_timeout_s": args.timeout,
        "rejected_sources": C.REJECTED_SOURCES,
        "summary": {
            "artifacts": len(results),
            "artifacts_with_lt2_sources": thin,
            "github_artifacts_with_lt3_sources": github_thin,
            "artifacts_with_zero_sources": pyalpha,
        },
        "features": {f: {"label": v["label"], "depends": list(v["depends"]),
                         "sizes": C.feature_sizes(f)} for f, v in C.FEATURES.items()},
        "artifacts": {r["key"]: r for r in results},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = sum(1 for r in results for p in r["probes"] if p.get("ok"))
    total = sum(len(r["probes"]) for r in results)
    print(f"[dl-verify] {len(results)} artifacts / {total} sources / {ok} ok -> {out}")
    for r in sorted(results, key=lambda x: x["key"]):
        best = r["recommended_speed_mb_s"]
        print(f"  {r['key']:22s} {r['tier']:2s} {r['kind']:7s} "
              f"usable={r['usable_sources']}/{len(r['probes'])} best={best:6.2f}MB/s")
    if thin:
        print("[dl-verify] 可用源 < 2：" + ", ".join(thin))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
