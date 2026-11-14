"""歌词语义：本地 Ollama 逐行分析 + 本地关键词兜底。

一首歌调用一次（一次请求内给出整首逐行情绪/意象/关键词/人称），使用 Ollama
原生 ``/api/chat`` 的 ``format``（JSON Schema）做结构化输出。

设计原则：
- **绝不阻塞演唱**。调用方（worker）在后台进程里调用本模块；任何异常、超时、
  解析失败都退回兜底，``analyze()`` 本身不抛异常，返回值一定带 ``source``。
- 兜底 = P1 既有行为：worker 已经把 ``lipsync.line_moods()``（内部用
  ``lipsync.line_mood()`` 关键词表）算好放进 job，``source == "fallback"`` 时
  director 完全走原路径，不读这里的 per-line 结果。
- 结果落盘缓存（``semantics.json``），缓存键 = 提示词版本 + 模型名 + 歌词全文；
  命中后同输入同结果，直接决定 show.json 的确定性。
- ``input_hash`` 的来源标记由 ``tag_for()`` 给出（``llm:<模型>`` / ``fallback``），
  worker 生成时与宿主缓存判断时都读同一份侧车文件，保证两端算出来的指纹一致。

模型/端点/超时都是参数，也可用环境变量覆盖：
``NEKO_SEMANTICS_ENDPOINT`` / ``NEKO_SEMANTICS_MODEL`` / ``NEKO_SEMANTICS_TIMEOUT``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 参数默认值

DEFAULT_ENDPOINT = os.environ.get(
    "NEKO_SEMANTICS_ENDPOINT", "http://127.0.0.1:11434/api/chat")
# D2 决策的对话模型是 nymphaea-rp 14B（32k 上下文）。本机安装的标签带量化后缀
# （``nymphaea-rp:14b-q5``），没有裸 ``:14b``；模型名做成参数，换模型不用改代码。
DEFAULT_MODEL = os.environ.get("NEKO_SEMANTICS_MODEL", "nymphaea-rp:14b-q5")
DEFAULT_TIMEOUT = float(os.environ.get("NEKO_SEMANTICS_TIMEOUT") or 120.0)
NUM_CTX = int(os.environ.get("NEKO_SEMANTICS_NUM_CTX") or 32768)
PROMPT_VERSION = "t2a-1"
CACHE_VERSION = 1
# 兜底结果只在短时间内复用：超过这个秒数再唱会重试一次 LLM（Ollama 可能刚起来）。
FALLBACK_RETRY_S = 600.0
CACHE_NAME = "semantics.json"

EMOTIONS = ("joy", "tender", "longing", "sad", "proud", "determined", "playful")
ADDRESSES = ("self", "you", "world")

# 逐行输出的紧凑 schema：短键 + 整数编码，把 96 行歌的输出压到 3k token 以内。
_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "e": {"type": "integer", "minimum": 0, "maximum": 6},
        "v": {"type": "number"},
        "a": {"type": "number"},
        "i": {"type": "number"},
        "k": {"type": "array", "items": {"type": "string"}},
        "d": {"type": "integer", "minimum": 0, "maximum": 2},
        "m": {"type": "string"},
    },
    "required": ["e", "v", "a", "i", "k", "d", "m"],
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "theme": {"type": "string"},
        "imagery": {"type": "array", "items": {"type": "string"}},
        "palette": {"type": "string"},
        "lines": {"type": "array", "items": _LINE_SCHEMA},
    },
    "required": ["theme", "imagery", "palette", "lines"],
}

_SENTENCE = re.compile(r"[^。！？!?…\n]+[。！？!?…]?")
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


# ---------------------------------------------------------------- 缓存 / 标记

def text_key(texts) -> str:
    raw = "\n".join(str(t or "") for t in (texts or []))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_key(texts, model: str) -> str:
    raw = f"{PROMPT_VERSION}|{model}|{text_key(texts)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def tag(source: str, model: str = "") -> str:
    """input_hash 里的语义来源标记。"""
    if source == "llm":
        return f"llm:{model or DEFAULT_MODEL}"
    return "fallback" if source else ""


def read_cache(path) -> dict | None:
    if not path:
        return None
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    return data


def tag_for(path) -> str:
    """宿主侧（``_show_cached``）用：读侧车文件拿来源标记；没有就空串。"""
    data = read_cache(path)
    if not data:
        return ""
    return str(data.get("tag") or "")


def _write_cache(path, payload: dict) -> None:
    if not path:
        return
    try:
        folder = os.path.dirname(str(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = str(path) + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, str(path))
    except OSError:
        pass


# ---------------------------------------------------------------- 提示词

def _prompt(track: dict, texts: list) -> tuple[str, str]:
    title = str((track or {}).get("title") or "")
    artist = str((track or {}).get("artist") or "")
    numbered = "\n".join(f"{i}. {str(t or '').strip()}"
                         for i, t in enumerate(texts) if str(t or "").strip())
    system = (
        "你是歌曲歌词分析师，只输出 JSON，不要解释，不要 Markdown。"
        "theme 必须是一句自然的中文主题句（不超过 40 个字，只写一句）。"
        "imagery 给 2~4 个简短意象词；palette 给 6~12 个字的主色调描述。"
        "lines 必须与输入的歌词行一一对应，行数、顺序完全一致，不得合并或遗漏。"
        "e 是情绪代码：0=joy 1=tender 2=longing 3=sad 4=proud 5=determined 6=playful；"
        "v=愉悦度(-1..1) a=唤醒度(-1..1) i=强度(0..1)；k 给 0~2 个关键词；"
        "d 是人称：0=self 1=you 2=world；m 给 1~2 个字的意象。"
    )
    user = (f"歌曲：{title}\n歌手：{artist}\n\n歌词（编号. 文本）：\n{numbered}\n\n"
            f"请输出 JSON：theme/imagery/palette/lines（共 {len(texts)} 行）。")
    return system, user


# ---------------------------------------------------------------- 调用

def _clean_sentence(text, fallback: str = "") -> str:
    raw = str(text or "").strip().replace("\r", " ").replace("\n", " ")
    raw = re.sub(r"\s+", " ", raw)
    match = _SENTENCE.search(raw)
    line = (match.group(0) if match else raw).strip()
    line = line.strip("　 ")
    if len(line) > 60:
        line = line[:60]
    return line or fallback


def _num(value, low: float, high: float, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return max(low, min(high, out))


def _normalize_line(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    try:
        code = int(item.get("e"))
    except (TypeError, ValueError):
        code = 1
    code = max(0, min(len(EMOTIONS) - 1, code))
    try:
        addr = int(item.get("d"))
    except (TypeError, ValueError):
        addr = 0
    addr = max(0, min(len(ADDRESSES) - 1, addr))
    kws = []
    for kw in item.get("k") or []:
        word = str(kw or "").strip()
        if word and word not in kws and len(word) <= 12:
            kws.append(word)
        if len(kws) >= 3:
            break
    return {
        "emotion": EMOTIONS[code],
        "valence": round(_num(item.get("v"), -1.0, 1.0), 3),
        "arousal": round(_num(item.get("a"), -1.0, 1.0), 3),
        "intensity": round(_num(item.get("i"), 0.0, 1.0, 0.5), 3),
        "keywords": kws,
        "address": ADDRESSES[addr],
        "image": str(item.get("m") or "").strip()[:24],
    }


def _parse_content(content: str, count: int) -> dict | None:
    text = _FENCE.sub("", str(content or "").strip())
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("lines")
    if not isinstance(rows, list) or not rows:
        return None
    lines = []
    for i in range(count):
        lines.append(_normalize_line(rows[i]) if i < len(rows) else None)
    got = sum(1 for item in lines if item)
    if got < max(1, int(count * 0.6)):
        return None
    imagery = []
    for item in data.get("imagery") or []:
        word = str(item or "").strip()
        if word and word not in imagery:
            imagery.append(word[:16])
        if len(imagery) >= 4:
            break
    return {
        "theme": _clean_sentence(data.get("theme"), ""),
        "imagery": imagery,
        "palette_hint": str(data.get("palette") or "").strip()[:40],
        "lines": lines,
        "coverage": round(got / float(count), 3) if count else 0.0,
    }


def _call_llm(texts: list, track: dict, *, endpoint: str, model: str,
              timeout: float) -> dict:
    system, user = _prompt(track, texts)
    count = len(texts)
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": RESULT_SCHEMA,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {
            "num_ctx": NUM_CTX,
            "temperature": 0.2,
            "seed": 42,
            "num_predict": min(8192, 160 + 90 * count),
        },
    }
    started = time.time()
    request = urllib.request.Request(
        endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=max(0.5, float(timeout))) as resp:
        raw = resp.read()
    payload = json.loads(raw.decode("utf-8", "replace"))
    content = (payload.get("message") or {}).get("content")
    parsed = _parse_content(content, count)
    if parsed is None:
        raise ValueError("LLM 返回内容不是可用的结构化 JSON")
    parsed.update({
        "latency_s": round(time.time() - started, 2),
        "prompt_tokens": payload.get("prompt_eval_count"),
        "eval_tokens": payload.get("eval_count"),
        "model": payload.get("model") or model,
        "endpoint": endpoint,
    })
    return parsed


# ---------------------------------------------------------------- 对外入口

def analyze(track: dict, lines: list, *, endpoint: str | None = None,
            model: str | None = None, timeout: float | None = None,
            cache_path=None, force: bool = False) -> dict:
    """分析整首歌的歌词语义；失败/超时返回兜底结果，绝不抛异常。

    返回::

        {"ok": bool, "source": "llm"|"fallback", "reason": str|None,
         "tag": "llm:<模型>"|"fallback",
         "theme": str, "imagery": [str], "palette_hint": str,
         "lines": [None|{emotion,valence,arousal,intensity,keywords,address,image}],
         "cache_hit": bool, "metrics": {...}}
    """
    endpoint = str(endpoint or DEFAULT_ENDPOINT)
    model = str(model or DEFAULT_MODEL)
    timeout = float(DEFAULT_TIMEOUT if timeout is None else timeout)
    texts = [str((row or {}).get("text") or "") for row in (lines or [])]
    key = cache_key(texts, model)
    empty = {
        "ok": False, "source": "fallback", "reason": "", "tag": tag("fallback"),
        "theme": "", "imagery": [], "palette_hint": "", "lines": [],
        "cache_hit": False, "key": key,
        "metrics": {"endpoint": endpoint, "model": model, "timeout_s": timeout},
    }
    if not [t for t in texts if t.strip()]:
        empty["reason"] = "没有歌词文本"
        return empty

    cached = read_cache(cache_path)
    if cached and not force and cached.get("key") == key:
        source = str(cached.get("source") or "")
        fresh = bool(cached.get("created_at")) and \
            (time.time() - float(cached.get("created_at") or 0)) < FALLBACK_RETRY_S
        if source == "llm" or fresh:
            result = dict(cached.get("result") or {})
            return {
                "ok": source == "llm",
                "source": source or "fallback",
                "reason": cached.get("reason"),
                "tag": str(cached.get("tag") or tag(source, model)),
                "theme": str(result.get("theme") or ""),
                "imagery": list(result.get("imagery") or []),
                "palette_hint": str(result.get("palette_hint") or ""),
                "lines": list(result.get("lines") or []),
                "cache_hit": True,
                "key": key,
                "metrics": dict(cached.get("metrics") or {}),
            }

    try:
        parsed = _call_llm(texts, track, endpoint=endpoint, model=model,
                           timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 兜底是契约要求，不往上抛
        reason = f"{type(exc).__name__}: {exc}"[:220]
        metrics = {"endpoint": endpoint, "model": model, "timeout_s": timeout}
        _write_cache(cache_path, {
            "version": CACHE_VERSION, "key": key, "source": "fallback",
            "tag": tag("fallback"), "reason": reason, "created_at": time.time(),
            "metrics": metrics, "result": {"theme": "", "imagery": [],
                                           "palette_hint": "", "lines": []},
        })
        return {**empty, "reason": reason, "cache_hit": False}

    metrics = {
        "endpoint": endpoint,
        "model": parsed.pop("model", model),
        "requested_model": model,
        "timeout_s": timeout,
        "latency_s": parsed.pop("latency_s", None),
        "prompt_tokens": parsed.pop("prompt_tokens", None),
        "eval_tokens": parsed.pop("eval_tokens", None),
        "coverage": parsed.pop("coverage", None),
    }
    _write_cache(cache_path, {
        "version": CACHE_VERSION, "key": key, "source": "llm",
        "tag": tag("llm", metrics["model"]), "created_at": time.time(),
        "metrics": metrics, "result": parsed,
    })
    return {
        "ok": True, "source": "llm", "reason": None,
        "tag": tag("llm", metrics["model"]),
        "theme": parsed.get("theme") or "",
        "imagery": list(parsed.get("imagery") or []),
        "palette_hint": parsed.get("palette_hint") or "",
        "lines": list(parsed.get("lines") or []),
        "cache_hit": False, "key": key, "metrics": metrics,
    }
