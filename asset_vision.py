"""第四十一轮：本地看图模型 → 设计结构描述（不是像素、不是照抄）→ 多张参考混合成新模板。

用户的思路（2026-11-17）：
  * 素材站的图下载进本地参考库，定期让本地视觉模型「看懂」它是什么样的设计——
    版式结构、层次、装饰元素的种类、卡片/边框的做法、氛围；
  * 不学具体设计（不记坐标、不复刻图形、不照搬换色），只抽象成「设计语法」；
  * 两三张参考的设计语法可以混合，形成新的模板，舞台用自己的程序绘制出来。

所以这里让视觉模型输出一份固定格式的 JSON「设计语法」：
  layout        版式：center/left_card/right_card/split/frame_full/diagonal/grid/collage/window
  layers        层次（从后到前）：如 ["gradient_bg","pattern","card","deco_front"]
  card          卡片做法：{shape: rounded/circle/ticket/polaroid/torn/none, border: none/thin/double/dashed/lace/glow, shadow: none/soft/hard, tilt: none/slight}
  deco          装饰元素种类（只记种类）：stars/hearts/ribbons/flowers/sparkles/bubbles/lines/dots/tape/stickers/clouds/notes/frames_lines/grid/checker/stripes
  pattern       背景纹理：none/dots/checker/stripes/grid/gingham/waves/halftone/noise
  palette_mood  配色气质：pastel/vivid/dark/mono/warm/cool/neon/vintage
  density       装饰密度 0~1
  symmetry      对称：none/mirror/radial
  typography    文字做法：outline/rounded/handwritten/serif/stamp/none
  keywords      3~6 个英文风格关键词
这些只是「类别 + 程度」，不含任何具体图形；混合时按类别取并集/抽样，得到新组合。
"""
from __future__ import annotations

import base64
import json
import random
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11435"
VISION_MODELS = ("qwen2.5vl:7b", "qwen2.5vl:3b", "llava:7b", "minicpm-v:8b")

ENUMS = {
    "layout": ["center", "left_card", "right_card", "split", "frame_full", "diagonal", "grid", "collage", "window"],
    "pattern": ["none", "dots", "checker", "stripes", "grid", "gingham", "waves", "halftone", "noise"],
    "palette_mood": ["pastel", "vivid", "dark", "mono", "warm", "cool", "neon", "vintage"],
    "symmetry": ["none", "mirror", "radial"],
    "typography": ["outline", "rounded", "handwritten", "serif", "stamp", "none"],
}
CARD = {"shape": ["rounded", "circle", "ticket", "polaroid", "torn", "none"],
        "border": ["none", "thin", "double", "dashed", "lace", "glow"],
        "shadow": ["none", "soft", "hard"], "tilt": ["none", "slight"]}
DECO = ["stars", "hearts", "ribbons", "flowers", "sparkles", "bubbles", "lines", "dots", "tape", "stickers",
        "clouds", "notes", "frames_lines", "grid", "checker", "stripes"]
CATEGORIES = ["frame", "background", "lines", "particles", "sticker", "text_plate", "overlay", "other"]
GENRES = ["ballad", "pop", "edm", "rock", "cute", "ancient", "jazz", "dark"]
SECTIONS = ["intro", "verse", "chorus", "bridge", "outro"]
LAYERS = ["gradient_bg", "solid_bg", "pattern", "photo_bg", "card", "frame", "deco_back", "deco_front", "light_leak", "text_band"]

PROMPT = (
    "You are a graphic-design analyst. Look at this stream overlay / background / frame asset and describe ONLY its "
    "abstract design grammar (structure and style categories), NOT its concrete artwork. Do not describe characters, "
    "logos or exact shapes. Answer with one JSON object and nothing else, using exactly these keys and allowed values:\n"
    '{"layout": one of ' + json.dumps(ENUMS["layout"]) + ',\n'
    ' "layers": list (back to front) from ' + json.dumps(LAYERS) + ',\n'
    ' "card": {"shape": one of ' + json.dumps(CARD["shape"]) + ', "border": one of ' + json.dumps(CARD["border"]) +
    ', "shadow": one of ' + json.dumps(CARD["shadow"]) + ', "tilt": one of ' + json.dumps(CARD["tilt"]) + '},\n'
    ' "deco": list from ' + json.dumps(DECO) + ',\n'
    ' "pattern": one of ' + json.dumps(ENUMS["pattern"]) + ',\n'
    ' "palette_mood": one of ' + json.dumps(ENUMS["palette_mood"]) + ',\n'
    ' "density": number 0..1,\n'
    ' "symmetry": one of ' + json.dumps(ENUMS["symmetry"]) + ',\n'
    ' "typography": one of ' + json.dumps(ENUMS["typography"]) + ',\n'
    ' "keywords": 3-6 english style words,\n'
    ' "category": what kind of asset this is, one of ' + json.dumps(CATEGORIES) + ',\n'
    ' "genres": which music genres it suits, list from ' + json.dumps(GENRES) + ',\n'
    ' "sections": which song sections it suits, list from ' + json.dumps(SECTIONS) + ',\n'
    ' "energy": number 0..1 (calm to energetic)}'
)


