"""官方歌词获取：标题 + 歌手 → 带时间戳的官方 LRC。

fix9 任务 A：给 ``track["official_lyrics"]`` 填上真实歌词，替换 ASR 识别产物
（识别错词 + 合唱被去重删字），让 pipeline 的「官方歌词分支」与
guided_tune 的「official 不去重」分支真正生效。

合规边界（硬约束）：
  - 不内嵌任何 API key / 登录态 / 凭据。
  - 只调用「公开、无需登录」的歌词查询接口，仅取歌词文本（不下载音频、
    不绕过付费、不批量抓取）。网易云 ``music.163.com/api`` 是社区公认的
    公开接口（NeteaseCloudMusicApi 开源项目同源），此处只用其搜索 + 歌词文本。
  - 所有来源可开关、可回滚；每个来源的成败都写进返回的 reason/source，
    供 pipeline 记日志。拉不到就优雅降级，绝不伪造歌词。

来源优先级：
  1. 本地 .lrc（source 同 stem）—— 用户/系统已有，最可信
  2. 网易云公开接口 —— 覆盖中文/日文/主流歌曲（含 B站歌）
  3. lrclib.net —— 开源公开 LRC 库（英文歌覆盖较好）
全部失败 -> (None, reason)，调用方降级 ASR 并标注「参考歌词不可信」。
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

# 制作人员 / 元数据行关键词（清洗时剔除，它们不是歌词）
_META_PREFIXES = (
    "作词", "作曲", "编曲", "编", "混", "母", "策划", "监制", "出品",
    "图绘", "影像", "本家", "v本家", "pv", "PV", "吉他", "键盘", "鼓",
    "和声", "调校", "调教", "扒谱", "演唱", "翻唱",
)

# 歌手切换标记（清洗时剔除；「合」单独处理见 _clean_lrc）
_DUET_MARKS = {"合", "合唱", "合唱部分"}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def env_flag(name: str, default: bool = True) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if val == "":
        return default
    return val in ("1", "true", "on", "yes")


def _http_json(url: str, data: bytes | None = None, timeout: float = 15.0):
    headers = {"User-Agent": _UA, "Referer": "https://music.163.com"}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _core_title(title: str) -> str:
    """去掉「【星火行动】」「（泠鸢&双笙）」这类前缀/后缀，得到核心标题。"""
    t = re.sub(r"[【\[].*?[】\]]", "", str(title or ""))
    t = re.sub(r"[（(].*?[)）]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _artist_parts(artist: str) -> set[str]:
    """歌手拆成若干可匹配的片段：双笙（陈元汐）/泠鸢yousa -> {双笙, 陈元汐, 泠鸢, yousa}。"""
    parts: set[str] = set()
    for seg in re.split(r"[/&、,，|]", str(artist or "")):
        seg = seg.strip()
        if not seg:
            continue
        core = re.sub(r"[（(].*?[)）]", "", seg).strip()
        for tok in (seg, core):
            if not tok:
                continue
            parts.add(tok)
            # 中文/英文片段拆开：泠鸢yousa -> 泠鸢 + yousa（歌手标记行用的是中文名）
            for sub in re.findall(r"[一-鿿]+|[a-zA-Z]+", tok):
                parts.add(sub)
    return {p for p in parts if p}


def _netease_search(title: str, artist: str, timeout: float) -> str | None:
    """网易云搜索 → 最匹配的 song id（title+artist 双校验）。"""
    q = urllib.parse.quote(f"{_core_title(title)} {artist}".strip())
    url = f"https://music.163.com/api/search/get?s={q}&type=1&limit=8&offset=0"
    try:
        data = _http_json(url, timeout=timeout)
    except Exception:
        return None
    songs = ((data.get("result") or {}).get("songs")) or []
    if not songs:
        return None
    core = _core_title(title)
    artist_set = _artist_parts(artist)
    for s in songs:
        name = str(s.get("name") or "")
        # 名字必须含核心标题
        if core and core not in name and name not in core:
            continue
        if artist_set:
            song_artists = {str(a.get("name") or "") for a in (s.get("artists") or [])}
            song_artists |= {re.sub(r"[（(].*?[)）]", "", a).strip()
                             for a in song_artists}
            if not (artist_set & song_artists):
                continue
        return str(s.get("id"))
    # 放宽：核心标题匹配不到就取第一个名字含核心标题的
    for s in songs:
        if core and core in str(s.get("name") or ""):
            return str(s.get("id"))
    return None


def _netease_lrc(song_id: str, timeout: float) -> str | None:
    url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
    try:
        data = _http_json(url, timeout=timeout)
    except Exception:
        return None
    lrc = ((data.get("lrc") or {}).get("lyric")) or ""
    return lrc.strip() or None


def _netease_search_by_lyric(lyric_text: str, timeout: float, limit: int = 6) -> "list[dict]":
    """网易云「歌词搜索」(type=1006)：未知标题时用 ASR 歌词片段找歌。

    返回 [{id, name, artists}]，按接口原序。ASR 文本只用于检索，命中后
    取官方 LRC，绝不把 ASR 文本当歌词。
    """
    plain = re.sub(r"\[[^\]]*\]", " ", str(lyric_text or ""))
    han = re.findall(r"[\u3400-\u9fff]+", plain)
    # 跳过开头 1 段（常是上一句残尾/幽灵字），取接下来最长的几段
    picks = [h for h in han[1:] if len(h) >= 3][:3] or [h for h in han if len(h) >= 2][:3]
    if not picks:
        return []
    q = urllib.parse.quote(" ".join(picks))
    url = f"https://music.163.com/api/search/get?s={q}&type=1006&limit={limit}&offset=0"
    try:
        data = _http_json(url, timeout=timeout)
    except Exception:
        return []
    out = []
    for s in ((data.get("result") or {}).get("songs")) or []:
        out.append({"id": str(s.get("id")), "name": str(s.get("name") or ""),
                    "artists": [str(a.get("name") or "") for a in (s.get("artists") or [])]})
    return out


def _drop_name_lines(lrc: str, names: "list[str]") -> str:
    """去掉只含歌手名的行（合唱曲 LRC 的「泠鸢」「双笙」分段标记）。"""
    parts = set()
    for n in names or []:
        parts |= _artist_parts(n)
        parts |= {x.strip() for x in re.split(r"[&/、,，\s]+", n) if x.strip()}
    keep = []
    for line in str(lrc or "").splitlines():
        body = re.sub(r"^(\[\d{1,2}:\d{2}[.:]\d{1,3}\])+", "", line).strip()
        if body in parts:
            continue
        keep.append(line)
    return "\n".join(keep)


def fetch_lrc_by_lyric(lyric_text: str, timeout: float = 15.0) -> "tuple[str | None, str, list[dict]]":
    """无标题/标题无效时的兜底：歌词片段 → 候选歌 → 官方 LRC。

    返回 (lrc, source, candidates)。candidates 每项带清洗后的 lrc，调用方
    可按「时间戳与本录音发声起点的吻合度」选最佳一首（同曲不同版本歌词
    分行不同）。
    """
    if not env_flag("MUSIC_SYNTH_LYRICS_FETCH", True) or not env_flag(
            "MUSIC_SYNTH_LYRICS_ALLOW_NETEASE", True):
        return None, "disabled", []
    cands = []
    for s in _netease_search_by_lyric(lyric_text, timeout):
        lrc = _netease_lrc(s["id"], timeout)
        if not lrc:
            continue
        cleaned = _drop_name_lines(clean_lrc(lrc, " ".join(s["artists"])),
                                   s["artists"] + [s["name"]])
        if cleaned:
            cands.append(dict(s, lrc=cleaned))
    if not cands:
        return None, "no_lyric_match", []
    return cands[0]["lrc"], f"netease_lyric:{cands[0]['id']}", cands


def _lrclib_search(title: str, artist: str, timeout: float) -> str | None:
    """lrclib.net（开源公开 LRC 库）→ 带时间戳 LRC。"""
    q = urllib.parse.quote(f"{_core_title(title)} {artist}".strip())
    try:
        data = _http_json(f"https://lrclib.net/api/search?q={q}", timeout=timeout)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    synced = (data[0].get("syncedLyrics")) or ""
    return synced.strip() or None


def clean_lrc(lrc_text: str, artist: str = "") -> str:
    """清洗 LRC：去元数据/制作人员/歌手标记行，只留 ``[时间戳] 歌词``。

    保留带时间戳的行（后续 B1 句级对齐要用时间戳），只是去掉非歌词内容。
    """
    artist_parts = _artist_parts(artist)
    out: list[str] = []
    for line in str(lrc_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # 非时间戳元数据行（[ti:] [ar:] [offset:] 等）直接丢
        if not re.match(r"^\[\d{1,2}:\d{2}[.:]\d{1,3}\]", line):
            continue
        # 去掉时间戳，取歌词文本（可能有多个时间戳）
        text = re.sub(r"^(\[\d{1,2}:\d{2}[.:]\d{1,3}\])+", "", line).strip()
        if not text:
            continue
        # 制作人员行：文本以「作词/作曲/…」开头
        if any(text.startswith(p) for p in _META_PREFIXES):
            continue
        # 歌手切换标记：文本是「合」「合唱」或等于某个歌手名片段
        if text in _DUET_MARKS or text in artist_parts:
            continue
        out.append(line)
    return "\n".join(out)


def fetch_official_lrc(title: str, artist: str, source_path: str | None = None,
                       timeout: float = 15.0) -> tuple[str | None, str]:
    """拉官方带时间戳 LRC。返回 (lrc_text, source) 或 (None, reason)。

    lrc_text 为清洗后、保留 ``[时间戳] 歌词`` 的 LRC；source/reason 描述成败，
    供 pipeline 记日志。
    """
    if not env_flag("MUSIC_SYNTH_LYRICS_FETCH", True):
        return None, "disabled"

    # 1) 本地 .lrc
    src = Path(str(source_path or ""))
    if src.name:
        for suffix in (".lrc", ".txt"):
            cand = src.with_suffix(suffix)
            if cand.is_file():
                try:
                    body = cand.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                cleaned = clean_lrc(body, artist)
                if cleaned:
                    return cleaned, f"local:{cand.name}"

    # 2) 网易云公开接口
    if env_flag("MUSIC_SYNTH_LYRICS_ALLOW_NETEASE", True):
        try:
            sid = _netease_search(title, artist, timeout)
            if sid:
                lrc = _netease_lrc(sid, timeout)
                if lrc:
                    cleaned = clean_lrc(lrc, artist)
                    if cleaned:
                        return cleaned, f"netease:{sid}"
        except Exception:  # noqa: BLE001
            pass  # 降级到下一来源，不抛

    # 3) lrclib.net
    try:
        lrc = _lrclib_search(title, artist, timeout)
        if lrc:
            cleaned = clean_lrc(lrc, artist)
            if cleaned:
                return cleaned, "lrclib.net"
    except Exception:  # noqa: BLE001
        pass

    return None, "no_source"
