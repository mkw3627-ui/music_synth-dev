"""逐字口型：歌词 + 时长 → 时间轴上的口型轨（为她而唱）。

宿主是冻结的 Python（没有 numpy / pypinyin），所以这个模块只做两件事：

1. 查 ``data/lips_pinyin.json``（离线生成的「汉字 → 拼音 → 口型」表）；
2. 把歌词按行、按字铺到时间轴上，产出一串 ``[字, 口型, 起, 止]``。

口型是按韵母归的（a/ia/uan… → A，o/uo/ong… → O，e/ie/ei… → E，i/in… → I，
u/ü… → U），所以「唱哪个字」决定「嘴张成什么形状」——这是和语言对齐的部分。
时间轴没有音素级对齐：优先用 ACE 给的 LRC（当前 vendored 版本不返回对齐张量，
所以基本走不到），否则按字数把整首歌的演唱段铺满。目标是「大概对得上」。
"""
from __future__ import annotations

import bisect
import json
import re
import struct
import wave
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
TABLE_PATH = DATA_DIR / "lips_pinyin.json"

# 标点/空白：不占口型位，但它们的时长仍算在整行里。
_PUNCT = re.compile(r"[\s，。！？、,.!?;；:：'\"“”‘’—\-…（）()\[\]{}<>《》~·]")
# 歌词里的段落标记：[Verse]、【副歌】、(Chorus)
_TAG = re.compile(r"^\s*[\[【(（].*?[\]】)）]\s*$")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
_VOWEL_GROUP = re.compile(r"[aeiouy]+", re.IGNORECASE)

_LATIN_VISEME = {"a": "A", "i": "I", "u": "U", "e": "E", "o": "O", "y": "I"}
# 假名直接按行段判口型（五十音每行一个主元音）。片假名单独列：
# 日文歌词里 ア/イ/ウ 比汉字还多，漏了它整句就只剩「rest」。
_KANA_VISEME = {
    "あかさたなはまやらわがざだばぱぁゃゎアカサタナハマヤラワガザダバパァャヮ": "A",
    "いきしちにひみりぎじぢびぴぃイキシチニヒミリギジヂビピィ": "I",
    "うくすつぬふむゆるぐずづぶぷぅゅゔウクスツヌフムユルグズヅブプゥュヴ": "U",
    "えけせてねへめれげぜでべぺぇエケセテネヘメレゲゼデベペェ": "E",
    "おこそとのほもよろをごぞどぼぽぉょオコソトノホモヨロヲゴゾドボポォョ": "O",
    "んン": "N",
    "っッ": "rest",
}

_TABLE: dict | None = None


def _table() -> dict:
    """Load the shipped char→pinyin table once (≈150KB JSON)."""
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    try:
        raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    syllables = list(raw.get("syllables") or [])
    chars = str(raw.get("chars") or "")
    vis = str(raw.get("vis") or "")
    index = {}
    if chars and vis and len(chars) == len(vis):
        for pos, ch in enumerate(chars):
            index[ch] = vis[pos]
    _TABLE = {"index": index, "syllables": syllables}
    return _TABLE


def viseme_of_char(ch: str) -> str:
    """One character → viseme letter (A/I/U/E/O/M/N/F/L/rest)."""
    if not ch or _PUNCT.match(ch):
        return "rest"
    table = _table()
    hit = table["index"].get(ch)
    if hit:
        return hit
    for row, vis in _KANA_VISEME.items():
        if ch in row:
            return vis
    low = ch.lower()
    if low in _LATIN_VISEME:
        return _LATIN_VISEME[low]
    if low in "mbp":
        return "M"
    if low in "fv":
        return "F"
    if low in "nlr":
        return "L"
    return "rest"


def split_lines(text: str) -> list[str]:
    """Lyrics → non-empty singable lines (section tags and blanks dropped)."""
    out = []
    for row in str(text or "").splitlines():
        line = row.strip()
        if not line or _TAG.match(line):
            continue
        out.append(line)
    return out


def _latin_units(word: str) -> list[tuple[str, str]]:
    """Latin word → one unit per vowel group (a syllable's mouth shape)."""
    groups = _VOWEL_GROUP.findall(word)
    if not groups:
        return [(word, viseme_of_char(word[0]) if word else "rest")]
    units = []
    for group in groups:
        # 双元音取开口更大的那个：ai→A、ou→O、ei→E、iu→I
        pick = group[0].lower()
        if len(group) > 1 and group[0].lower() in "iu" and group[1].lower() in "aoe":
            pick = group[1].lower()
        units.append((group, _LATIN_VISEME.get(pick, "A")))
    return units