def available_model() -> str:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
            names = [m.get("name") for m in json.loads(r.read()).get("models", [])]
    except Exception:  # noqa: BLE001
        return ""
    for m in VISION_MODELS:
        if m in names:
            return m
    return ""


def _clean(d: dict) -> dict:
    """把模型回答规整到允许值（不认识的丢掉），保证只剩类别信息。"""
    out = {}
    for k, vals in ENUMS.items():
        v = str(d.get(k) or "").strip().lower()
        out[k] = v if v in vals else vals[0]
    card = d.get("card") if isinstance(d.get("card"), dict) else {}
    out["card"] = {k: (str(card.get(k) or "").lower() if str(card.get(k) or "").lower() in vals else vals[0])
                   for k, vals in CARD.items()}
    out["layers"] = [x for x in (d.get("layers") or []) if isinstance(x, str) and x.lower() in LAYERS][:6] or ["gradient_bg", "deco_front"]
    out["deco"] = sorted({x.lower() for x in (d.get("deco") or []) if isinstance(x, str) and x.lower() in DECO})[:8]
    try:
        out["density"] = max(0.0, min(1.0, float(d.get("density"))))
    except (TypeError, ValueError):
        out["density"] = .5
    out["keywords"] = [str(x).lower()[:20] for x in (d.get("keywords") or []) if isinstance(x, str)][:6]
    cat = str(d.get("category") or "").lower()
    out["category"] = cat if cat in CATEGORIES else "other"
    out["genres"] = [g for g in (str(x).lower() for x in (d.get("genres") or [])) if g in GENRES][:5] or ["pop"]
    out["sections"] = [g for g in (str(x).lower() for x in (d.get("sections") or [])) if g in SECTIONS][:5] or ["verse"]
    try:
        out["energy"] = max(0.0, min(1.0, float(d.get("energy"))))
    except (TypeError, ValueError):
        out["energy"] = .5
    return out


def describe(image_path, model: str = "", timeout: float = 180.0) -> dict | None:
    """本地视觉模型看一张图 → 设计语法 JSON（失败返回 None）。"""
    model = model or available_model()
    if not model:
        return None
    img = base64.b64encode(open(image_path, "rb").read()).decode("ascii")
    body = {"model": model, "prompt": PROMPT, "images": [img], "stream": False, "format": "json",
            "options": {"temperature": 0.2, "num_ctx": 4096}, "keep_alive": "2m"}
    req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    text = str(resp.get("response") or "")
    try:
        d = json.loads(text)
    except ValueError:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            d = json.loads(text[i:j + 1])
        except ValueError:
            return None
    return _clean(d) if isinstance(d, dict) else None


