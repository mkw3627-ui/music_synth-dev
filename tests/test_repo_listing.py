"""S7 验收：repo 型文件清单的三级回落（对照 PLAN_download_tiering.md D-S7）。

三级顺序：
1. ModelScope ``/repo/files``（含 size + sha256）
2. HF tree API（``lfs.oid`` 即 sha256）
3. catalog 内置最小文件清单（``minimal_files``）

用注入的 ``fetch`` 模拟各种失败（500 / 空清单 / 网络异常），不依赖外网。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from importlib import import_module
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_WORKERS = str(PLUGIN_ROOT / "workers")
if _WORKERS not in sys.path:
    sys.path.insert(0, _WORKERS)

catalog = import_module("download_catalog")

DAEMON_PATH = PLUGIN_ROOT / "workers" / "download_daemon.py"


def _load_daemon():
    """按文件路径加载守护进程模块（它不在包内，且会自己改 sys.path）。"""
    spec = importlib.util.spec_from_file_location("_s7_daemon", DAEMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def daemon():
    return _load_daemon()


# ---------------------------------------------------------------------------
# ModelScope 响应样例
# ---------------------------------------------------------------------------
def _ms_payload(rows):
    return {"Code": 200, "Data": {"Files": [
        {"Path": rel, "Size": size, "Sha256": sha, "Type": "blob"}
        for rel, size, sha in rows]}}


def _hf_payload(rows):
    return [
        {"type": "file", "path": rel, "size": size, "oid": "deadbeef",
         "lfs": {"oid": sha, "size": size}}
        for rel, size, sha in rows]


class FetchScript:
    """按 URL 顺序返回预设结果；结果可以是 payload / 异常 / "empty"。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        if not self.results:
            raise AssertionError("fetch 被调用次数超出预期")
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# ---------------------------------------------------------------------------
# 第一级：ModelScope 成功
# ---------------------------------------------------------------------------
def test_level1_modelscope_ok(daemon, tmp_path):
    rows_in = [
        ("acestep-v15-turbo/model.safetensors", 4787825604, "3f6e" * 16),
        ("vae/diffusion_pytorch_model.safetensors", 337431388, "da17" * 16),
        (".gitattributes", 2961, "f9d3" * 16),      # 应被过滤
        ("README.md", 5496, "69d6" * 16),            # 应被过滤
    ]
    fetch = FetchScript([_ms_payload(rows_in)])
    rows = daemon.list_repo_files("ace_model", catalog.ARTIFACTS["ace_model"],
                                  tmp_path, fetch=fetch)
    rels = [r["rel"] for r in rows]
    assert "acestep-v15-turbo/model.safetensors" in rels
    assert "vae/diffusion_pytorch_model.safetensors" in rels
    assert ".gitattributes" not in rels and "README.md" not in rels
    assert len(fetch.calls) == 1, "第一级成功就不该请求后面的源"
    row = next(r for r in rows if r["rel"].endswith("model.safetensors")
               and r["rel"].startswith("acestep"))
    assert row["size"] == 4787825604
    assert row["sha256"] == "3f6e" * 16
    assert "modelscope.cn" in row["url"]


# ---------------------------------------------------------------------------
# 第二级：ModelScope 500 → HF tree
# ---------------------------------------------------------------------------
def test_level2_falls_back_to_hf(daemon, tmp_path):
    hf_rows = [
        ("acestep-v15-turbo/model.safetensors", 4787825604, "3f6e" * 16),
        ("vae/diffusion_pytorch_model.safetensors", 337431388, "da17" * 16),
    ]
    fetch = FetchScript([
        RuntimeError("HTTP 500"),          # ModelScope 挂
        _hf_payload(hf_rows),              # hf-mirror 成功
    ])
    rows = daemon.list_repo_files("ace_model", catalog.ARTIFACTS["ace_model"],
                                  tmp_path, fetch=fetch)
    assert len(rows) == 2
    # sha256 来自 lfs.oid
    row = next(r for r in rows if r["rel"].endswith("model.safetensors")
               and r["rel"].startswith("acestep"))
    assert row["sha256"] == "3f6e" * 16
    assert "hf-mirror.net" in row["url"]
    assert len(fetch.calls) == 2


def test_level2_skips_when_modelscope_returns_empty(daemon, tmp_path):
    """ModelScope 返回空清单（不是异常）也要继续往下走。"""
    hf_rows = [("model-svc.pt", 2793965154, "38e7" * 16)]
    fetch = FetchScript([_ms_payload([]), _hf_payload(hf_rows)])
    rows = daemon.list_repo_files("soulx_weights", catalog.ARTIFACTS["soulx_weights"],
                                  tmp_path, fetch=fetch)
    assert [r["rel"] for r in rows] == ["model-svc.pt"]


# ---------------------------------------------------------------------------
# 第三级：两个 API 都挂 → 内置最小清单
# ---------------------------------------------------------------------------
def test_level3_minimal_list_when_both_apis_fail(daemon, tmp_path):
    fetch = FetchScript([
        RuntimeError("HTTP 500"),
        RuntimeError("HTTP 503"),
        RuntimeError("timeout"),
        RuntimeError("timed out"),
    ])
    rows = daemon.list_repo_files("ace_model", catalog.ARTIFACTS["ace_model"],
                                  tmp_path, fetch=fetch)
    expected = catalog.ARTIFACTS["ace_model"]["minimal_files"]
    assert len(rows) == len(expected)
    rels = {r["rel"] for r in rows}
    # 最小清单必须覆盖主仓库的三个组件
    assert any(r.startswith("acestep-v15-turbo/") for r in rels)
    assert any(r.startswith("vae/") for r in rels)
    assert any(r.startswith("Qwen3-Embedding-0.6B/") for r in rels)
    # size 与 sha256 来自 catalog
    turbo = next(r for r in rows if r["rel"].endswith("acestep-v15-turbo/model.safetensors"))
    assert turbo["size"] == 4787825604
    assert turbo["sha256"].startswith("3f6e0797")


