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
import time
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
# 英语拼写和发音对不上的高频词：整词直接给口型，别让「按字母找元音」去猜。
# 这些词在歌词里出现得极密（the / you / love / one / are），错一个就整句错位。
_WORD_VIS = {
    # v14：the/a/an/of 的元音是弱读的 schwa，用 Z（半张、不咧嘴）；
    # 原来借 S（声母式中性口型）会让英文整句的虚词都「不张嘴」。and 实际是 [æ]。
    "the": "Z", "a": "Z", "an": "Z", "of": "Z", "and": "A", "to": "U",
    "you": "U", "youre": "U", "youve": "U", "youll": "U", "youd": "U",
    "your": "O", "yours": "O",
    "one": "A", "once": "A", "love": "A", "come": "A", "some": "A",
    "done": "A", "gone": "O", "have": "A", "give": "I", "live": "I",
    "are": "A", "were": "E", "where": "E", "there": "E", "here": "I",
    "more": "O", "before": "O", "sure": "O", "eye": "I", "eyes": "I",
    "my": "I", "by": "I", "why": "I", "sky": "I", "fly": "I", "try": "I",
    "say": "E", "day": "E", "way": "E", "away": "E", "okay": "E",
    "now": "A", "how": "A", "down": "A", "out": "A", "about": "A",
    "without": "A", "around": "A", "shout": "A", "loud": "A",
    "know": "O", "low": "O", "show": "O", "slow": "O", "snow": "O",
    "grow": "O", "blow": "O", "tomorrow": "O",
    "would": "U", "could": "U", "should": "U", "good": "U",
    "book": "U", "look": "U", "heart": "A", "break": "E", "great": "E",
}
# 元音组合 → 口型。英语的双字母组合（oo/ea/ai/ou…）如果按第一个字母读，
# 「moon」会变成 O、「heart」会变成 E——这两个错得很显眼。
_GROUP_VIS = {
    "oo": "U", "ou": "A", "ow": "A", "oa": "O", "oe": "O", "oi": "O", "oy": "O",
    "au": "O", "aw": "O", "ee": "E", "ea": "E", "ie": "I", "ei": "E", "ey": "E",
    "ai": "A", "ay": "E", "ui": "U", "ue": "U", "ew": "U", "iu": "U",
}
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
    sylmap = {}
    if chars and vis and len(chars) == len(vis):
        for pos, ch in enumerate(chars):
            index[ch] = vis[pos]
    # ``idx`` 是和 ``chars`` 一一对应的十六进制音节序号（"0,1,2,…"）。
    # 有了它才算得出「这个字唱的是哪个韵母」——拼音表里同一类口型（E）
    # 其实混着 [e] 和 [ɤ]/[ɨ] 两种完全不同的嘴形，详见 _is_schwa。
    codes = str(raw.get("idx") or "")
    if codes and syllables and chars:
        parts = codes.split(",")
        if len(parts) == len(chars):
            for pos, ch in enumerate(chars):
                code = parts[pos]
                if not code:
                    continue
                try:
                    sylmap[ch] = syllables[int(code, 16)]
                except (ValueError, IndexError):
                    pass
    _TABLE = {"index": index, "syllables": syllables, "syl": sylmap}
    return _TABLE


# 舌尖元音：zhi/chi/shi/ri/zi/ci/si 的 i 不是 [i]，是 [ɨ]——嘴唇基本不咧，
# 只把牙露出来。按 I（form +0.95）去写，满篇「是/时/只」都变成咧嘴笑。
_APICAL = frozenset(("zhi", "chi", "shi", "ri", "zi", "ci", "si"))

# ---------------- v12：音节内部的口型相位（声母 → 韵母 → 韵尾） ----------------
# 以前一个 cue 只有一个元音口型，整个字从开唱到唱完都是同一个嘴形。
# 可「妈」和「怕」和「他」在画面上的区别，一半在开口那一下：b/p/m 是真的闭唇，
# f 是唇齿，d/t/l 微张，z/c/s/zh/ch/sh 嘴唇中性略收；字尾的 n/ng 和 ai/ao 这类
# 韵尾也是嘴形的收势。把这几段按比例放进同一个 cue 里，桥上按时间插值，
# 嘴就会在字内「动起来」，而不是一个字一张定格的图。
# 值 = (口型, 占整个字时长的比例)。比例按音节长度走，字唱得越慢这段越长。
_ZH_ONSET = {
    "b": ("M", 0.11), "p": ("M", 0.10), "m": ("M", 0.12),
    "f": ("F", 0.14),
    "d": ("L", 0.10), "t": ("L", 0.10), "n": ("L", 0.11), "l": ("L", 0.11),
    "g": ("S", 0.10), "k": ("S", 0.10), "h": ("S", 0.13),
    "j": ("S", 0.13), "q": ("S", 0.13), "x": ("S", 0.13),
    "zh": ("S", 0.15), "ch": ("S", 0.15), "sh": ("S", 0.15), "r": ("L", 0.13),
    "z": ("S", 0.14), "c": ("S", 0.14), "s": ("S", 0.14),
}
_ZH_INITIAL = re.compile(r"^(zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw])")


def _zh_coda(fin: str) -> tuple[str, float]:
    """韵母的收势：n/ng 收到闭口，ai/ao 这类复韵母往滑音收。"""
    if fin.endswith("ng"):
        return ("S", 0.18)     # 后鼻音：嘴回到中性，不像 n 那样闭死
    if fin.endswith("n"):
        return ("N", 0.18)     # 前鼻音：舌尖抵住，嘴基本合上
    if fin.endswith(("ai", "ei", "ui", "uai", "uei")):
        return ("I", 0.20)
    if fin.endswith(("ao", "ou", "iu", "iao", "iou", "uo")):
        return ("U", 0.18)
    return ("", 0.0)


# 英语同样分两段：词首辅音（唇音闭上、唇齿、圆唇的 w）和词尾辅音
# （home 的 m、time 的 m、hold 的 d…）。只给词的最后一个音节加韵尾——
# 词中间那些辅音本来就连着下一个音节的起头，硬拆反而更假。
_EN_ONSET = {
    "b": ("M", 0.12), "p": ("M", 0.11), "m": ("M", 0.13),
    "f": ("F", 0.14), "v": ("F", 0.13),
    "w": ("U", 0.13),
    "l": ("L", 0.11), "n": ("L", 0.11), "r": ("L", 0.11),
    "s": ("S", 0.13), "z": ("S", 0.12), "c": ("S", 0.12), "j": ("S", 0.13),
    "t": ("S", 0.10), "d": ("S", 0.10), "k": ("S", 0.10), "g": ("S", 0.10),
    "h": ("S", 0.11), "x": ("S", 0.12), "q": ("S", 0.12),
}
_EN_CODA = {
    "m": ("M", 0.16), "n": ("N", 0.16),
    "f": ("F", 0.14), "v": ("F", 0.14),
    "l": ("L", 0.14), "r": ("L", 0.12),
    "s": ("S", 0.14), "z": ("S", 0.14),
    "t": ("S", 0.12), "d": ("S", 0.12), "k": ("S", 0.12), "g": ("S", 0.12),
    "p": ("M", 0.12), "b": ("M", 0.12), "c": ("S", 0.12), "x": ("S", 0.12),
    "j": ("S", 0.12), "h": ("S", 0.12), "q": ("S", 0.12),
}


def _is_schwa(syl: str) -> bool:
    """这个音节是不是「中性口型」：[ɤ]（的/了/着/和/个/这）或舌尖元音。

    普通话歌词里「的」这种字一出现就是每行一个，按 E 的咧嘴笑去演，
    整首歌看起来都在假笑；英语的 the / a / of 同理，统一用中性元音 Z
    （嘴半张、不咧嘴）。
    """
    if syl in _APICAL:
        return True
    if not syl.endswith("e"):
        return False
    if syl.startswith("y"):          # ye / yue：前元音
        return False
    if syl.endswith(("ie", "ue", "ve")):   # jie / qie / xie / xue / jue
        return False
    return True


def viseme_of_char(ch: str) -> str:
    """One character → viseme letter (A/I/U/E/O/S/Z/M/N/F/L/rest)."""
    if not ch or _PUNCT.match(ch):
        return "rest"
    table = _table()
    hit = table["index"].get(ch)
    if hit:
        syl = table.get("syl", {}).get(ch) or ""
        if syl and _is_schwa(syl):
            # v14：中性元音另立 Z。[ɤ]/[ɨ] 是「嘴半张、嘴唇不咧」的元音，
            # 之前借用 S（= 声母的中性口型，张口度只有 0.34），
            # 「的/是/实/时」这类字唱起来嘴几乎不动，就是用户说的「嘴巴不张」。
            # S 仍然是声母/韵尾的中性收口，不能跟着抬高，否则 g/k/h/zh 起手就张。
            return "Z"
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


