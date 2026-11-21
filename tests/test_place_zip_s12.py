"""S12a 修复回归：zip 放置遇到「目标已存在」时不能抛 WinError 183。

真机实测：``ddsp_rmvpe`` 后处理失败 —— rmvpe.zip 解出 ``rmvpe/model.pt``，
目标目录里已有同名文件时 ``shutil.move`` 抛
``FileExistsError: [WinError 183] 当文件已存在时，无法创建该文件``。
"""
from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PLUGIN_ROOT), str(PLUGIN_ROOT / "workers")):
    if p not in sys.path:
        sys.path.insert(0, p)

DAEMON = PLUGIN_ROOT / "workers" / "download_daemon.py"


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location("_s12_zip_daemon", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _zip_with(tmp_path: Path, layout: dict[str, bytes]) -> Path:
    path = tmp_path / "payload.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in layout.items():
            zf.writestr(name, data)
    return path


def test_into_base_overwrites_existing_file(daemon, tmp_path):
    """rmvpe 场景：目标 model.pt 已存在时不报错，内容被覆盖。"""
    archive = _zip_with(tmp_path, {"rmvpe/model.pt": b"NEW-CONTENT"})
    dest = tmp_path / "pretrain" / "rmvpe"
    dest.mkdir(parents=True)
    (dest / "model.pt").write_bytes(b"OLD")          # 已存在

    daemon.place_zip("into_base", archive, dest)
    assert (dest / "model.pt").read_bytes() == b"NEW-CONTENT"


def test_into_base_overwrites_existing_dir(daemon, tmp_path):
    """目标里已有同名文件/目录时覆盖，而不是 FileExistsError。

    ``into_base`` 的语义是「单顶层目录时铺其**内容**」——所以 zip 里
    ``rmvpe/model.pt`` 落到 ``dest/model.pt``，与 setup._install_zip 一致
    （rmvpe 的 target 本身就是 ``pretrain/rmvpe``）。
    """
    archive = _zip_with(tmp_path, {"rmvpe/model.pt": b"NEW", "rmvpe/cfg.json": b"{}"})
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "model.pt").write_bytes(b"OLD")
    (dest / "cfg.json").mkdir()                       # 同名目录也要能覆盖

    daemon.place_zip("into_base", archive, dest)
    assert (dest / "model.pt").read_bytes() == b"NEW"
    assert (dest / "cfg.json").read_bytes() == b"{}"


def test_flatten_overwrites(daemon, tmp_path):
    """flatten（vocoder）：目标已有 model / config.json 时覆盖。"""
    archive = _zip_with(tmp_path, {"top/model.ckpt": b"M", "top/config.json": b"C"})
    dest = tmp_path / "vocoder"
    dest.mkdir()
    (dest / "config.json").write_bytes(b"OLD")

    daemon.place_zip("flatten", archive, dest, rename={"model.ckpt": "model"})
    assert (dest / "model").read_bytes() == b"M"
    assert (dest / "config.json").read_bytes() == b"C"


def test_repo_root_flattens_single_top(daemon, tmp_path):
    archive = _zip_with(tmp_path, {"DDSP-SVC-abc/main_reflow.py": b"#code"})
    dest = tmp_path / "repo"
    daemon.place_zip("repo_root", archive, dest)
    assert (dest / "main_reflow.py").is_file()


def test_ffmpeg_bin_mode(daemon, tmp_path):
    archive = _zip_with(tmp_path, {
        "ffmpeg-9.0/bin/ffmpeg.exe": b"exe",
        "ffmpeg-9.0/bin/ffprobe.exe": b"probe",
        "ffmpeg-9.0/README.txt": b"readme",
    })
    dest = tmp_path / "ffbin"
    daemon.place_zip("ffmpeg_bin", archive, dest)
    assert (dest / "ffmpeg.exe").is_file()
    assert not (dest / "README.txt").exists()


def test_twice_in_a_row_is_idempotent(daemon, tmp_path):
    """重复放置同一个包（重下/重试）不能失败。"""
    archive = _zip_with(tmp_path, {"rmvpe/model.pt": b"V1"})
    dest = tmp_path / "twice"
    daemon.place_zip("into_base", archive, dest)
    assert (dest / "model.pt").read_bytes() == b"V1"
    # 再放一次（模拟同一 item 重试）
    daemon.place_zip("into_base", archive, dest)
    assert (dest / "model.pt").read_bytes() == b"V1"


def test_copy_over_fallback(daemon, tmp_path):
    """move 失败时的拷贝回退能工作。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_bytes(b"A")
    sub = src / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"B")
    dst = tmp_path / "dst"
    daemon._copy_over(src, dst)
    assert (dst / "a").read_bytes() == b"A"
    assert (dst / "sub" / "b").read_bytes() == b"B"


# ---------------------------------------------------------------------------
# 真机实测的第二个解压 bug：catalog 的 target 是「文件」而不是「目录」
# ---------------------------------------------------------------------------
def test_zip_dest_dir_for_file_target(daemon):
    r"""ddsp_rmvpe 的 target 是 ...\rmvpe\model.pt，解压目录应是 ...\rmvpe。"""
    target = Path("E:/x/ddsp/DDSP-SVC/pretrain/rmvpe/model.pt")
    assert daemon._zip_dest_dir(target, "ddsp_rmvpe") == \
        Path("E:/x/ddsp/DDSP-SVC/pretrain/rmvpe")


def test_zip_dest_dir_for_dir_target(daemon):
    """ddsp_vocoder 的 target 无扩展名，本身就是目录。"""
    target = Path("E:/x/ddsp/DDSP-SVC/pretrain/nsf_hifigan")
    assert daemon._zip_dest_dir(target, "ddsp_vocoder") == target


def test_zip_dest_dir_only_treats_known_extensions_as_files(daemon):
    """只有 .pt/.bin/.ckpt/.zip 才当文件；带点号的目录名不动。"""
    target = Path("E:/x/some.dir.v2")
    assert daemon._zip_dest_dir(target, "x") == target


def test_run_post_unzip_uses_directory(daemon, tmp_path, monkeypatch):
    """端到端：run_post 的 unzip 分支把包解到目录，而不是尝试解到文件路径。"""
    # 造一个真实的 rmvpe 形态：zip 里只有 model.pt
    archive = _zip_with(tmp_path, {"model.pt": b"NEW-MODEL"})
    staged = tmp_path / "staging"
    staged.mkdir()
    moved = staged / archive.name
    moved.write_bytes(archive.read_bytes())

    dest_file = tmp_path / "pretrain" / "rmvpe" / "model.pt"
    dest_file.parent.mkdir(parents=True)
    dest_file.write_bytes(b"OLD-MODEL")

    item = {
        "key": "ddsp_rmvpe",
        "paths": {"compute_root": str(tmp_path),
                  "ddsp_root": str(tmp_path / "ddsp"),
                  "ddsp_pretrain_rmvpe": str(dest_file)},
        "files": [{"rel": moved.name, "dest": str(moved), "state": "done"}],
    }
    state, err = daemon.run_post("ddsp_rmvpe", item, tmp_path)
    assert state == "done", err
    assert dest_file.read_bytes() == b"NEW-MODEL"