def blend(grammars: list, rng: random.Random | None = None) -> dict:
    """两三份设计语法混合成一个新模板：每个维度从参与者里各取一部分（不是取平均，也不是照抄任何一份）。"""
    rng = rng or random.Random()
    gs = [g for g in grammars if g]
    if not gs:
        return {}
    def pick(key):
        return rng.choice(gs)[key]

    out = {
        "layout": pick("layout"), "pattern": pick("pattern"), "palette_mood": pick("palette_mood"),
        "symmetry": pick("symmetry"), "typography": pick("typography"),
        "card": {k: rng.choice(gs)["card"][k] for k in CARD},
        "density": round(sum(g["density"] for g in gs) / len(gs) * rng.uniform(.8, 1.2), 2),
    }
    deco = set()
    for g in gs:
        if g["deco"]:
            deco.update(rng.sample(g["deco"], max(1, len(g["deco"]) // 2)))
    out["deco"] = sorted(deco)[:6]
    layers = []
    for g in gs:
        for x in g["layers"]:
            if x not in layers and (rng.random() < .6 or not layers):
                layers.append(x)
    order = {x: i for i, x in enumerate(LAYERS)}
    out["layers"] = sorted(layers, key=lambda x: order.get(x, 99))[:6]
    kw = []
    for g in gs:
        kw += g.get("keywords") or []
    out["keywords"] = sorted(set(kw))[:6]
    out["blend_of"] = len(gs)
    out["made_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out


FRAME_KINDS = ['ribbon', 'stitch', 'washi', 'starline', 'heartline', 'bubbleframe', 'filmstrip2', 'arcade', 'notebook',
               'postcard', 'stamp', 'neontube', 'glitchframe', 'cyberbrackets', 'goldleaf', 'artdeco', 'chinoise', 'cloudframe',
               'flowerwreath', 'pixel', 'comic', 'window', 'ticket', 'gradientring']


def frame_genome(g: dict, palette: list, seed: int) -> dict:
    """一份设计语法 → 一个边框基因（只取类别信息决定画法和参数；不含原图的任何形状）。"""
    rng = random.Random(seed)
    deco = set(g.get("deco") or [])
    card = g.get("card") or {}
    mood = g.get("palette_mood")
    kinds = []
    if card.get("shape") == "ticket":
        kinds.append("ticket")
    if card.get("shape") == "polaroid":
        kinds += ["postcard", "stamp"]
    if card.get("border") == "lace":
        kinds += ["stitch", "flowerwreath"]
    if card.get("border") == "glow" or mood == "neon":
        kinds += ["neontube", "gradientring"]
    if card.get("border") == "double":
        kinds += ["goldleaf", "artdeco"]
    if card.get("border") == "dashed":
        kinds.append("stitch")
    if "stars" in deco or "sparkles" in deco:
        kinds.append("starline")
    if "hearts" in deco:
        kinds.append("heartline")
    if "bubbles" in deco:
        kinds.append("bubbleframe")
    if "clouds" in deco:
        kinds.append("cloudframe")
    if "flowers" in deco:
        kinds.append("flowerwreath")
    if "tape" in deco or "stickers" in deco:
        kinds.append("washi")
    if "ribbons" in deco:
        kinds.append("ribbon")
    if g.get("pattern") in ("grid", "checker", "gingham"):
        kinds += ["notebook", "pixel"]
    if mood in ("dark", "neon") or g.get("typography") == "stamp":
        kinds += ["cyberbrackets", "glitchframe"]
    if mood == "vintage":
        kinds += ["filmstrip2", "artdeco"]
    # 设计语法给出的「相近画法」只占一半机会，另一半在全部 24 种里挑（避免一种画法泛滥：星星装饰太常见）
    if not kinds or rng.random() < .5:
        kinds = FRAME_KINDS
    pal = [c for c in (palette or []) if isinstance(c, str)] + ["#ff6fae", "#7fd8ff", "#ffe066"]
    dens = float(g.get("density") or .5)
    return {
        "kind": rng.choice(kinds), "seed": seed,
        "margin": round(.012 + .03 * rng.random(), 4), "radius": 0 if card.get("shape") in ("none", "ticket") and rng.random() < .5 else round(.01 + .06 * rng.random(), 4),
        "line": round(1.5 + 4 * rng.random(), 2), "double": card.get("border") == "double", "dash": card.get("border") == "dashed",
        "corner": rng.choice(["star", "heart", "spark", "flower", "dot", "diamond", "none"] if deco else ["none", "dot", "diamond"]),
        "cornerSize": round(.02 + .03 * rng.random(), 4),
        "side": rng.choice(["dots", "ticks", "waves", "chain", "scallop", "none"]) if dens > .4 else "none",
        "opacity": round(.6 + .35 * rng.random(), 3), "glow": round(.006 + .012 * rng.random(), 4) if card.get("border") == "glow" or mood == "neon" else 0,
        "tint": rng.randrange(3), "bar": 0, "tilt": 0, "count": 4 + int(dens * 14),
        "cols": pal[:3] + ["#ffffff"],
        "genres": list(g.get("genres") or ["pop"]),
    }


def breed_genomes(parents: list, seed: int) -> dict:
    """边框基因交叉 + 变异（与舞台 stage-frames.js 的 breed 同规则）。"""
    rng = random.Random(seed)
    kid = {}
    for k in parents[0]:
        kid[k] = rng.choice(parents)[k] if all(k in p for p in parents) else parents[0][k]
    # 画法偶尔突变成别的（25%），让后代不会都长一个样
    if rng.random() < .25:
        kid["kind"] = rng.choice(FRAME_KINDS)
    for k in ("margin", "radius", "line", "cornerSize", "opacity", "glow"):
        if isinstance(kid.get(k), (int, float)) and not isinstance(kid.get(k), bool):
            kid[k] = round(max(0.0, kid[k] * rng.uniform(.8, 1.2)), 4)
    kid["cols"] = [rng.choice(parents)["cols"][i % 3] for i in range(3)] + ["#ffffff"]
    g = set()
    for p in parents:
        g.update(p.get("genres") or [])
    kid["genres"] = sorted(g)
    kid["seed"] = seed
    return kid