def _cell_of_char(ch: str) -> tuple[str, str, str, float, str, float]:
    """一个字 → (字, 元音口型, 声母口型, 声母占比, 韵尾口型, 韵尾占比)。

    元音口型就是 v11 的 ``viseme_of_char``；多出来的是同一音节里
    「开口那一下」和「收尾那一下」。韵尾和元音同形时不写——嘴形一样，
    写了也只是多一次无变化的插值。表外字（假名、没拼音的汉字）只给元音。
    """
    vis = viseme_of_char(ch)
    if vis == "rest":
        return (ch, vis, "", 0.0, "", 0.0)
    syl = _table().get("syl", {}).get(ch) or ""
    if not syl:
        return (ch, vis, "", 0.0, "", 0.0)
    m = _ZH_INITIAL.match(syl)
    on, of = _ZH_ONSET.get(m.group(1), ("", 0.0)) if m else ("", 0.0)
    coda, cf = _zh_coda(syl[m.end():] if m else syl)
    if on == vis:
        on, of = "", 0.0
    if coda == vis:
        coda, cf = "", 0.0
    return (ch, vis, on, of, coda, cf)


def _phase_tag(on: str, of: float, coda: str, cf: float) -> str:
    """cue 第 7 位（可选）的相位串：``声母:占比:韵尾:占比``，缺的写 ``-``。"""
    if not on and not coda:
        return ""
    return "%s:%.2f:%s:%.2f" % (on or "-", of, coda or "-", cf)


def split_lines(text: str) -> list[str]:
    """Lyrics → non-empty singable lines (section tags and blanks dropped)."""
    out = []
    for row in str(text or "").splitlines():
        line = row.strip()
        if not line or _TAG.match(line):
            continue
        out.append(line)
    return out


def _latin_units(word: str) -> list[tuple]:
    """Latin word → one unit per sung syllable (a syllable's mouth shape).

    以前是「一个元音组一个口型」，但英语里有两类结构会让字数算多：
      · 词尾不发音的 e：love / take / shine / time / please / dance
        全都被拆成两拍，一行歌词的口型位比真实音节多出 1~3 个，
        时间被摊薄之后，嘴就整体跑在声音前面（用户说的「英文口型不对」）；
      · 词首的 y 是辅音：your / yes / young 被当成元音，your 变成 I。
    现在先按拼写规则去掉不发音的 e、把词首 y 当辅音，再用双字母组合表
    （oo→U、ea→E、ai→A…）取口型，最后才按元音组切分。

    v12 起每个音节多带两段：本音节的起手辅音（b/p/m 闭唇、f 唇齿、w 圆唇）
    和词尾辅音（home 的 m、hold 的 d）。词中间的辅音不加韵尾——它本来就连着
    下一个音节的起头。返回的是 6 元组（同 ``_cell_of_char``）。
    """
    low = word.lower().replace("’", "'")
    key = re.sub(r"[^a-z]", "", low)
    if not key:
        return [(word, "rest", "", 0.0, "", 0.0)]
    if key in _WORD_VIS:
        return [(word, _WORD_VIS[key], "", 0.0, "", 0.0)]
    w = key
    # 词尾不发音的 e（但 wanted / needed、makes / watches 里的 e 要发音）
    if len(w) >= 3 and w.endswith("e") and w[-2] not in "aeiouy":
        keep = False
        if w.endswith("ed") and len(w) >= 4 and w[-3] in "td":
            keep = True
        elif w.endswith("es") and len(w) >= 4 and (
                w[-3] in "sxz" or w.endswith(("ches", "shes"))):
            keep = True
        if not keep and any(c in "aeiouy" for c in w[:-1]):
            w = w[:-1]
    # 词首 y 在元音前是辅音（your / yes / you）
    if len(w) >= 2 and w[0] == "y" and w[1] in "aeiou":
        w = "j" + w[1:]
    matches = list(_VOWEL_GROUP.finditer(w))
    if not matches:
        return [(word, viseme_of_char(word[0]) if word else "rest",
                 "", 0.0, "", 0.0)]
    last = len(matches) - 1
    units = []
    for pos, match in enumerate(matches):
        g = match.group(0).lower()
        if g in _GROUP_VIS:
            vis = _GROUP_VIS[g]
        elif len(g) >= 2 and g[:2] in _GROUP_VIS:
            vis = _GROUP_VIS[g[:2]]
        else:
            vis = _LATIN_VISEME.get(g[0], "A")
        # 词首 / 两个元音之间的那串辅音，取第一个字母当起手口型。
        pre = w[(matches[pos - 1].end() if pos else 0):match.start()]
        on, of = _EN_ONSET.get(pre[:1], ("", 0.0))
        # 韵尾只给词的最后一个音节。
        coda, cf = ("", 0.0)
        if pos == last:
            post = w[match.end():]
            if post.endswith("ng"):
                coda, cf = ("N", 0.16)
            elif post:
                coda, cf = _EN_CODA.get(post[-1], ("", 0.0))
        if on == vis:
            on, of = "", 0.0
        units.append((match.group(0), vis, on, of, coda, cf))
    return units


def _carry_visemes(units: list[tuple]) -> list[tuple]:
    """让「占时值但不发音」的字沿用邻字的口型，别在句子中间闭嘴。

    ``line_units`` 会把长音符「ー」、重复记号「々」、促音「っ」以及
    ``lips_pinyin.json`` 里没有的汉字判成 rest；rest 在模型上等于
    ParamMouthOpenY≈0.1 —— 一句话里每隔几个字嘴就闭一下，看起来像在嚼东西。
    演唱时长音符是把上一个元音延长、促音只是把音顿一下马上接下去，
    表外字至少也该沿用邻字的口型。所以行内所有 rest 都接到最近的有效口型
    （行首的 rest 借后面第一个），只有整行都没有口型时才保持 rest。
    借来的口型不带相位：声母/韵尾是那个字自己的一次发音，不该重复。
    """
    if not units:
        return units
    out = list(units)
    fill = next((unit[1] for unit in out if unit[1] != "rest"), "")
    if not fill:
        return out
    last = fill
    for pos, unit in enumerate(out):
        if unit[1] == "rest":
            out[pos] = (unit[0], last, "", 0.0, "", 0.0)
        else:
            last = unit[1]
    return out


def _line_cells(line: str) -> list[tuple]:
    """A lyric line → [(text, viseme, onset, onset_frac, coda, coda_frac)]."""
    cells: list[tuple] = []
    pos = 0
    for match in _LATIN_WORD.finditer(line):
        for ch in line[pos:match.start()]:
            if not _PUNCT.match(ch):
                cells.append(_cell_of_char(ch))
        cells.extend(_latin_units(match.group(0)))
        pos = match.end()
    for ch in line[pos:]:
        if not _PUNCT.match(ch):
            cells.append(_cell_of_char(ch))
    return _carry_visemes(cells)


def line_units(line: str) -> list[tuple[str, str]]:
    """A lyric line → [(text, viseme)] in singing order."""
    return [(cell[0], cell[1]) for cell in _line_cells(line)]


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


def vocal_syllable_onsets(env: dict, *, floor: float = 0.12, delta: float = 0.05,
                          min_gap: float = 0.10) -> list[float]:
    """音节级起音：包络「明显往上跳」的那些帧，约 3~4 个/秒。

    ``vocal_onsets`` 找的是从安静跨过门限的大起音，适合把整行歌词对到录音上；
    但连唱时音量很少掉到门限以下，一整行里往往只剩一个起音。这里改看能量上升沿：
    唱一个字通常伴随一次能量跳变，用来把行内的字边界吸到真正唱出来的位置上。
    """
    amp = list(env.get("amp") or [])
    hop = float(env.get("hop") or 0.05)
    if not amp or hop <= 0:
        return []
    out: list[float] = []
    for index in range(1, len(amp) - 1):
        if amp[index] < floor or amp[index] - amp[index - 1] < delta:
            continue
        if amp[index] < amp[index + 1] * 0.9:   # 只是上升沿中间，不是一次起音
            continue
        at = index * hop
        if not out or at - out[-1] >= min_gap:
            out.append(at)
    return out


def _norm_text(t: str) -> str:
    return re.sub(r"[\W_]+", "", str(t or "").lower())


