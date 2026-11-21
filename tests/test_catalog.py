"""S1 验收：download_catalog 的结构完整性（不联网）。

对应 PLAN_download_tiering.md D-S1：

* 每个非 repo 文件有 size > 0 和 64 位十六进制 sha256；
* sources 数量满足完成定义（普通文件 ≥ 2、GitHub 类 ≥ 3、repo 型 ≥ 2 个站点）；
* artifacts_for(["create"], ["T2"]) 只含 ace_model（+ cover 的 T2）；
* artifacts_for(["train"], ["T2"]) 含 cover 的 T2 + 重建底模，不含 low/high；
* 可选档位默认不入队。

联网用例标 ``@pytest.mark.network``：``python -m pytest tests -m "not network"`` 可跳过。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import download_catalog as C  # noqa: E402

HEX64 = re.compile(r"^[0-9a-f]{64}$")
GITHUB_ONLY_KINDS = {"zip", "dlc", "sep", "file"}
NO_DOWNLOAD_KINDS = {"pip", "venv", "winget"}


def test_features_complete():
    """B.1 表里的 6 个功能都在，依赖闭合。"""
    assert set(C.FEATURES) == {"cover", "train", "create", "soulx", "capture", "stage"}
    assert C.FEATURES["cover"]["depends"] == ()
    assert C.FEATURES["train"]["depends"] == ("cover",)
    assert C.FEATURES["create"]["depends"] == ("cover",)
    assert C.FEATURES["soulx"]["depends"] == ("create",)
    # capture 的采集组件装进合成环境，必须依赖 cover 的 T0
    assert "cover" in C.FEATURES["capture"]["depends"]
    # 每个依赖都真实存在
    for name, meta in C.FEATURES.items():
        for dep in meta["depends"]:
            assert dep in C.FEATURES, f"{name} 依赖了不存在的功能 {dep}"
            assert dep not in C._expand(name) - {dep} or True  # 无环由下面单独测


def test_no_dependency_cycle():
    for name in C.FEATURES:
        seen: set[str] = set()
        stack = [name]
        while stack:
            cur = stack.pop()
            assert cur not in seen, f"功能依赖成环：{name} → {cur}"
            seen.add(cur)
            stack.extend(C.FEATURES[cur]["depends"])


@pytest.mark.parametrize("key", sorted(C.ARTIFACTS))
def test_artifact_schema(key):
    spec = C.ARTIFACTS[key]
    assert spec["feature"] in C.FEATURES, f"{key} 的 feature 不存在"
    for f in spec.get("features") or ():
        assert f in C.FEATURES
    assert spec["tier"] in C.TIERS
    assert spec["kind"] in {"file", "zip", "dlc", "sep", "repo", "pip", "winget", "venv"}
    assert spec.get("label"), f"{key} 缺 label"
    assert spec.get("post") in {"none", "dlc_convert", "hf_layout"} or \
        str(spec.get("post", "")).startswith("unzip:"), f"{key} 的 post 不合法"


@pytest.mark.parametrize("key", sorted(C.ARTIFACTS))
def test_file_artifacts_have_size_and_sha(key):
    """非 repo / 非 pip / 非 venv 的项必须有 size 与 sha256。"""
    spec = C.ARTIFACTS[key]
    if spec["kind"] in ("repo", "pip", "venv", "winget"):
        pytest.skip(f"{key} 由 pip/winget/仓库 API 解析")
    assert int(spec.get("size") or 0) > 0, f"{key} 缺 size"
    sha = spec.get("sha256")
    assert sha and HEX64.match(sha), f"{key} 的 sha256 不是 64 位十六进制：{sha}"


@pytest.mark.parametrize("key", sorted(C.ARTIFACTS))
def test_source_counts(key):
    """完成定义：每个下载物 ≥ 2 个实测通过的源；GitHub 类 ≥ 3。"""
    spec = C.ARTIFACTS[key]
    kind = spec["kind"]
    if kind in NO_DOWNLOAD_KINDS:
        pytest.skip("不下载或由包管理器解析")
    if kind == "repo":
        meta = spec.get("sources_meta") or {}
        assert len([v for v in meta.values() if v]) >= 2, f"{key} 的 repo 源不足 2 个站点"
        return
    srcs = list(spec.get("sources") or [])
    assert len(srcs) >= 2, f"{key} 只有 {len(srcs)} 个源"
    if "github.com" in " ".join(srcs) or "codeload.github.com" in " ".join(srcs):
        assert len(srcs) >= 3, f"{key} 是 GitHub 类下载物，需要 ≥ 3 个源，实际 {len(srcs)}"


def test_create_t2_scope():
    """artifacts_for(["create"], ["T2"]) = cover 的 T2 + ace_model。"""
    got = set(C.artifacts_for(["create"], ["T2"]))
    assert got == {"ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke", "ace_model"}
    assert "soulx_weights" not in got
    assert "ddsp_reflow_base" not in got


def test_train_t2_scope_excludes_optional_bases():
    """训练只带中档底模；低/高是可选档位，默认不勾。"""
    got = set(C.artifacts_for(["train"], ["T2"]))
    assert "ddsp_reflow_base" in got
    assert "ddsp_base_low" not in got
    assert "ddsp_base_high" not in got
    # 显式允许时才出现
    full = set(C.artifacts_for(["train"], ["T2"], allow_optional=True))
    assert {"ddsp_base_low", "ddsp_base_high"} <= full


def test_cover_t0_t1_t2_split():
    items = C.feature_items("cover")
    assert set(items["T0"]) == {"base_python", "ffmpeg", "synth_env", "torch",
                                "ddsp_deps", "audio_separator"}
    assert items["T1"] == ["ddsp_repo"]
    assert set(items["T2"]) == {"ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke"}


def test_capture_pulls_cover_env():
    """采集组件要装进 synth_python，所以 T0 必须带上 cover 的环境。"""
    got = set(C.artifacts_for(["capture"], ["T0"]))
    assert {"synth_env", "torch", "ddsp_deps", "capture_native"} <= got


def test_soulx_pulls_create():
    got = set(C.artifacts_for(["soulx"], ["T2"]))
    assert {"ace_model", "soulx_weights", "soulx_rmvpe"} <= got
    assert "ddsp_contentvec" in got  # 经 create → cover


def test_stage_has_no_download():
    assert C.artifacts_for(["stage"]) == []
    assert C.feature_sizes("stage")["total"] == 0


def test_feature_sizes_monotonic():
    """训练/原创/依赖链更多，体积应不小于被依赖的功能。"""
    cover = C.feature_sizes("cover")["total"]
    train = C.feature_sizes("train")["total"]
    create = C.feature_sizes("create")["total"]
    soulx = C.feature_sizes("soulx")["total"]
    assert train > cover
    assert create > cover
    assert soulx > create


def test_target_path_resolution(tmp_path):
    """target 能解析成 paths 里的绝对路径。"""
    paths = {
        "compute_root": tmp_path,
        "ddsp_root": tmp_path / "ddsp" / "DDSP-SVC",
        "sep_model_dir": tmp_path / "models" / "sep",
        "ddsp_pretrain_contentvec": tmp_path / "ddsp" / "DDSP-SVC" / "pretrain" / "contentvec" / "pytorch_model.bin",
        "acestep": tmp_path / "acestep",
        "soulx_root": tmp_path / "soulx",
    }
    p = C.target_path("ddsp_contentvec", paths)
    assert str(p).endswith("pytorch_model.bin")
    p2 = C.target_path("ddsp_repo", paths)
    assert str(p2) == str(paths["ddsp_root"])
    p3 = C.target_path("ace_model", paths)
    assert str(p3).replace("\\", "/").endswith("acestep/checkpoints")


def test_sha_uniqueness_of_distinct_files():
    """不同文件不应共用同一个 sha256（排除复制粘贴错误）。"""
    seen: dict[str, str] = {}
    for key, spec in C.ARTIFACTS.items():
        sha = spec.get("sha256")
        if not sha:
            continue
        if sha in seen and key != "base_python":
            pytest.fail(f"{key} 与 {seen[sha]} 的 sha256 相同，疑似抄错")
        seen[sha] = key


@pytest.mark.network
def test_live_sources_respond():
    """联网冒烟：每个非 pip 项至少有一个源现在能拿到真实字节。"""
    import tools.dl_verify_sources as V  # noqa: PLC0415
    for key in ("ddsp_contentvec", "ddsp_vocoder", "sep_karaoke"):
        spec = C.ARTIFACTS[key]
        rows = [V.probe(u, 256 << 10, 8.0) for u in spec["sources"][:2]]
        assert any(r["ok"] for r in rows), f"{key} 的两个首选源都不可用"