def line_units(line: str) -> list[tuple[str, str]]:
    """A lyric line → [(text, viseme)] in singing order."""
    units: list[tuple[str, str]] = []
    pos = 0
    for match in _LATIN_WORD.finditer(line):
        for ch in line[pos:match.start()]:
            if not _PUNCT.match(ch):
                units.append((ch, viseme_of_char(ch)))
        units.extend(_latin_units(match.group(0)))
        pos = match.end()
    for ch in line[pos:]:
        if not _PUNCT.match(ch):
            units.append((ch, viseme_of_char(ch)))
    return units


def song_duration(path) -> float:
    """Duration of a WAV in seconds, float WAV included (stdlib only)."""
    p = Path(str(path or ""))
    try:
        with wave.open(str(p), "rb") as fh:
            rate = fh.getframerate() or 0
            if rate:
                return float(fh.getnframes()) / float(rate)
    except (wave.Error, EOFError, OSError):
        pass
    # wave 只认 PCM；float32 WAV 要自己读 RIFF 头。
    try:
        with open(p, "rb") as fh:
            head = fh.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return 0.0
            rate = block = 0
            data_size = 0
            while True:
                chunk = fh.read(8)
                if len(chunk) < 8:
                    break
                cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                if cid == b"fmt ":
                    body = fh.read(size)
                    rate = struct.unpack("<I", body[4:8])[0]
                    block = struct.unpack("<H", body[12:14])[0]
                elif cid == b"data":
                    data_size = size
                    break
                else:
                    fh.seek(size + (size & 1), 1)
            if rate and block and data_size:
                return float(data_size) / float(rate * block)
    except (OSError, struct.error, IndexError):
        pass
    return 0.0


# 24 位 PCM 的符号扩展表：高字节补 0xFF（负数）或 0x00（正数），
# 这样 array("i") 读出来就是正确的有符号 32 位，不用逐样本做 Python 循环。
_LRC_STAMP = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text) -> list[dict]:
    """官方 LRC → ``[{"at_s": 31.24, "text": "伝える言葉は…"}]``。

    一行挂多个时间戳（副歌复用）时逐个展开；同一时间的重复行只留一条。
    """
    rows: list[dict] = []
    for raw in str(text or "").splitlines():
        stamps = list(_LRC_STAMP.finditer(raw))
        if not stamps:
            continue
        body = _LRC_STAMP.sub("", raw).strip()
        if not body:
            continue
        for match in stamps:
            minute = int(match.group(1))
            second = int(match.group(2))
            frac = match.group(3) or "0"
            at = minute * 60 + second + int(frac) / (10 ** len(frac))
            rows.append({"at_s": round(at, 3), "text": body})
    rows.sort(key=lambda item: item["at_s"])
    out: list[dict] = []
    for row in rows:
        if out and abs(row["at_s"] - out[-1]["at_s"]) < 1e-3 and row["text"] == out[-1]["text"]:
            continue
        out.append(row)
    return out


def vocal_onsets(env: dict, *, thr: float = 0.15) -> list[float]:
    """干声包络里的「起音」时刻（秒）：响度由低跨过 thr 的那一帧。"""
    amp = list(env.get("amp") or [])
    hop = float(env.get("hop") or 0.05)
    if not amp or hop <= 0:
        return []
    out: list[float] = []
    for index in range(2, len(amp) - 1):
        if amp[index] >= thr and amp[index - 1] < thr and amp[index] >= amp[index + 1] * 0.6:
            out.append(index * hop)
    return out