def _sim(a: str, b: str) -> float:
    """两段文字的相似度 0..1（按字符二元组重合，不怕识别器错一两个字）。"""
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return 0.0
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def _vocal_phrases(env: dict | None, *, gap: float = 0.6, min_len: float = 0.4) -> list:
    """干声包络 → 唱的短语起点列表（间隔 ≥gap 秒的无声才算断句）。"""
    amp = list((env or {}).get("amp") or [])
    hop = float((env or {}).get("hop") or 0.05)
    if not amp or hop <= 0:
        return []
    peak = max(amp) or 1.0
    on = [v > peak * 0.12 for v in amp]
    starts, i, n = [], 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j < n and (on[j] or any(on[j:j + int(gap / hop)])):
                j += 1
            if (j - i) * hop >= min_len:
                starts.append(round(i * hop, 3))
            i = j
        else:
            i += 1
    return starts


def align_user_lyrics(user_lyrics: str, asr_lines: list, duration: float = 0.0,
                      env: dict | None = None) -> list:
    """用户写的歌词（无时间）+ 识别出的带时间行 → 带时间的用户歌词行。

    文字一律用用户写的（识别只提供时间）。用动态规划做全局单调对齐（类似编辑距离）：
    用户句 i 对识别句 k 的得分 = 文字相似度；允许跳过（识别漏句 / 多出幻觉句）、
    允许一句识别对应用户的连续几句（识别把两句并成一句：按字数在该识别句时长里分配）。
    没对上的句子按前后已定位的句子插值。对上的比例 <30% 返回空，宁可不用。
    """
    lines = split_lines(user_lyrics)
    asr = [r for r in (asr_lines or []) if isinstance(r, dict) and str(r.get("text") or "").strip()]
    n, m = len(lines), len(asr)
    if not n or not m:
        return []
    NEG = -1e9
    # 覆盖判定：用户句 i 是不是识别句 k 的一部分（识别合并句）
    def score(i, k):
        a, b = lines[i], asr[k]["text"]
        sc = _sim(a, b)
        na, nb = _norm_text(a), _norm_text(b)
        if na and nb and (na in nb or nb in na):
            sc = max(sc, 0.75)
        return sc
    S = [[score(i, k) for k in range(m)] for i in range(n)]
    # dp[i][k]：前 i 句用户歌词、前 k 句识别已处理的最高分；允许一个识别句被连续多句用户歌词共用
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    bt = [[None] * (m + 1) for _ in range(n + 1)]
    for k in range(m + 1):
        dp[0][k] = 0.0
    for i in range(1, n + 1):
        dp[i][0] = 0.0
        bt[i][0] = ("skip_u", i - 1, 0)
        for k in range(1, m + 1):
            best, how = dp[i - 1][k], ("skip_u", i - 1, k)          # 用户句没对上
            if dp[i][k - 1] > best:
                best, how = dp[i][k - 1], ("skip_a", i, k - 1)      # 识别句是多余的
            sc = S[i - 1][k - 1]
            if sc >= 0.4:
                v = dp[i - 1][k - 1] + sc                           # 一对一
                if v > best:
                    best, how = v, ("match", i - 1, k - 1)
                v2 = dp[i - 1][k] + sc * 0.9                         # 与上一句共用同一识别句
                if bt[i - 1][k] and bt[i - 1][k][0] in ("match", "share") and v2 > best:
                    best, how = v2, ("share", i - 1, k)
            dp[i][k], bt[i][k] = best, how
    pairs = {}
    i, k = n, m
    while i > 0 and k >= 0:
        how = bt[i][k]
        if how is None:
            break
        kind, pi, pk = how
        if kind in ("match", "share"):
            pairs[i - 1] = k - 1 if kind == "match" else k - 1
        i, k = (pi, pk) if kind != "match" else (pi, pk)
        if kind == "share":
            i, k = pi, pk
    if len(pairs) < max(2, 0.3 * n):
        return []
    # 同一识别句被几句用户歌词共用：按字数在该识别句时长里分配起点
    out: list = [None] * n
    by_k: dict = {}
    for ui, ak in pairs.items():
        by_k.setdefault(ak, []).append(ui)
    for ak, uis in by_k.items():
        uis.sort()
        a0 = float(asr[ak].get("at_s") or 0)
        a1 = float(asr[ak].get("end_s") or a0 + 2.0)
        w = [max(1, len(_norm_text(lines[u]))) for u in uis]
        tot = float(sum(w))
        acc = 0.0
        for u, ww in zip(uis, w):
            out[u] = a0 + (a1 - a0) * acc / tot
            acc += ww
    known = [(i, t) for i, t in enumerate(out) if t is not None]
    end = float(duration or 0) or (float(asr[-1].get("end_s") or asr[-1].get("at_s") or 0) + 2.0)
    # 没对上的句子：先按干声的「唱的短语」逐句往后排（在前后已定位句之间，按顺序取短语起点）
    phrases = _vocal_phrases(env)
    if phrases:
        i = 0
        while i < n:
            if out[i] is not None:
                i += 1
                continue
            j = i
            while j < n and out[j] is None:
                j += 1
            lo = (out[i - 1] + 0.8) if i > 0 and out[i - 1] is not None else 0.0
            hi = out[j] - 0.3 if j < n and out[j] is not None else end
            cand = [p for p in phrases if lo <= p < hi]
            need = j - i
            if len(cand) >= need:
                # 按顺序一句接一句取短语起点（唱歌是连着唱的，句子跟着下一个短语走）
                for t in range(need):
                    out[i + t] = cand[t]
            i = j
        known = [(i, t) for i, t in enumerate(out) if t is not None]
    for i in range(n):
        if out[i] is not None:
            continue
        prev = max((x for x in known if x[0] < i), default=None, key=lambda x: x[0])
        nxt = min((x for x in known if x[0] > i), default=None, key=lambda x: x[0])
        if prev and nxt:
            out[i] = prev[1] + (nxt[1] - prev[1]) * (i - prev[0]) / (nxt[0] - prev[0])
        elif prev:
            out[i] = min(end - 1.0, prev[1] + 3.0 * (i - prev[0]))
        elif nxt:
            out[i] = max(0.0, nxt[1] - 3.0 * (nxt[0] - i))
    rows = []
    last = -1.0
    for line, at in zip(lines, out):
        at = max(float(at), last + 0.3)
        rows.append({"at_s": round(at, 3), "text": line})
        last = at
    return rows


# 歌声识别在没人声/纯伴奏处常「编」出来的句子（视频平台片尾、字幕组署名之类）
_ASR_HALLU = re.compile(r"(独播|獨播|剧场|劇場|字幕|订阅|訂閱|点赞|點贊|关注|關注|转发|轉發|感谢观看|謝謝觀看|谢谢观看|"
                        r"谢谢收看|謝謝收看|感谢收看|感謝收看|下期再见|下期再見|Television|Exclusive|subtitles?|"
                        r"Thank(s| you) for watching|请不吝|請不吝|明镜|明鏡|栏目|欄目|本节目|本節目|未经授权|未經授權|ご視聴)", re.I)


def _voiced_frac(env: dict | None, t0: float, t1: float) -> float:
    amp = list((env or {}).get("amp") or [])
    hop = float((env or {}).get("hop") or 0.05)
    if not amp or hop <= 0 or t1 <= t0:
        return 1.0
    peak = max(amp) or 1.0
    seg = amp[max(0, int(t0 / hop)):max(1, int(t1 / hop))]
    return sum(1 for v in seg if v > peak * 0.12) / max(1, len(seg))


def clean_asr_lines(asr_lines, env: dict | None = None) -> list:
    """去掉识别的幻觉句：命中常见片尾/字幕套话，或这句的时间里干声基本没有声音。"""
    out = []
    for r in asr_lines or []:
        if not isinstance(r, dict) or not str(r.get("text") or "").strip():
            continue
        text = str(r["text"])
        if _ASR_HALLU.search(text):
            continue
        a0 = float(r.get("at_s") or 0.0)
        a1 = float(r.get("end_s") or a0 + 2.0)
        if env and _voiced_frac(env, a0, a1) < 0.12:
            continue
        out.append(r)
    return out


def asr_missing_frac(asr_lines, env: dict | None) -> float:
    """有人声、但没有任何识别句覆盖的时间占全部人声时间的比例（0..1）。"""
    amp = list((env or {}).get("amp") or [])
    hop = float((env or {}).get("hop") or 0.05)
    if not amp or hop <= 0:
        return 0.0
    peak = max(amp) or 1.0
    spans = [(float(r.get("at_s") or 0) - 0.5,
              float(r.get("end_s") or (float(r.get("at_s") or 0) + 2.0)) + 0.5)
             for r in asr_lines or [] if isinstance(r, dict)]
    voiced = miss = 0
    for i, v in enumerate(amp):
        if v <= peak * 0.12:
            continue
        voiced += 1
        t = i * hop
        if not any(a <= t <= b for a, b in spans):
            miss += 1
    return miss / voiced if voiced else 0.0


