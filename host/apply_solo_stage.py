# -*- coding: utf-8 -*-
"""独唱舞台 · 宿主加载器安装/检查/还原（T3-B）

宿主只注入一个加载器 <script>：
  /static/live2d/solo-stage-loader.js?v={{ static_asset_version }}-solo-loader1

安装时会移除旧的 3 个 canvas / GLSL / solo-stage.js 标签（它们由旧版
apply_solo_stage.py 注入），并移除 app-websocket.js 里旧的事件分发补丁——
T3-B 之后由 bridge.js 的 neko-solo-stage 事件统一驱动。旧行段在改前整文件备份
里完整保留，`--revert` 可逐字节还原。

用法：
  python -I apply_solo_stage.py --check
  python -I apply_solo_stage.py            # 安装（幂等）
  python -I apply_solo_stage.py --revert   # 用备份还原宿主文件
  python -I apply_solo_stage.py --host <resources/bin>
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

def _guess_host() -> Path:
    """在常见 Steam 库位置里找 N.E.K.O 的 resources/bin；都没有就用环境变量 NEKO_HOST_BIN，再不行要求 --host。"""
    import os
    env = os.environ.get("NEKO_HOST_BIN")
    if env:
        return Path(env)
    libs = (r"\Program Files (x86)\Steam\steamapps\common", r"\Program Files\Steam\steamapps\common",
            r"\SteamLibrary\steamapps\common", r"\Steam\steamapps\common")
    for drive in "CDEFGHIJ":
        for lib in libs:
            c = Path(drive + ":" + lib) / "n.e.k.o" / "resources" / "bin"
            if (c / "templates" / "index.html").is_file():
                return c
    return Path(".")


DEFAULT_HOST = _guess_host()
SRC_LOADER = Path(__file__).with_name("solo-stage-loader.js")
LOADER_REL = Path("static") / "live2d" / "solo-stage-loader.js"
BakExt = ".bak_stage_P3_T3B_20261115"

MARK_BEGIN = "<!-- NEKO_SOLO_LOADER:BEGIN"
MARK_END = "<!-- NEKO_SOLO_LOADER:END -->"
LOADER_TAG = ('<script src="/static/live2d/solo-stage-loader.js'
              '?v={{ static_asset_version }}-solo-loader102"></script>')
ANCHOR = "/static/live2d/live2d-init.js"

OLD_INDEX_BLOCKS = [
    ("<!-- NEKO_SOLO_STAGE:BEGIN", "<!-- NEKO_SOLO_STAGE:END -->"),
    ("<!-- NEKO_SOLO_JS:BEGIN", "<!-- NEKO_SOLO_JS:END -->"),
]
OLD_WS_BLOCKS = [
    ("// NEKO_SOLO_STAGE:BEGIN", "// NEKO_SOLO_STAGE:END"),
    ("// NEKO_SOLO_PLAY:BEGIN", "// NEKO_SOLO_PLAY:END"),
]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_text(p: Path) -> str:
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp_t3b")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    tmp.replace(p)


def backup_once(p: Path) -> Path:
    bak = p.with_name(p.name + BakExt)
    if not bak.exists():
        shutil.copy2(p, bak)
        print("[BAK]  " + str(bak))
    else:
        print("[SKIP] 备份已存在 " + str(bak))
    return bak


def remove_block(text: str, begin: str, end: str):
    i = text.find(begin)
    if i < 0:
        return text, False
    ls = text.rfind("\n", 0, i) + 1
    j = text.find(end, i)
    if j < 0:
        raise SystemExit("找不到结束标记: " + end)
    ee = text.find("\n", j)
    ee = len(text) if ee < 0 else ee + 1
    return text[:ls] + text[ee:], True


def insert_after_line(text: str, needle: str, block: str):
    i = text.find(needle)
    if i < 0:
        raise SystemExit("找不到插入锚点: " + needle)
    ee = text.find("\n", i)
    if ee < 0:
        return text + "\n" + block
    return text[: ee + 1] + block + text[ee + 1:]


def loader_block() -> str:
    return (MARK_BEGIN + " 独唱舞台加载器（勿手改；重打用 music_synth/tools/solo_stage/"
            "apply_solo_stage.py） -->\n    " + LOADER_TAG + "\n    " + MARK_END + "\n")


def check(host: Path) -> int:
    tpl = host / "templates" / "index.html"
    ws = host / "static" / "app" / "app-websocket.js"
    ok = True
    t = read_text(tpl)
    n = t.count(MARK_BEGIN)
    print(("[OK]   " if n == 1 else "[BAD]  ") + "index.html 加载器标记计数 = " + str(n))
    ok = ok and n == 1
    tagok = LOADER_TAG in t
    print(("[OK]   " if tagok else "[BAD]  ") + "index.html 加载器 tag 为当前版本 = " + str(tagok))
    ok = ok and tagok
    for b, e in OLD_INDEX_BLOCKS:
        present = (b in t) or (e in t)
        print(("[BAD]  " if present else "[OK]   ") + "index.html 旧标签段 " + b + " 存在=" + str(present))
        ok = ok and not present
    w = read_text(ws)
    for b, e in OLD_WS_BLOCKS:
        present = (b in w) or (e in w)
        print(("[BAD]  " if present else "[OK]   ") + "app-websocket.js 旧补丁 " + b + " 存在=" + str(present))
        ok = ok and not present
    dst = host / LOADER_REL
    exists = dst.exists()
    same = exists and SRC_LOADER.exists() and sha256(dst) == sha256(SRC_LOADER)
    print(("[OK]   " if same else "[BAD]  ") + "宿主加载器文件存在=" + str(exists) + " 与源一致=" + str(same))
    ok = ok and same
    print("\n结论: " + ("T3-B 加载器已就位" if ok else "未就位/有残留"))
    return 0 if ok else 1


def install(host: Path) -> int:
    tpl = host / "templates" / "index.html"
    ws = host / "static" / "app" / "app-websocket.js"
    if not tpl.exists():
        raise SystemExit("找不到 index.html: " + str(tpl))

    src_ok = SRC_LOADER.exists()
    if not src_ok:
        raise SystemExit("找不到加载器源文件: " + str(SRC_LOADER))

    # 1) 部署加载器文件
    dst = host / LOADER_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        backup_once(dst)
    shutil.copyfile(SRC_LOADER, dst)
    print("[CP]   " + str(SRC_LOADER) + " -> " + str(dst))

    # 2) index.html：备份 → 移除旧 tags → 插入唯一加载器
    backup_once(tpl)
    t = read_text(tpl)
    t0 = t
    for b, e in OLD_INDEX_BLOCKS:
        t, hit = remove_block(t, b, e)
        print(("[DEL]  " if hit else "[SKIP] ") + "index.html 旧标签段 " + b + " 移除=" + str(hit))
    if MARK_BEGIN in t:
        # 已装过：把旧的加载器 tag 换成当前版本号（避免浏览器用旧缓存脚本）
        import re as _re
        t2 = _re.sub(r'<script src="/static/live2d/solo-stage-loader\.js[^"]*"></script>', LOADER_TAG, t, count=1)
        if t2 != t:
            t = t2
            print("[UPD]  index.html 加载器版本号已更新")
        else:
            print("[SKIP] 加载器标记已存在")
    else:
        t = insert_after_line(t, ANCHOR, loader_block())
        print("[INS]  index.html 加载器标签")
    if t != t0:
        write_text(tpl, t)

    # 3) app-websocket.js：备份 → 移除旧事件分发补丁
    if ws.exists():
        backup_once(ws)
        w = read_text(ws)
        w0 = w
        for b, e in OLD_WS_BLOCKS:
            w, hit = remove_block(w, b, e)
            print(("[DEL]  " if hit else "[SKIP] ") + "app-websocket.js 旧补丁 " + b + " 移除=" + str(hit))
        if w != w0:
            write_text(ws, w)

    print("\n安装完成。")
    return check(host)


def revert(host: Path) -> int:
    tpl = host / "templates" / "index.html"
    ws = host / "static" / "app" / "app-websocket.js"
    done = 0
    for p in (tpl, ws):
        bak = p.with_name(p.name + BakExt)
        if bak.exists():
            shutil.copyfile(bak, p)
            print("[REV]  " + str(p) + " <- " + str(bak))
            done += 1
        else:
            print("[MISS] 无备份，未还原 " + str(p))
    print("\n还原完成（加载器文件保留，未删除；旧舞台标签随 index.html 备份恢复）。还原数=" + str(done))
    return 0 if done else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="独唱舞台宿主加载器安装/检查/还原（T3-B）")
    ap.add_argument("--host", default=str(DEFAULT_HOST), help="宿主 resources/bin")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()
    host = Path(a.host)
    if not host.is_dir():
        raise SystemExit("宿主目录不存在: " + str(host))
    if a.check:
        return check(host)
    if a.revert:
        return revert(host)
    return install(host)


if __name__ == "__main__":
    sys.exit(main())