def align_offset(env: dict, lines, *, max_shift: float = 15.0,
                 step: float = 0.05, tol: float = 0.25) -> float:
    """歌词时间轴相对录音的整体平移量（秒）。

    录音常常晚几秒才开始（开头留了缓冲），官方 LRC 的秒数却是原曲的。
    只比「有声区的平均响度」分不出真假——整首都在响，分数曲线几乎是平的，
    会挑到完全错的平移量。所以改比**行首**：每个歌词行的开头都该落在干声的
    一次起音附近，统计 tol 秒内命中的行占比；同分时取平均距离最小的那个。
    比原地（0 秒）没有明显优势就返回 0，宁可不挪。
    """
    starts: list[float] = []
    for row in lines or []:
        if not isinstance(row, dict) or row.get("at_s") is None:
            continue
        try:
            starts.append(max(0.0, float(row["at_s"])))
        except (TypeError, ValueError):
            continue
    if len(starts) < 3:
        return 0.0
    onsets = vocal_onsets(env)
    if len(onsets) < 3:
        return 0.0

    def measure(shift: float) -> tuple[int, float]:
        hits = 0
        nearest = 0.0
        for at in starts:
            want = at + shift
            index = bisect.bisect_left(onsets, want)
            gap = None
            for cand in onsets[max(0, index - 1):index + 2]:
                value = abs(cand - want)
                if gap is None or value < gap:
                    gap = value
            if gap is not None and gap <= tol:
                hits += 1
                nearest += gap
        return hits, (nearest / hits if hits else tol)

    steps = int(round(2 * max_shift / step)) + 1
    shifts = [round(-max_shift + index * step, 3) for index in range(steps)]
    scores = {shift: measure(shift) for shift in shifts}
    best_hits = max(item[0] for item in scores.values())
    zero_hits = measure(0.0)[0]
    # 命中行数要压过「原地」一大截，否则不挪（防止整首跑偏）。
    if best_hits < zero_hits + max(2, len(starts) * 0.15):
        return 0.0
    tied = [shift for shift, item in scores.items() if item[0] == best_hits]
    best = min(tied, key=lambda shift: (scores[shift][1], abs(shift)))
    if best_hits / len(starts) < 0.6 or scores[best][1] > tol * 0.75:
        return 0.0
    return round(best, 2)


_SIGN24 = bytes(0xFF if i >= 128 else 0x00 for i in range(256))