# 歌词文件里的制作人员行（「词曲：xxx」「混音/母带：xxx」）
_CREDIT_LINE = re.compile(r"^[^：:]{0,16}(词|曲|编|混|母|唱|和声|制作|监制|出品|策划|企划|统筹|营销|吉他|贝斯|鼓|录|美术|题字|调校|调教|PV|OP|SP)[^：:]{0,10}[：:]")


def fuse_asr_reference(asr_lines, ref_lines, env: dict | None, duration: float) -> list:
    """识别为主、搜到的歌词为辅：
    - 时间以识别为准（它听的就是这段录音，版本/剪辑都对）；
    - 先用文字很像的句子（锚点）量出「录音时间 - 参考歌词时间」的差（就近取，录音有剪辑也能跟上）；
    - 每个识别句：参考歌词里按时间差落在这句时间段内的句子，文字合起来和识别句相像 → 换成参考歌词
      （修错字；识别把两句并成一句时，会换成两句并按参考时间分开）；
    - 参考歌词里有、识别漏掉的句子 → 放进去，但只放在真有人声、又没有识别句的地方（补缺句）；
    - 识别有、参考对不上的句子保留（翻唱改词/即兴）。
    锚点太少（<3）说明不是同一首，返回 []。"""
    asr = [r for r in (asr_lines or []) if isinstance(r, dict) and str(r.get("text") or "").strip()]
    ref = [r for r in (ref_lines or []) if isinstance(r, dict) and str(r.get("text") or "").strip()
           and r.get("at_s") is not None and not _CREDIT_LINE.match(str(r.get("text") or "").strip())]
    if len(asr) < 3 or len(ref) < 3:
        return []
    # 1) 锚点：文字很像的识别句 ↔ 参考句（单调往后找；歌会重复，所以每次从上个锚点往后）
    anchors = []
    j0 = 0
    for k, a in enumerate(asr):
        best, bj = 0.0, -1
        for j in range(j0, min(len(ref), j0 + 25)):
            sc = _sim(a["text"], ref[j]["text"])
            na, nb = _norm_text(a["text"]), _norm_text(ref[j]["text"])
            if na and nb and len(min(na, nb, key=len)) >= 3 and (na in nb or nb in na):
                sc = max(sc, 0.7)
            if sc > best:
                best, bj = sc, j
        if bj >= 0 and best >= 0.34:
            anchors.append((float(a["at_s"]), float(a["at_s"]) - float(ref[bj]["at_s"])))
            j0 = bj + 1
    if len(anchors) < 3:
        return []

    def shift_at(t: float) -> float:
        # 就近 3 个锚点的时间差取中位数
        near = sorted(anchors, key=lambda x: abs(x[0] - t))[:3]
        ds = sorted(d for _, d in near)
        return ds[len(ds) // 2]

    used = set()
    rows = []
    for a in asr:
        a0 = float(a["at_s"])
        a1 = float(a.get("end_s") or a0 + 2.0)
        sh = shift_at(a0)
        inside = [j for j, r in enumerate(ref) if j not in used and a0 - 0.8 <= float(r["at_s"]) + sh < a1 - 0.2]
        take = []
        if inside:
            joined = "".join(ref[j]["text"] for j in inside)
            single = max(inside, key=lambda j: _sim(a["text"], ref[j]["text"]))
            if _sim(a["text"], joined) >= 0.25:
                take = inside
            elif _sim(a["text"], ref[single]["text"]) >= 0.34:
                take = [single]
        if take:
            for n, j in enumerate(take):
                at = a0 if n == 0 else min(max(float(ref[j]["at_s"]) + sh, a0 + 0.3), a1 - 0.3)
                rows.append({"at_s": round(at, 3), "text": ref[j]["text"], "from": "asr+ref"})
                used.add(j)
        elif rows and rows[-1]["from"] == "asr+ref" and _sim(a["text"], rows[-1]["text"]) >= 0.3:
            continue      # 识别把参考歌词的一句拆成了两段：后半段已经包含在上一句里
        else:
            rows.append({"at_s": a0, "text": a["text"], "from": "asr"})
    spans = [(float(a["at_s"]) - 0.3, float(a.get("end_s") or float(a["at_s"]) + 2.0) + 0.3) for a in asr]
    dur = float(duration or 0.0) or 1e9
    for j, r in enumerate(ref):
        if j in used:
            continue
        at = float(r["at_s"]) + shift_at(float(r["at_s"]) + anchors[0][1])
        nxt = at + 3.0
        if j + 1 < len(ref):
            nxt = float(ref[j + 1]["at_s"]) + (at - float(r["at_s"]))
        end = max(at + 0.8, min(nxt, at + 6.0))
        if at < 0 or at >= dur - 0.5:
            continue
        if any(a < end - 0.4 and at + 0.4 < b for a, b in spans):
            continue      # 这段已经有识别句了（识别听到的版本和参考不同），不硬塞
        if env and _voiced_frac(env, at, end) < 0.3:
            continue      # 这里没唱（录音剪掉了这段 / 参考是完整版）
        rows.append({"at_s": round(at, 3), "text": r["text"], "from": "ref"})
        spans.append((at - 0.3, end + 0.3))
    rows.sort(key=lambda r: r["at_s"])
    out, last = [], -1.0
    for r in rows:
        at = max(r["at_s"], last + 0.3)
        out.append({"at_s": round(at, 3), "text": r["text"], "from": r["from"]})
        last = at
    return out


def text_match_rate(heard, lyric_lines) -> float | None:
    """识别出的句子里有多少「二字片段」出现在歌词里（0..1；字太少返回 None）。
    同一首歌即使识别错字多也有 0.3~0.6；不同的歌通常 <0.05。"""
    def grams(text):
        t = re.sub(r"[\W_]+", "", str(text or "").lower())
        return [t[i:i + 2] for i in range(len(t) - 1)]
    ref = set()
    for line in lyric_lines or []:
        ref.update(grams(line))
    got = [g for line in (heard or []) for g in grams(line)]
    if len(got) < 20 or not ref:
        return None
    return sum(1 for g in got if g in ref) / len(got)


def lyric_match_rate(env: dict, lines, *, tol: float = 0.5) -> float | None:
    """歌词和这首歌的人声对不对得上（0..1，越高越可信；行太少或没起音返回 None）。

    两项取小：① 行首落在干声起音 tol 秒内的比例；② 「首句之前」干声基本无声
    （首句前 >4 s 时，那段的人声占比要 <0.15，否则说明歌词比人声晚一大截——多半不是这首，
    或者是完整版歌词配了剪过的音频）。② 不满足时返回 ① 与 0.3 的较小值。
    """
    onsets = vocal_onsets(env)
    dur = float(env.get("duration") or 0.0) or 1e9
    starts = []
    for row in lines or []:
        try:
            at = float(row.get("at_s"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= at < dur:
            starts.append(at)
    if len(starts) < 6 or len(onsets) < 6:
        return None
    hits = 0
    for at in starts:
        i = bisect.bisect_left(onsets, at)
        if any(abs(c - at) <= tol for c in onsets[max(0, i - 1):i + 2]):
            hits += 1
    rate = hits / len(starts)
    first = min(starts)
    amp = list(env.get("amp") or [])
    hop = float(env.get("hop") or 0.05)
    if first > 4.0 and amp and hop > 0:
        peak = max(amp) or 1.0
        seg = amp[: max(1, int((first - 1.0) / hop))]
        pre_voiced = sum(1 for v in seg if v > peak * 0.12) / max(1, len(seg))
        if pre_voiced >= 0.15:
            rate = min(rate, 0.3)
    return rate


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


def _snap_bounds(bounds: list[float], onsets: list[float],
                 tol: float) -> tuple[list[float], int]:
    """把行内按字数均分的字边界挪到干声的起音上——一个字 ≈ 一个音节。

    每个内部边界只在 ``±tol`` 秒内找最近的起音；找不到就保留均分位置。
    首尾边界不动（它们是真时间轴或整行的上下限），并且强制单调递增，
    免得两段相邻的起音把某个字挤成负时长。
    """
    if len(bounds) < 3 or not onsets:
        return bounds, 0
    snapped = list(bounds)
    moved = 0
    floor = bounds[0] + 0.04
    for pos in range(1, len(bounds) - 1):
        target = bounds[pos]
        ceiling = bounds[pos + 1] - 0.04
        start = bisect.bisect_left(onsets, target - tol)
        best, gap = 0.0, tol + 1e-6
        index = start
        while index < len(onsets) and onsets[index] <= target + tol:
            value = onsets[index]
            if floor < value < ceiling:
                offset = abs(value - target)
                if offset < gap:
                    best, gap = value, offset
            index += 1
        if gap <= tol:
            snapped[pos] = best
            moved += 1
        # 下一个边界至少留 50ms，别把某个字挤成一闪而过的口型。
        floor = max(floor, snapped[pos] + 0.05)
    return snapped, moved


def _breath_marks(cues: list[list], env: dict) -> list[list]:
    """句尾标记只留在「干声真的掉下去」的行尾上。

    LRC 的行与行常常首尾相接，时间轴上没有空隙；可录音里并不一定每一行
    都换气——连着唱的两句之间嘴不该闭。逐行看句尾前 0.15 秒、后 0.20 秒
    的最低能量，掉到本行峰值 55% 以下就算换气（阈值放宽过：按 30% 判，
    很多只轻轻掉一点的句尾不被认可，画面上就几乎看不到闭嘴）。
    没有包络（纯估计轨）时保留原标记，由桥按标记收口。
    """
    amp = list((env or {}).get("amp") or [])
    hop = float((env or {}).get("hop") or 0.0)
    if not amp or hop <= 0:
        return cues
    out: list[list] = []
    for cue in cues:
        if len(cue) > 4 and cue[4]:
            a, b = float(cue[2]), float(cue[3])
            ia, ib = max(0, int(a / hop)), min(len(amp), int(b / hop) + 1)
            seg = amp[ia:ib]
            ref = max(seg) if seg else 0.0
            i0 = max(0, int((b - 0.15) / hop))
            i1 = min(len(amp), int((b + 0.20) / hop) + 1)
            low = min(amp[i0:i1]) if i1 > i0 else 0.0
            # 包络在这一段是 0（例如分离人声前段是静音）时无法判断，宁可不标。
            if ref <= 0.05 or low > 0.55 * ref:
                # 只清句尾标记；v12 的相位串在第 7 位，不能跟着被截掉。
                cue = [cue[0], cue[1], cue[2], cue[3], 0] \
                    + [x for x in cue[5:] if isinstance(x, str)]
        out.append(cue)
    return out


def _apply_stress(cues: list[list], env) -> list[list]:
    """给每个字估一个 0..1 的重音强度（桥用它决定张口幅度和起手速度）。

    三路信号：
    - 全局：这个字在自己的时长里能到多响（对整首 20%~90% 分位归一化）；
    - 局部：比前后各三个字的中位数响多少——同一句里被强调的那个词；
    - 名次：在前后各四个字里排第几，副歌整段都响时也能分出轻重。
    最后按本曲自己的分位二次拉伸，轻音压到 0、重音顶到 1。没有包络时统一
    给 0.5，桥的行为和以前一样。顺带把 cue 统一成
    ``[字, 口型, 起, 止, 句尾标记, 重音]`` 六元组，旧的四/五元组也能读。
    """
    amp = list((env or {}).get("amp") or [])
    hop = float((env or {}).get("hop") or 0.0)
    n = len(cues)
    if n <= 0:
        return cues
    peaks: list[float] = []
    for cue in cues:
        a = b = 0
        if amp and hop > 0:
            try:
                # 前后各放宽一帧：0.1 秒的短字本来只盖住两三个包络帧，
                # 落在换气缝里就会读出 0，重音直接判成轻音。
                a = max(0, int(float(cue[2]) / hop) - 1)
                b = min(len(amp), int(float(cue[3]) / hop) + 2)
            except (TypeError, ValueError, IndexError):
                a = b = 0
        seg = amp[a:b]
        if seg:
            # 取「字里较响的那一半帧」的平均，而不是取峰值：包络是按全曲 p95
            # 归一化的，响一点的字峰值全被削到 1.0，只看 max 会让副歌整段
            # 都是同一个重音；平均半边能分出「一直很响」和「碰巧顶了一下」。
            ordered = sorted(seg)
            half = ordered[len(ordered) // 2:]
            peaks.append(sum(half) / len(half))
        else:
            peaks.append(0.0)
    lo = span = 0.0
    if any(peaks):
        ordered = sorted(peaks)
        last = len(ordered) - 1

        def pct(q: float) -> float:
            return ordered[min(last, max(0, int(round(q * last))))]

        lo = pct(0.20)
        span = max(pct(0.90) - lo, 0.12)
    raw: list[float] = []
    for i, cue in enumerate(cues):
        stress = 0.5
        if span > 0:
            g = min(1.0, max(0.0, (peaks[i] - lo) / span))
            neigh = [peaks[j] for j in range(max(0, i - 3), min(n, i + 4)) if j != i]
            local = 0.0
            if neigh:
                neigh.sort()
                med = neigh[len(neigh) // 2]
                # 比邻字中位数响 20% 记一半，60% 记满。
                local = min(1.0, max(0.0, (peaks[i] - med) / max(med, 0.18) * 0.5))
            stress = 0.65 * g + 0.35 * local
        raw.append(stress)
    # 二次拉伸：上面那路信号天生挤在 0.2~0.8（g 只到过自己曲子的 p90，local
    # 也很少打满），直接用的话重音和轻音差不到两成，画面上还是一样。按本曲
    # 自己的 15%~90% 分位再拉开一次：轻音压到 0、重音顶到 1，桥那边才真的有
    # 「重」和「轻」的分别（张口更深、起手更快）。
    lo2 = hi = 0.5
    if any(raw):
        ordered = sorted(raw)
        last = len(ordered) - 1
        lo2 = ordered[max(0, int(round(0.15 * last)))]
        hi = ordered[max(0, int(round(0.90 * last)))]
    spread = max(hi - lo2, 0.10)
    def _local_rank(i: int, w: int = 4) -> float:
        """这个字在前后各 w 个字里排第几（0..1，并列算半个）。"""
        lo_i = max(0, i - w)
        hi_i = min(n, i + w + 1)
        pool = sorted(peaks[j] for j in range(lo_i, hi_i) if j != i)
        if not pool:
            return 0.5
        value = peaks[i]
        less = bisect.bisect_left(pool, value - 1e-9)
        eq = bisect.bisect_right(pool, value + 1e-9) - less
        return (less + 0.5 * eq) / len(pool)

    out: list[list] = []
    for i, cue in enumerate(cues):
        flag = 1 if len(cue) > 4 and cue[4] else 0
        # v12：第 7 位的音内相位串原样带过去（重音要占第 6 位）。
        extra = [x for x in cue[5:] if isinstance(x, str)]
        if span <= 0:
            # 没有包络就没有依据，别把整首歌压成轻音：保持中性。
            out.append([cue[0], cue[1], cue[2], cue[3], flag, 0.5] + extra)
            continue
        t = min(1.0, max(0.0, (raw[i] - lo2) / spread))
        t = t ** 1.15
        # 局部名次：在前后各四个字里排第几。全曲归一化在副歌里会把一整段
        # 压成同一个值，相邻字之间看不出轻重；掺一点「这一句里谁更响」的
        # 名次，轻音和重音挨在一起也分得开。0.4 是实测既不来回跳、又能
        # 把相邻字的差拉开一半的位置（0.5 以上就会一重一轻地抖）。
        t = 0.6 * t + 0.4 * _local_rank(i)
        out.append([cue[0], cue[1], cue[2], cue[3], flag, round(t, 2)] + extra)
    return out


def _vocal_span_end(env, start: float, limit: float, *,
                    thr: float = 0.14, max_gap: float = 0.55,
                    start_window: float = 0.80) -> float:
    """干声从 ``start`` 起一直唱到哪一秒；没有包络/没有声音时返回 0。

    LRC 的行结束时间只是「下一行什么时候开始」，录音里最后一个字常常还
    拖着长音（或者 ラララ、副歌延长），这一行真正收声的时刻要靠包络量。
    """
    amp = list((env or {}).get("amp") or [])
    hop = float((env or {}).get("hop") or 0.0)
    if not amp or hop <= 0 or limit <= start:
        return 0.0
    i0 = max(0, int(start / hop))
    i1 = min(len(amp), int(limit / hop) + 1)
    last = -1
    gap = 0.0
    for i in range(i0, i1):
        if amp[i] >= thr:
            last, gap = i, 0.0
        elif last >= 0:
            gap += hop
            if gap > max_gap:
                break
        elif (i - i0) * hop > start_window:
            # 行尾之后半天都没声音：这是真的间奏，不是拖长音。
            break
    if last < 0:
        return 0.0
    return min(limit, (last + 1) * hop)


def _line_segments(count: int, start: float, end: float,
                   onsets: list[float]) -> list[tuple[float, float]]:
    """一行里的 ``count`` 个字 → ``count`` 段时间，尽量对齐干声的音节起音。

    旧版把整行按字数均分，再把边界吸附到最近的起音上。均分只在「每个字
    一样长」时成立；真实演唱里常有长音——副歌最后一句可能有一半时间都在
    拖同一个字。均分会把长音摊给后面的字，结果声音还在「啊——」的时候，
    嘴已经换成了别的元音，听起来就是「长音发音不标准」。

    这里把起音当音节边界直接切分：
      · 起音比字多（装饰音/颤音也被算成起音）→ 合并最短的相邻段，
        长音那一整段保持完整，不会被切碎；
      · 起音比字少（连音/滑音）→ 切分最长的段；
      · 一个起音都没有 → 均分，行为退回旧版。
    """
    if count <= 0:
        return []
    if end <= start:
        end = start + max(0.2, 0.25 * count)
    cand = [float(start)]
    for value in onsets or ():
        try:
            at = float(value)
        except (TypeError, ValueError):
            continue
        # 边界太密会让同一个字被切成一闪而过的口型；离行尾太近的起音
        # 多半是这一句最后一个字本身，留给收尾段。
        if at < cand[-1] + 0.07 or at > end - 0.05:
            continue
        cand.append(at)
    cand.append(float(end))
    segs = [[cand[i], cand[i + 1]]
            for i in range(len(cand) - 1) if cand[i + 1] > cand[i]]
    if not segs:
        segs = [[float(start), float(end)]]
    # 合并最短的相邻段：被合并掉的是最不像音节边界的那个起音。
    while len(segs) > count:
        best = min(
            range(len(segs) - 1),
            key=lambda i: (segs[i][1] - segs[i][0]) + (segs[i + 1][1] - segs[i + 1][0]))
        segs[best][1] = segs[best + 1][1]
        del segs[best + 1]
    # 起音不够：切最长的那段（通常是漏检的连音），仍然保持时间顺序。
    while len(segs) < count:
        best = max(range(len(segs)), key=lambda i: segs[i][1] - segs[i][0])
        a, b = segs[best]
        if b - a < 0.09:
            break
        parts = min(count - len(segs) + 1, max(2, int(round((b - a) / 0.35))))
        if parts < 2:
            break
        edges = [a + (b - a) * k / parts for k in range(parts + 1)]
        segs[best:best + 1] = [[edges[k], edges[k + 1]] for k in range(parts)]
    if len(segs) != count:
        step = (end - start) / count
        segs = [[start + i * step, start + (i + 1) * step] for i in range(count)]
    return [(round(a, 3), round(b, 3)) for a, b in segs]


def build_timed_cues(lines, duration: float, *, onsets=None,
                     env=None) -> list[list]:
    """``[{"at_s": 12.0, "text": "…"}]`` → cues using the real line timestamps.

    「为她所听」识别出的歌词带每行的起唱时间，比按字数铺满整首准得多：
    行与行的边界是真时间。行内不再均分，而是用干声的音节起音切段
    （见 ``_line_segments``），长音会被完整地留给它真正的那个字。
    传了 ``env``（干声响度包络）时，还会量出这一行真正唱到哪一秒，
    拖长音的尾段不会被下一行的行首时间截断。
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
    onset_list = sorted(float(x) for x in (onsets or []) if x is not None)
    total = float(duration or 0.0)
    # 感知转写偶尔会把尾奏/和声又拆成一行，时间戳落在曲长之外（本例：144.09s 的歌
    # 有一行 at_s=152.0）。这些字根本不会被唱到，却会被当成「最后一行」硬塞进
    # at+2s——十几个字挤进 0.6s，桥上一秒抖十几次。直接丢掉，不参与排布。
    if total > 0:
        timed = [(at, text) for at, text in timed if at < total]
        if not timed:
            return []
    cues: list[list] = []
    for index, (at, text) in enumerate(timed):
        if index + 1 < len(timed):
            limit = timed[index + 1][0]
        elif total > at:
            limit = total
        else:
            limit = at + max(2.0, len(text) * 0.35)
        if limit <= at:
            limit = at + max(1.0, len(text) * 0.3)
        units = _line_cells(text)
        if not units:
            continue
        # 行与行之间常有几秒间奏。真按「唱到下一行」算，这一行最后一个字会被拉长
        # 成好几秒的长音，口型和歌词就彻底对不上了——所以先按字数给一个上限。
        cap = max(1.2, len(units) * 0.42)
        sung_end = min(limit, at + cap)
        if env and limit > at + 0.10:
            # 干声量到的真实收声点。它通常比「按字均分」长（长音），
            # 但不会越过下一行；量不出来（整句静音）才退回上限。
            hold_to = _vocal_span_end(env, at, limit)
            if hold_to > at + 0.30:
                sung_end = min(limit, hold_to)
        if env and onset_list:
            segs = _line_segments(len(units), at, sung_end, onset_list)
        else:
            step = (sung_end - at) / len(units)
            segs = [(round(at + pos * step, 3), round(at + (pos + 1) * step, 3))
                    for pos in range(len(units))]
        for pos, cell in enumerate(units):
            begin, stop = segs[pos]
            if stop <= begin:
                stop = begin + 0.05
            row = [cell[0], cell[1], round(begin, 3), round(stop, 3)]
            # v4：句尾标记。LRC 的行与行往往首尾相接（间隙为 0），
            # 光看时间轴永远没有换气口；这里显式标出每行最后一个字，
            # 由桥在句尾做一次短暂收口。标记不影响唱字本身。
            row.append(1 if pos == len(units) - 1 else 0)
            # v12：第 7 位是可选的音内相位串（声母:占比:韵尾:占比）。
            # 位置固定在最后，句尾标记/重音仍然按老位置读。
            tag = _phase_tag(cell[2], cell[3], cell[4], cell[5])
            if tag:
                row.append(tag)
            cues.append(row)
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
    units_per_line = [_line_cells(line) for line in lines]
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
        for index, cell in enumerate(units):
            begin = cursor + index * step
            row = [cell[0], cell[1], round(begin, 3), round(begin + step, 3)]
            row.append(1 if index == len(units) - 1 else 0)   # 句尾：桥在这里收一下嘴
            tag = _phase_tag(cell[2], cell[3], cell[4], cell[5])
            if tag:
                row.append(tag)
            cues.append(row)
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
        # v13：曲长之外的行（转写把尾奏/和声又拆成一句）不再塞进排布——
        # 那会把十几个字压进 0.6s，桥上一秒抖十几次。旧缓存整批作废。
        # v14：中性元音 / schwa 从 S 改判 Z（嘴半张），旧缓存口型偏闭，重算。
        "v": 14,
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
                and cached.get("lhash") == lyrics_key(lyrics) \
                and cached.get("v") == 14:
            return cached
    payload = payload_for(wav, lyrics, lang=lang, duration=duration)
    write_sidecar(payload, wav)
    return payload


# ---------------- 整首歌的口型包（流水线算一次，唱的时候直接读） ----------------

def track_lyrics(track: dict) -> tuple[str, list, str]:
    """曲目记录 → ``(歌词全文, 带 at_s 的行, 来源标签)``。"""
    track = track or {}
    timed: list = []
    source = ""
    for song in (track.get("lyrics_from_perception") or []):
        if isinstance(song, dict) and song.get("lines"):
            timed = song["lines"]
            source = "perception_timed"
            break
    official = track.get("official_lyrics")
    if isinstance(official, dict) and official.get("rejected"):
        official = None   # 和人声对不上、已拒用的网上歌词：当作没有
    if not timed and isinstance(official, dict) and isinstance(official.get("timed"), list):
        timed = official["timed"]
        # 存下来的官方歌词带着抓取来源（lrclib.net / qq …），标签沿用，
        # 这样处理期和播放期算出来的 source 完全一致。
        source = str(official.get("source") or "official")
    lyrics = str(track.get("lyrics") or "")
    if not lyrics.strip():
        preview = track.get("lyrics_preview") or []
        if isinstance(preview, list):
            lyrics = "\n".join(str(x) for x in preview if str(x).strip())
    if not lyrics.strip() and isinstance(official, dict):
        off_lines = official.get("lines")
        if isinstance(off_lines, list):
            lyrics = "\n".join(str(x) for x in off_lines if str(x).strip())
    if not lyrics.strip() and timed:
        lyrics = "\n".join(str(row.get("text") or "") for row in timed
                           if isinstance(row, dict))
    return lyrics, timed, source


# ---------------- 歌词情绪（每行的「意思」→ VAD） ----------------
# 口型轨只回答「这个字怎么张嘴」；表情还需要「这一句在说什么」。
# 这里用一个双语词表给每行打分：正负（valence）、唤醒（arousal）、掌控
# （dominance）。不是语义模型，但歌词的情绪词密度很高（爱/泪/笑/孤独…），
# 词表够用；置信度低的行（没命中任何词）交给音频特征去决定。
_MOOD_POS = ("笑", "开心", "快乐", "甜", "美", "幸", "温暖", "拥抱", "希望", "梦",
             "爱", "喜欢", "阳光", "彩虹", "星", "飞", "自由", "一起", "永远", "勇",
             "相信", "好", "光", "花", "春", "心跳", "温柔")
_MOOD_NEG = ("哭", "泪", "痛", "伤", "别离", "孤独", "寂寞", "遗忘", "再见", "碎",
             "冷", "雨", "错", "空", "失去", "后悔", "恨", "怕", "黑暗", "逃", "谎",
             "累", "等", "远", "走", "死", "苦")
_MOOD_HI = ("喊", "燃", "烧", "冲", "狂", "爆", "跳", "奔跑", "大声", "高", "一起",
            "不停", "疯", "尖叫")
_MOOD_LO = ("轻轻", "安静", "慢慢", "温柔", "静静", "悄悄", "沉默", "缓缓")
_MOOD_POS_EN = ("love", "smile", "happy", "joy", "dream", "light", "sunshine",
                "beautiful", "together", "forever", "shine", "sky", "fly", "free",
                "sweet", "warm", "heart", "hope", "laugh", "dance", "sing", "young",
                "gold", "star", "bright")
_MOOD_NEG_EN = ("cry", "tear", "pain", "hurt", "lonely", "alone", "goodbye", "cold",
                "rain", "broken", "sorry", "lose", "lost", "empty", "sad", "miss",
                "far", "never", "die", "lie", "tired", "wait", "dark", "fear", "scared")
_MOOD_HI_EN = ("scream", "shout", "burn", "fire", "run", "fight", "loud", "louder",
               "jump", "tonight", "party", "wild", "crazy", "alive")
_MOOD_LO_EN = ("quiet", "softly", "slow", "calm", "gentle", "whisper", "slowly",
               "still")


def _mood_hits(low: str, words) -> int:
    total = 0
    for word in words:
        if word.isascii():
            total += len(re.findall(r"\b" + re.escape(word) + r"\b", low))
        else:
            total += low.count(word)
    return total


def _unit(x: float) -> float:
    return max(-1.0, min(1.0, float(x)))


def line_mood(text: str) -> tuple[float, float, float, float]:
    """一行歌词 → ``(v, a, d, conf)``，全部在 -1..1，conf 是 0..1 的置信度。"""
    low = str(text or "").lower()
    if not low.strip():
        return (0.0, 0.0, 0.0, 0.0)
    pos = _mood_hits(low, _MOOD_POS) + _mood_hits(low, _MOOD_POS_EN)
    neg = _mood_hits(low, _MOOD_NEG) + _mood_hits(low, _MOOD_NEG_EN)
    hi = _mood_hits(low, _MOOD_HI) + _mood_hits(low, _MOOD_HI_EN)
    lo = _mood_hits(low, _MOOD_LO) + _mood_hits(low, _MOOD_LO_EN)
    ex = low.count("!") + low.count("！")
    ell = low.count("…") + low.count("...")
    q = low.count("?") + low.count("？")
    hits = pos + neg + hi + lo + ex + ell + q
    if hits <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    v = (pos - neg) * 1.6 / (pos + neg + 2.0)
    a = (0.30 + 0.45 * hi - 0.30 * lo + 0.12 * ex - 0.10 * ell + 0.08 * q)
    d = (0.25 + 0.35 * hi + 0.30 * max(v, 0.0) - 0.25 * max(-v, 0.0))
    conf = min(1.0, 0.35 + 0.30 * hits)
    return (round(_unit(v), 3), round(_unit(a), 3), round(_unit(d), 3),
            round(min(1.0, conf), 2))


def line_moods(timed: list, duration: float) -> list[list]:
    """带时间戳的歌词行 → ``[[start, end, v, a, d, conf], ...]``（给桥用）。"""
    rows: list[list] = []
    for index, row in enumerate(timed or []):
        if not isinstance(row, dict):
            continue
        try:
            at = float(row.get("at_s"))
        except (TypeError, ValueError):
            continue
        if at < 0:
            continue
        if index + 1 < len(timed):
            try:
                stop = float(timed[index + 1].get("at_s"))
            except (TypeError, ValueError, AttributeError):
                stop = at + 4.0
        else:
            stop = float(duration or 0.0) if duration and duration > at else at + 4.0
        if stop <= at:
            stop = at + 1.0
        v, a, d, conf = line_mood(str(row.get("text") or ""))
        if conf <= 0:
            continue
        rows.append([round(at, 3), round(stop, 3), v, a, d, conf])
    return rows


def build_track_payload(track: dict, *, stem: str = "", duration: float | None = None,
                        lrc_fetcher=None, asr_fetcher=None, web_fetcher=None) -> dict:
    """一首歌的完整口型包：逐字 cue 轨 + 起音对齐 + （没有歌词时）响度包络。

    处理曲目时跑一次、存成 ``lips.json``；唱的时候直接拿来用，不再现场
    抓歌词、算包络、做对齐——那几秒的空白正是「嘴不动」的来源。

    ``lrc_fetcher(track) -> (lrc_text, source)`` 由插件提供（带网络和缓存），
    流水线和播放路径共用同一份逻辑，算出来的口型一定一致。
    """
    track = track or {}
    lyrics, timed, lrc_source = track_lyrics(track)
    official_saved: dict = {}
    # 原创歌（用户自己写的歌词）不去网上搜：按「原创_xxxx」这种名字只会搜到别人的歌
    is_original = str(track.get("origin") or "") == "ace" and bool(lyrics.strip())
    if not timed and lrc_fetcher is not None and not is_original:
        try:
            lrc, how = lrc_fetcher(track)
        except Exception:  # noqa: BLE001 - 网络失败只是回到「估计」路径
            lrc, how = None, ""
        if lrc:
            parsed = parse_lrc(lrc)
            if parsed:
                timed = parsed
                lrc_source = how or "lrc"
                lyrics = "\n".join(row["text"] for row in parsed)
                official_saved = {"source": lrc_source, "lrc": lrc,
                                  "timed": parsed,
                                  "lines": [row["text"] for row in parsed]}

    stem = str(stem or "")
    stem_ok = bool(stem) and Path(stem).is_file()
    # 兜底：仍然没有带时间的歌词 → 用「为她所听」的歌声识别模型识别干声。
    #   有用户歌词（原创歌）：文字用用户写的，识别只给时间；
    #   没有任何歌词：直接用识别出的句子（标注 asr，可能有错字）。
    if not timed and stem_ok and asr_fetcher is not None:
        try:
            asr = asr_fetcher(track, stem) or {}
        except Exception:  # noqa: BLE001 - 识别失败只是回到估计路径
            asr = {}
        asr_lines = list(asr.get("lines") or [])
        if asr_lines:
            if lyrics.strip():
                try:
                    env_for_align = get_or_build_envelope(stem)
                except Exception:  # noqa: BLE001
                    env_for_align = None
                aligned = align_user_lyrics(lyrics, asr_lines, float(duration or 0.0), env=env_for_align)
                if aligned:
                    timed = aligned
                    lrc_source = "user+asr"
                    official_saved = {"source": "user+asr", "timed": aligned,
                                      "lines": [r["text"] for r in aligned],
                                      "asr_model": asr.get("model")}
            else:
                env_asr = None
                try:
                    env_asr = get_or_build_envelope(stem)
                except Exception:  # noqa: BLE001
                    env_asr = None
                asr_lines = clean_asr_lines(asr_lines, env_asr) or asr_lines
                fused = []
                fuse_src = ""
                miss = asr_missing_frac(asr_lines, env_asr) if env_asr else 0.0
                if web_fetcher is not None and miss >= 0.12:
                    # 识别缺得严重（有人声的地方没认出句子）：网上搜歌词做辅助，识别为主
                    try:
                        cands = web_fetcher(track, asr_lines) or []
                    except Exception:  # noqa: BLE001
                        cands = []
                    best_tm = 0.0
                    for lrc_c, how_c in cands:
                        ref = parse_lrc(lrc_c) if lrc_c else []
                        tm = text_match_rate([r["text"] for r in asr_lines], [r["text"] for r in ref]) or 0.0
                        if tm < 0.2 or tm <= best_tm:
                            continue
                        got = fuse_asr_reference(asr_lines, ref, env_asr, float(duration or 0.0))
                        if got:
                            fused, fuse_src, best_tm = got, how_c, tm
                if fused:
                    timed = [{"at_s": r["at_s"], "text": r["text"]} for r in fused]
                    lrc_source = "asr+web"
                    lyrics = "\n".join(r["text"] for r in timed)
                    official_saved = {"source": "asr+web", "timed": timed,
                                      "lines": [r["text"] for r in timed], "web_source": fuse_src,
                                      "asr_missing": round(miss, 2),
                                      "fused_from": {k: sum(1 for r in fused if r["from"] == k)
                                                     for k in ("asr", "asr+ref", "ref")},
                                      "asr_model": asr.get("model"), "asr_language": asr.get("language")}
                else:
                    timed = [{"at_s": r["at_s"], "text": r["text"]} for r in asr_lines]
                    lrc_source = "asr"
                    lyrics = "\n".join(r["text"] for r in timed)
                    official_saved = {"source": "asr", "timed": timed,
                                      "lines": [r["text"] for r in timed],
                                      "asr_missing": round(miss, 2),
                                      "asr_model": asr.get("model"), "asr_language": asr.get("language")}
    env: dict = {}
    align = 0.0
    if stem_ok:
        try:
            env = get_or_build_envelope(stem)
        except Exception:  # noqa: BLE001
            env = {}
    if timed and env.get("amp"):
        try:
            align = align_offset(env, timed)
        except Exception:  # noqa: BLE001
            align = 0.0
        if abs(align) >= 0.25:
            timed = [dict(row, at_s=round(float(row["at_s"]) + align, 3))
                     for row in timed]
        # 网上抓来的歌词可能根本不是这首（标题太泛、同名歌）：行首几乎都对不上人声起音就不用，
        # 错的歌词比没有歌词更糟（字幕乱跳、嘴对不上）。本地 .lrc 和「为她所听」认出来的不判。
        # 平台原版歌词（为她所听查到的）也要过匹配检查：同名歌/不同版本时间会对不上
        if lrc_source and not str(lrc_source).startswith(("local:", "perception_timed", "asr", "user+asr", "asr+web")):
            rate = lyric_match_rate(env, timed)
            if rate is not None and rate < 0.35 and stem_ok and asr_fetcher is not None:
                # 时间对不上不一定是别的歌（比如录音里有一段原版歌词没写的开头）：
                # 用歌声识别的文字再核一次，文字明显是这首就照用（识别有错字，所以看二字片段重合）
                try:
                    asr_chk = asr_fetcher(track, stem) or {}
                except Exception:  # noqa: BLE001
                    asr_chk = {}
                tm = text_match_rate([r.get("text") for r in (asr_chk.get("lines") or []) if isinstance(r, dict)],
                                     [r.get("text") for r in timed if isinstance(r, dict)])
                if tm is not None and tm >= 0.2:
                    rate = None
            if rate is not None and rate < 0.35:
                timed = []
                lyrics = ""
                official_saved = {"rejected": True, "source": lrc_source, "match_rate": round(rate, 2)}
                lrc_source = ""
                # 第四十八轮：网上歌词拒用后不能就此没词——改用「为她所听」歌声识别出的句子
                # （以前这里直接落到「估计」，识别好的 51 句白白丢掉，舞台和口型都没有歌词）
                if stem_ok and asr_fetcher is not None:
                    try:
                        asr_fb = asr_fetcher(track, stem) or {}
                    except Exception:  # noqa: BLE001
                        asr_fb = {}
                    asr_lines_fb = list(asr_fb.get("lines") or [])
                    if asr_lines_fb:
                        asr_lines_fb = clean_asr_lines(asr_lines_fb, env) or asr_lines_fb
                        timed = [{"at_s": r["at_s"], "text": r["text"]} for r in asr_lines_fb]
                        lrc_source = "asr"
                        lyrics = "\n".join(r["text"] for r in timed)
                        official_saved = {"source": "asr", "timed": timed,
                                          "lines": [r["text"] for r in timed],
                                          "web_rejected": official_saved,
                                          "asr_model": asr_fb.get("model"),
                                          "asr_language": asr_fb.get("language")}

    dur = float(duration or 0.0) or float((env or {}).get("duration") or 0.0)
    if dur <= 0 and stem_ok:
        dur = song_duration(stem)
    cues = build_timed_cues(timed, dur,
                            onsets=vocal_syllable_onsets(env) if env.get("amp") else None,
                            env=env if env.get("amp") else None)
    if cues and lrc_source.startswith("perception"):
        source = "perception_timed"
    elif cues and lrc_source:
        source = "lrc:" + lrc_source
    else:
        source = "estimate"
    if not cues and not timed and lyrics.strip():
        # 一行时间戳都没有（录下来的歌、也没抓到 LRC）：退到「按字数铺满整首」，
        # 至少嘴会跟着歌词字数动，而不是只剩包络开合。
        guess = dur or song_duration(stem)
        cues = build_cues(lyrics, guess)
        if cues:
            dur = dur or guess
            source = "estimate"
    # 句尾标记只有在干声真的掉下去时才保留：连着唱的两句之间不该闭嘴。
    cues = _breath_marks(cues, env)
    # 每个字的轻重：桥按它调张口幅度和起手速度，重音张得开、起得快。
    cues = _apply_stress(cues, env)
    mode = "cues" if cues else ("envelope" if env.get("amp") else "none")
    payload = {
        # v3：长音符/促音/表外字不再单独给 rest（以前唱到就在句中闭一下嘴），
        # 改成沿用邻字的口型。版本号进缓存，旧曲目的 lips.json 会自动重算。
        # v4：每行最后一个 cue 带句尾标记（第 5 位 = 1），桥在句尾做一次
        # 换气收口——LRC 行首尾相接时，光靠时间轴空隙永远不会闭嘴。
        # v5：行尾拖长音用干声包络补延续段，长音里嘴不再提前闭上。
        # v6：cue 轨也带干声包络——桥用它调制张口度（唱得响张得大、轻的地方
        #     收一点），不再只有 A/O/E/I/U 五个固定张度，嘴才有微动作。
        # v7：每个 cue 第 6 位是重音强度 0..1（包络峰值 + 邻字对比），桥用它
        #     决定张口幅度和起手快慢——重音咬得开、轻音收着唱。
        # v8：重音掺入「这一句里谁更响」的局部名次。
        # v9：包络/对齐只允许来自干净人声（payload 里带 stem_kind）；
        #     用成品混音算出来的旧缓存全部作废重算——伴奏鼓点会让嘴乱张。
        # v10：行内改用干声音节起音切段（长音不再被均分给后面的字），
        #      行尾拖长音直接由最后一个字撑满；桥同时用干声的实时音高/
        #      颤音给长音做微开合。旧缓存（均分轨）全部作废重算。
        # v11：每一行的情绪（v/a/d + 置信度）随口型轨一起发出去，桥用它把
        #      「这句在唱什么」掺进表情；英文口型位按拼写规则重新计数
        #      （去词尾哑 e、词首 y 当辅音、双字母组合取口型），中文 e/舌尖
        #      元音改判 S（中性口型）。旧缓存全部作废重算。
        # v12：每个 cue 多带一段音内相位（第 7 位，可选）——声母的口型
        #      （b/p/m 闭唇、f 唇齿、w 圆唇）和韵尾的收势（n/ng、ai/ao），
        #      桥按占比在字内插值。旧缓存（整字一个口型）全部作废重算。
        # v13：曲长之外的行（转写把尾奏/和声又拆成一句）直接丢掉——以前会被
        #      当成「最后一行」压进 0.6s，十几个字挤在一起，桥一秒抖十几次。
        # v14：中性元音 / schwa 从 S 改判 Z（嘴半张），旧缓存口型偏闭，重算。
        "v": 14,
        "track_id": str(track.get("id") or ""),
        "song": str(track.get("title") or ""),
        "duration": round(dur, 3),
        "lang": str(track.get("vocal_language") or ""),
        "source": source,
        "mode": mode,
        "align": round(float(align), 2),
        "lyrics_key": lyrics_key(lyrics) if lyrics.strip() else "",
        "stem": stem if stem_ok else "",
        "cues": cues,
        "moods": line_moods(timed, dur),
        # 有歌词时包络也带上：不再是「没歌词才用」，而是作为张口度的调制信号。
        "hop": float((env or {}).get("hop") or 0.05),
        "env": list((env or {}).get("amp") or []),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if official_saved:
        payload["official_lyrics"] = official_saved
    return payload