def test_level3_gives_both_urls_per_file(daemon, tmp_path):
    fetch = FetchScript([RuntimeError("500"), RuntimeError("503"),
                         RuntimeError("500"), RuntimeError("503")])
    rows = daemon.list_repo_files("soulx_rmvpe", catalog.ARTIFACTS["soulx_rmvpe"],
                                  tmp_path, fetch=fetch)
    assert len(rows) == 1
    urls = rows[0]["urls"]
    assert any("modelscope.cn" in u for u in urls)
    assert any("hf-mirror.net" in u for u in urls)
    assert rows[0]["sha256"] == \
        "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193"


def test_level3_error_when_no_minimal_list(daemon, tmp_path):
    """catalog 里没有 minimal_files 的 repo 型：三级都失败要抛清晰错误。"""
    spec = dict(catalog.ARTIFACTS["ace_model"])
    spec.pop("minimal_files", None)
    fetch = FetchScript([RuntimeError("500"), RuntimeError("503"),
                         RuntimeError("500"), RuntimeError("503")])
    with pytest.raises(RuntimeError) as excinfo:
        daemon.list_repo_files("ace_model", spec, tmp_path, fetch=fetch)
    assert "列文件失败" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def test_cache_hit_skips_fetch(daemon, tmp_path):
    cache_dir = tmp_path / "repo_index"
    cache_dir.mkdir(parents=True)
    cached = [{"rel": "acestep-v15-turbo/model.safetensors", "size": 5,
               "sha256": "ab" * 32, "url": "https://example/x"}]
    (cache_dir / "ace_model.json").write_text(
        json.dumps({"key": "ace_model", "source": "modelscope", "files": cached}),
        encoding="utf-8")

    def boom(url):
        raise AssertionError("有缓存时不应请求网络")

    rows = daemon.list_repo_files("ace_model", catalog.ARTIFACTS["ace_model"],
                                  tmp_path, fetch=boom)
    assert rows == cached


def test_success_is_cached(daemon, tmp_path):
    rows_in = [("vae/config.json", 425, "14e0" * 16)]
    fetch = FetchScript([_ms_payload(rows_in)])
    daemon.list_repo_files("ace_model", catalog.ARTIFACTS["ace_model"],
                           tmp_path, fetch=fetch)
    cache = tmp_path / "repo_index" / "ace_model.json"
    assert cache.is_file()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["source"] == "modelscope"
    assert data["files"][0]["rel"] == "vae/config.json"


def test_corrupt_cache_is_ignored(daemon, tmp_path):
    cache_dir = tmp_path / "repo_index"
    cache_dir.mkdir(parents=True)
    (cache_dir / "ace_model.json").write_text("{not json", encoding="utf-8")
    fetch = FetchScript([_ms_payload([("vae/config.json", 425, "14e0" * 16)])])
    rows = daemon.list_repo_files("ace_model", catalog.ARTIFACTS["ace_model"],
                                  tmp_path, fetch=fetch)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 与 plan_item 的衔接：urls 会带上备用源
# ---------------------------------------------------------------------------
def test_plan_item_uses_all_urls(daemon, tmp_path):
    item = {"key": "soulx_rmvpe",
            "paths": {"compute_root": str(tmp_path), "soulx_root": str(tmp_path / "soulx")}}

    # 先写一份缓存，避免真的联网
    cache_dir = tmp_path / "_downloads" / "repo_index"
    cache_dir.mkdir(parents=True)
    (cache_dir / "soulx_rmvpe.json").write_text(json.dumps({
        "key": "soulx_rmvpe", "source": "minimal",
        "files": daemon._minimal_repo_rows(catalog.ARTIFACTS["soulx_rmvpe"]),
    }), encoding="utf-8")

    plans = daemon.plan_item(item, tmp_path / "_downloads")
    assert len(plans) == 1
    plan = plans[0]
    assert plan["rel"] == "rmvpe/rmvpe.pt"
    assert plan["dest"].replace("\\", "/").endswith("preprocess/rmvpe/rmvpe.pt")
    assert any("modelscope.cn" in u for u in plan["urls"])
    assert any("hf-mirror.net" in u for u in plan["urls"])
    assert plan["sha256"].startswith("6d62215f")


def test_plan_item_component_subset(daemon, tmp_path):
    """ACE 只下 components 里的三个组件，不整仓 9.4GB。"""
    spec = catalog.ARTIFACTS["ace_model"]
    rows = daemon._minimal_repo_rows(spec)
    tops = {r["rel"].split("/", 1)[0] for r in rows}
    assert tops == set(spec["components"])


def test_all_repo_entries_have_minimal_files():
    """三个 repo 型都必须有内置最小清单（第三级回落的前提）。"""
    for key in ("ace_model", "soulx_weights", "soulx_rmvpe"):
        spec = catalog.ARTIFACTS[key]
        assert spec["kind"] == "repo"
        files = spec.get("minimal_files")
        assert files, f"{key} 缺 minimal_files"
        for entry in files:
            assert len(entry) == 3, entry
            rel, size, sha = entry
            assert size > 0
            assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