def _wav_fmt(path) -> tuple | None:
    """``(格式, 声道, 采样率, 位深, 帧字节, 数据偏移, 数据长度)``；读不了返回 None。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            info = None
            while True:
                chunk = fh.read(8)
                if len(chunk) < 8:
                    return None
                cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                if cid == b"fmt ":
                    body = fh.read(size)
                    fmt, ch, rate = struct.unpack("<HHI", body[:8])
                    align = struct.unpack("<H", body[12:14])[0]
                    bits = struct.unpack("<H", body[14:16])[0]
                    if fmt == 0xFFFE and len(body) >= 26:   # WAVE_FORMAT_EXTENSIBLE
                        fmt = struct.unpack("<H", body[24:26])[0]
                    info = (fmt, ch, rate, bits, align or max(1, ch * max(1, bits // 8)))
                elif cid == b"data":
                    if info is None:
                        return None
                    begin = fh.tell()
                    total = Path(path).stat().st_size - begin
                    if size <= 0 or size > total:
                        size = max(0, total)
                    return info + (begin, size)
                else:
                    fh.seek(size + (size & 1), 1)
    except (OSError, struct.error, IndexError):
        return None


def _decode_block(raw: bytes, fmt: int, bits: int):
    """一小块 PCM → 有符号样本数组（未归一化）；不支持的格式返回 None。"""
    import array
    if fmt == 3 and bits == 32:
        arr = array.array("f")
    elif fmt == 1 and bits == 16:
        arr = array.array("h")
    elif fmt == 1 and bits == 32:
        arr = array.array("i")
    elif fmt == 1 and bits == 8:
        return array.array("h", [(b - 128) << 8 for b in raw])
    elif fmt == 1 and bits == 24:
        n = len(raw) // 3
        if n <= 0:
            return array.array("i")
        body = raw[: n * 3]
        buf = bytearray(n * 4)
        buf[0::4] = body[0::3]
        buf[1::4] = body[1::3]
        buf[2::4] = body[2::3]
        buf[3::4] = body[2::3].translate(_SIGN24)
        arr = array.array("i")
        arr.frombytes(bytes(buf))
        return arr
    else:
        return None
    width = arr.itemsize
    arr.frombytes(raw[: len(raw) // width * width])
    return arr


def _block_scale(fmt: int, bits: int) -> float:
    if fmt == 3:
        return 1.0
    if bits == 32:
        return 1.0 / 2147483648.0
    if bits == 24:
        return 1.0 / 8388608.0
    if bits == 16:
        return 1.0 / 32768.0
    return 1.0 / 32768.0


def read_envelope(path, hop: float = 0.05) -> dict:
    """WAV → ``{"hop", "amp", "duration"}``：每 hop 秒的响度（0..1）。

    没有歌词时用它让嘴跟着「真的在唱」的地方开合。整段流式读（每次 1 秒），
    再抽稀到约 8kHz 算 RMS，上百 MB 的成品也只占几 MB 内存。
    """
    info = _wav_fmt(path)
    if not info:
        return {"hop": hop, "amp": [], "duration": 0.0}
    fmt, ch, rate, bits, align, offset, size = info
    ch, rate = max(1, int(ch)), int(rate or 0)
    if rate <= 0 or align <= 0 or size <= 0:
        return {"hop": hop, "amp": [], "duration": 0.0}
    duration = size / float(rate * align)
    stride = max(1, ch * max(1, rate // 8000))     # ≈8kHz、单声道取样就够
    dec_rate = rate * ch / float(stride)
    per_hop = max(1, int(round(dec_rate * hop)))
    read_frames = max(1, int(rate))                # 一次读 1 秒
    scale = _block_scale(fmt, bits)
    amps: list[float] = []
    acc, count = 0.0, 0
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            left = size
            while left > 0:
                frames = min(read_frames, left // align)
                if frames <= 0:
                    break
                raw = fh.read(frames * align)
                if not raw:
                    break
                left -= len(raw)
                block = _decode_block(raw, fmt, bits)
                if block is None:
                    return {"hop": hop, "amp": [], "duration": round(duration, 3)}
                dec = block[::stride]
                i, n = 0, len(dec)
                while i < n:
                    take = min(per_hop - count, n - i)
                    total = 0.0
                    for j in range(i, i + take):
                        v = dec[j]
                        total += v * v
                    acc += total
                    count += take
                    i += take
                    if count >= per_hop:
                        amps.append((acc / count) ** 0.5 * scale)
                        acc, count = 0.0, 0
    except (OSError, ValueError, IndexError):
        return {"hop": hop, "amp": [], "duration": round(duration, 3)}
    if count and amps is not None:
        amps.append((acc / count) ** 0.5 * scale)
    if not amps:
        return {"hop": hop, "amp": [], "duration": round(duration, 3)}
    # 用 95 分位当满幅：整体偏安静的歌也不会张不开嘴；再压一道噪声门。
    # 减掉 12% 的底噪再取 1.5 次方，嘴不会一直半张着，音节之间才有开合感。
    ordered = sorted(amps)
    top = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] or 1e-9
    gate = top * 0.06
    floor = top * 0.12
    span = max(top - floor, 1e-9)
    gains = [0.0 if a <= gate
             else round(min(1.0, max(0.0, (a - floor) / span)) ** 1.5, 3)
             for a in amps]
    return {"hop": hop, "amp": gains, "duration": round(duration, 3)}


def get_or_build_envelope(wav, *, hop: float = 0.05, refresh: bool = False) -> dict:
    """包络按文件大小+修改时间缓存成 ``<wav>.env.json``，重播不用再算。"""
    path = Path(str(wav or ""))
    try:
        st = path.stat()
    except OSError:
        return {"hop": hop, "amp": [], "duration": 0.0}
    sig = f"{st.st_size}-{int(st.st_mtime)}"
    side = path.with_suffix(".env.json")
    if not refresh:
        try:
            cached = json.loads(side.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("sig") == sig and cached.get("amp"):
                return cached
        except (OSError, ValueError):
            pass
    data = read_envelope(path, hop)
    data["sig"] = sig
    try:
        side.write_text(json.dumps({"sig": sig, "hop": data["hop"], "amp": data["amp"]}),
                        encoding="utf-8")
    except OSError:
        pass
    return data


def envelope_cues(env: dict, *, min_amp: float = 0.18,
                  piece_s: float = 0.18) -> list[list]:
    """干声包络 → 一串粗略口型 cue（给只认歌词轨的旧版桥用）。

    一段连续发声切成长约 0.18 秒的小节，每节一个口型，嘴就会跟着
    「哪里在唱」一开一合；口型字母轮换只是让嘴形有变化（没有歌词就没法知道韵母）。
    """
    amp = list(env.get("amp") or [])
    hop = float(env.get("hop") or 0.05)
    if not amp or hop <= 0:
        return []
    step = max(1, int(round(piece_s / hop)))
    letters = "AEIOU"
    cues: list[list] = []
    i, n = 0, len(amp)
    while i < n:
        if amp[i] < min_amp:
            i += 1
            continue
        start = i
        while i < n and amp[i] >= min_amp:
            i += 1
        pos = start
        while pos < i:
            span = min(step, i - pos)
            begin = pos * hop
            cues.append([".", letters[len(cues) % len(letters)],
                         round(begin, 3), round(begin + span * hop, 3)])
            pos += span
    return cues


def build_timed_cues(lines, duration: float) -> list[list]:
    """``[{"at_s": 12.0, "text": "…"}]`` → cues using the real line timestamps.

    「为她所听」识别出的歌词带每行的起唱时间，比按字数铺满整首准得多：
    行与行的边界是真时间，行内仍然按字数均分（没有音素级对齐）。
    """
    timed: list[tuple[float, str]] = []
    for row in lines or []:
        if not isinstance(row, dict):
            continue
        try:
            at = float(row.get("at_s"))
        except (TypeError, ValueError):
            continue
        text = str(row.get("text") or "").strip()
        if not text or at < 0:
            continue
        timed.append((at, text))
    if not timed:
        return []
    timed.sort(key=lambda item: item[0])
    total = float(duration or 0.0)
    cues: list[list] = []
    for index, (at, text) in enumerate(timed):
        if index + 1 < len(timed):
            end = timed[index + 1][0]
        elif total > at:
            end = total
        else:
            end = at + max(2.0, len(text) * 0.35)
        if end <= at:
            end = at + max(1.0, len(text) * 0.3)
        units = line_units(text)
        if not units:
            continue
        # 行与行之间常有几秒间奏。真按「唱到下一行」算，这一行最后一个字会被拉长
        # 成好几秒的长音，口型和歌词就彻底对不上了——所以按字数给一个上限。
        cap = max(1.2, len(units) * 0.42)
        end = min(end, at + cap)
        step = (end - at) / len(units)
        for pos, (ch, viseme) in enumerate(units):
            begin = at + pos * step
            cues.append([ch, viseme, round(begin, 3), round(begin + step, 3)])
    return cues


def _lead_in(duration: float) -> float:
    return max(1.2, min(6.0, duration * 0.06))


def _tail(duration: float) -> float:
    return max(0.8, min(2.5, duration * 0.03))


def build_cues(lyrics: str, duration: float, *, lead_in: float | None = None,
               tail: float | None = None) -> list[list]:
    """Lyrics + song length → ``[[char, viseme, start_s, end_s], ...]``.

    Lines share the sung span in proportion to their syllable count, characters
    share their line evenly.  Without phonetic alignment this is an estimate;
    within a line the error is small because the mouth only follows the local
    character order.
    """
    duration = float(duration or 0.0)
    if duration <= 0:
        return []
    lines = split_lines(lyrics)
    if not lines:
        return []
    units_per_line = [line_units(line) for line in lines]
    weights = [max(1, len(units)) for units in units_per_line]
    total = float(sum(weights))
    start_at = _lead_in(duration) if lead_in is None else float(lead_in)
    end_at = duration - (_tail(duration) if tail is None else float(tail))
    if end_at <= start_at:
        start_at, end_at = 0.0, duration
    span = end_at - start_at
    cues: list[list] = []
    cursor = start_at
    for units, weight in zip(units_per_line, weights):
        line_span = span * (weight / total)
        if not units:
            cursor += line_span
            continue
        # 行尾留 4% 给换气，避免上一行最后一个字延长到下一行第一个字。
        usable = line_span * 0.96
        step = usable / len(units)
        for index, (text, viseme) in enumerate(units):
            begin = cursor + index * step
            cues.append([text, viseme, round(begin, 3), round(begin + step, 3)])
        cursor += line_span
    return cues


def cues_path(wav) -> Path:
    return Path(str(wav)).with_suffix(".lips.json")


def lyrics_key(lyrics: str) -> str:
    """Stable short fingerprint: rebuild the sidecar when the lyrics change."""
    import hashlib
    text = "\n".join(split_lines(lyrics))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def payload_for(wav, lyrics: str, *, lang: str = "", duration: float | None = None,
                source: str = "estimate") -> dict:
    duration_seen = float(duration or 0.0) or song_duration(wav)
    cues = build_cues(lyrics, duration_seen)
    return {
        "v": 1,
        "wav": str(wav),
        "duration": round(duration_seen, 3),
        "lang": str(lang or ""),
        "source": source if cues else "none",
        "lhash": lyrics_key(lyrics),
        "cues": cues,
    }


def read_sidecar(wav) -> dict | None:
    path = cues_path(wav)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("cues") is not None else None
    except (OSError, ValueError):
        return None


def write_sidecar(payload: dict, wav=None) -> Path:
    target = cues_path(wav or payload.get("wav") or "")
    try:
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return target


def get_or_build(wav, lyrics: str, *, lang: str = "", duration: float | None = None,
                 refresh: bool = False) -> dict:
    """Sidecar on disk wins unless the lyrics changed; otherwise build + cache."""
    if not refresh:
        cached = read_sidecar(wav)
        if cached and cached.get("cues") is not None \
                and cached.get("lhash") == lyrics_key(lyrics):
            return cached
    payload = payload_for(wav, lyrics, lang=lang, duration=duration)
    write_sidecar(payload, wav)
    return payload
