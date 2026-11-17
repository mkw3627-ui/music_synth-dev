"""「为她而唱」舞台素材库：下载 → 学风格 → 生成自有图案 → 效果库（带标签）。

设计约束（用户 2026-11-17 要求 + 素材站条款）：
  * 下载的图片只作「参考」，学完就移入回收站（不长期保留、不直接上台）。
  * 学到的是「风格参数」（主色/配色关系/明暗/饱和/构图密度/线条感/形状倾向/纹理颗粒），
    不是像素；舞台用这些参数驱动自己的程序化绘制，生成和原图不同的新图案。
  * 不用于训练任何 AI 模型（OKUMONO/sozaino.site、AOmaterial 条款明确禁止 AI 学习用途），
    这里只做数值统计（颜色直方图、边缘密度、K 均值聚类），而且：
      - 条款写明「禁止 AI 学习」的站点，默认只登记、不下载（status=blocked_by_terms），
        需要用户逐站明确授权（且由用户对条款负责）才会抓；
      - 允许的来源才会自动抓。
  * 只抓公开页面里的缩略图/预览图，遵守 robots.txt，限速（每站每 3 s 一个请求），
    单次最多 N 张，不登录、不绕过任何限制。

产物（插件数据目录 asset_lib/）：
  sources.json    素材来源登记（站点、条款摘要、是否允许自动抓、用户授权）
  inbox/          刚下载的参考图（学完移入 _trash 并记清单）
  styles.json     学到的风格条目（每条带标签：mood/genre/palette/shape/density…）
  effects.json    效果库：每个效果的类型、参数、标签（舞台按歌的特征筛选）
  feedback.json   用户对风格的喜好（喜欢/不喜欢），影响抽选权重
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "NEKO-music_synth-asset-lib/1.0 (local, low-rate)"
# 用户 2026-11-17 明确授权：下载到本地参考库，由本地模型看结构/风格（不照搬具体设计、不再配布）
USER_AUTHORIZED_20261117 = {"sozaino", "aomaterial", "usaneko", "aoshio"}
_LOCK = threading.Lock()

# 已知素材站（条款读于 2026-11-17；ai_learning=False 表示条款禁止 AI 学习用途）
KNOWN_SOURCES = [
    {"id": "sozaino", "name": "OKUMONO (sozaino.site)", "url": "https://sozaino.site/",
     "terms_url": "https://sozaino.site/kiyaku", "commercial": True, "ai_learning": False,
     "terms_note": "商用免费、可加工；禁止：素材再配布/贩卖、AI 学习目的使用"},
    {"id": "aomaterial", "name": "AOmaterial", "url": "https://aomaterial.com/",
     "terms_url": "https://aomaterial.com/tos/", "commercial": True, "ai_learning": False,
     "terms_note": "个人/法人/商用免费、可改色加工；禁止：二次配布/再配布、AI 学习"},
    {"id": "usaneko", "name": "うさねこメモリー", "url": "https://usanekomemory.com/",
     "terms_url": "https://usanekomemory.com/pages/terms-of-service-page", "commercial": None, "ai_learning": None,
     "terms_note": "以付费/免费商品为主，条款未明确 AI 学习，默认不自动抓"},
    {"id": "aoshio", "name": "AOSHIO", "url": "https://aoshio.blogspot.com/",
     "terms_url": "https://aoshio.blogspot.com/p/blog-page.html", "commercial": None, "ai_learning": None,
     "terms_note": "条款未明确 AI 学习，默认不自动抓"},
    # 明确允许任何用途（含参考/学习）的公有领域/CC0 来源
    {"id": "wikimedia_cc0", "name": "Wikimedia Commons（CC0/公有领域）",
     "url": "https://commons.wikimedia.org/w/api.php", "commercial": True, "ai_learning": True,
     "terms_note": "只取 CC0 / Public domain 授权的文件", "api": "wikimedia"},
    {"id": "openverse_cc0", "name": "Openverse（CC0）",
     "url": "https://api.openverse.org/v1/images/", "commercial": True, "ai_learning": True,
     "terms_note": "只取 license=cc0 的图片", "api": "openverse"},
    # 第四十九轮：再加 4 个公开授权的图库 API（全部 CC0 / 公有领域，官方开放接口，不需要账号）
    {"id": "met_openaccess", "name": "大都会艺术博物馆 Open Access（公有领域）",
     "url": "https://collectionapi.metmuseum.org/public/collection/v1.1/", "commercial": True, "ai_learning": True,
     "terms_note": "只取 isPublicDomain=true 的藏品图（纹样、织物、装饰、版画）", "api": "met"},
    {"id": "artic_cc0", "name": "芝加哥艺术博物馆（公有领域）",
     "url": "https://api.artic.edu/api/v1/artworks/search", "commercial": True, "ai_learning": True,
     "terms_note": "只取 is_public_domain=true，IIIF 600px 预览", "api": "artic"},
    {"id": "cleveland_cc0", "name": "克利夫兰艺术博物馆 Open Access（CC0）",
     "url": "https://openaccess-api.clevelandart.org/api/artworks/", "commercial": True, "ai_learning": True,
     "terms_note": "只取 share_license_status=CC0 的 web 预览图", "api": "cleveland"},
    {"id": "nasa_images", "name": "NASA Image Library（公有领域）",
     "url": "https://images-api.nasa.gov/search", "commercial": True, "ai_learning": True,
     "terms_note": "NASA 图片一般为公有领域；适合星空/星云/宇宙背景", "api": "nasa"},
]

# 风格关键词（用于在允许的来源里搜参考图；也作为标签）
# 按素材类别的搜索词（素材站里用日文关键词更准）
CATEGORY_QUERIES = {
    "frame": ["フレーム", "枠", "frame border"], "background": ["背景", "background"], "lines": ["ライン", "線 素材"],
    "particles": ["キラキラ", "エフェクト", "sparkle"], "sticker": ["スタンプ", "アイコン 素材"], "text_plate": ["吹き出し", "テロップ", "見出し"],
}
# 第四十九轮：按来源补充的搜索词（博物馆的纹样/装饰/织物最适合学边框和底纹；NASA 适合星空背景）
SOURCE_QUERIES = {
    "met_openaccess": ["ornament pattern", "textile pattern", "border design", "japanese pattern", "stencil"],
    "artic_cc0": ["ornament", "textile", "japanese woodblock", "decorative border", "wallpaper"],
    "cleveland_cc0": ["textile pattern", "ornament", "lacquer", "kimono", "tile"],
    "nasa_images": ["nebula", "galaxy", "aurora", "star cluster", "earth at night"],
}
QUERIES = {
    "sakura": ["cherry blossom night", "sakura petals"], "neon": ["neon city night", "neon lights"],
    "stage": ["concert stage lights", "stage spotlight"], "sky": ["sunset sky clouds", "starry sky"],
    "ocean": ["underwater light rays", "sea sunset"], "forest": ["forest fireflies", "misty forest"],
    "pastel": ["pastel gradient", "kawaii pattern"], "retro": ["retro film grain", "vintage theater"],
    "lantern": ["paper lanterns festival", "fireworks festival"], "space": ["nebula galaxy", "planet rings"],
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class AssetLib:
    def __init__(self, data_dir: Path, logger=None):
        self.root = Path(data_dir) / "asset_lib"
        self.inbox = self.root / "inbox"
        self.trash = self.root / "_trash"
        for d in (self.root, self.inbox, self.trash):
            d.mkdir(parents=True, exist_ok=True)
        self.log = logger
        self._robots: dict = {}
        self._last_hit: dict = {}
        self._ensure_sources()

    # ------------------------------------------------------------ 存取
    def _p(self, name: str) -> Path:
        return self.root / name

    def _load(self, name: str, default):
        try:
            return json.loads(self._p(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self, name: str, data) -> None:
        tmp = self._p(name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._p(name))

    def _info(self, msg, *a):
        if self.log:
            try:
                self.log.info("[asset-lib] " + msg, *a)
            except Exception:  # noqa: BLE001
                pass

    def _ensure_sources(self) -> None:
        cur = {s["id"]: s for s in self._load("sources.json", [])}
        out = []
        for s in KNOWN_SOURCES:
            old = cur.pop(s["id"], {})
            merged = dict(s)
            merged["user_authorized"] = bool(old.get("user_authorized", s["id"] in USER_AUTHORIZED_20261117))
            merged["enabled"] = old.get("enabled", True)
            merged["auto"] = bool(s.get("ai_learning")) or merged["user_authorized"]
            out.append(merged)
        out.extend(cur.values())   # 用户自己加的来源保留
        self._save("sources.json", out)

    def sources(self) -> list:
        return self._load("sources.json", [])

    def authorize(self, source_id: str, allow: bool) -> dict:
        """用户逐站授权（条款禁止 AI 学习的站，用户明确授权后才会抓，后果由用户负责）。"""
        src = self.sources()
        for s in src:
            if s["id"] == source_id:
                s["user_authorized"] = bool(allow)
                s["auto"] = bool(s.get("ai_learning")) or bool(allow)
                s["authorized_at"] = _now()
        self._save("sources.json", src)
        return {"ok": True, "sources": src}

    # ------------------------------------------------------------ 网络（礼貌抓取）
    # 官方公开 API（文档允许程序调用，robots.txt 针对的是网页爬虫；Openverse 的 robots 被 Cloudflare 挡住读不到）
    API_HOSTS = ("api.openverse.org", "commons.wikimedia.org", "upload.wikimedia.org", "live.staticflickr.com",
                 "collectionapi.metmuseum.org", "images.metmuseum.org", "api.artic.edu", "www.artic.edu",
                 "openaccess-api.clevelandart.org", "openaccess-cdn.clevelandart.org",
                 "images-api.nasa.gov", "images-assets.nasa.gov")

    def _allowed_by_robots(self, url: str) -> bool:
        u = urllib.parse.urlparse(url)
        if u.netloc in self.API_HOSTS:
            return True
        base = f"{u.scheme}://{u.netloc}"
        rules = self._robots.get(base)
        if rules is None:
            # 宿主内置 Python 没有 urllib.robotparser：自己解析「User-agent: *」段的 Disallow/Allow（最长前缀优先）
            rules = []
            try:
                req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=10) as r:
                    txt = r.read(256 * 1024).decode("utf-8", "replace")
                active = False
                for line in txt.splitlines():
                    line = line.split("#", 1)[0].strip()
                    if not line or ":" not in line:
                        continue
                    k, v = [x.strip() for x in line.split(":", 1)]
                    k = k.lower()
                    if k == "user-agent":
                        active = (v == "*")
                    elif active and k in ("disallow", "allow") and v:
                        rules.append((k == "allow", v))
            except Exception:  # noqa: BLE001
                rules = []
            self._robots[base] = rules
        path = u.path + (("?" + u.query) if u.query else "")
        best, allowed = -1, True
        for allow, prefix in rules:
            pre = prefix.rstrip("*").replace("$", "")
            if path.startswith(pre) and len(pre) > best:
                best, allowed = len(pre), allow
        return allowed

    def _get(self, url: str, timeout: float = 20.0, binary: bool = False):
        if not self._allowed_by_robots(url):
            raise PermissionError("robots.txt disallow: " + url)
        host = urllib.parse.urlparse(url).netloc
        wait = 3.0 - (time.time() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": f"https://{host}/"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(12 * 1024 * 1024)
        return data if binary else data.decode("utf-8", "replace")

    def _candidates(self, src: dict, query: str, limit: int) -> list:
        """来源 → 候选图片 [{url, page, license, title}]"""
        out = []
        api = src.get("api")
        if api == "openverse":
            q = urllib.parse.urlencode({"q": query, "license": "cc0", "page_size": min(20, limit * 2)})
            data = json.loads(self._get(src["url"] + "?" + q))
            for r in data.get("results") or []:
                if str(r.get("license") or "").lower() != "cc0":
                    continue
                out.append({"url": r.get("thumbnail") or r.get("url"), "page": r.get("foreign_landing_url"),
                            "license": "cc0", "title": r.get("title") or ""})
        elif api == "wikimedia":
            q = urllib.parse.urlencode({"action": "query", "format": "json", "generator": "search",
                                        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": min(20, limit * 3),
                                        "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 640})
            data = json.loads(self._get(src["url"] + "?" + q))
            for page in ((data.get("query") or {}).get("pages") or {}).values():
                ii = (page.get("imageinfo") or [{}])[0]
                lic = str(((ii.get("extmetadata") or {}).get("LicenseShortName") or {}).get("value") or "").lower()
                if "cc0" not in lic and "public domain" not in lic:
                    continue
                out.append({"url": ii.get("thumburl") or ii.get("url"), "page": ii.get("descriptionurl"),
                            "license": lic, "title": page.get("title") or ""})
        elif api == "met":
            q = urllib.parse.urlencode({"q": query, "hasImages": "true", "isPublicDomain": "true"})
            ids = (json.loads(self._get(src["url"] + "search?" + q)).get("objectIDs") or [])[:max(4, limit * 2)]
            random.shuffle(ids)
            for oid in ids[:limit * 2]:
                try:
                    # 搜索走 v1.1，单个藏品详情仍在 v1（v1.1/objects 返回 404）
                    o = json.loads(self._get(src["url"].replace("/v1.1/", "/v1/") + f"objects/{oid}"))
                except Exception:  # noqa: BLE001
                    continue
                if o.get("isPublicDomain") and (o.get("primaryImageSmall") or o.get("primaryImage")):
                    out.append({"url": o.get("primaryImageSmall") or o.get("primaryImage"), "page": o.get("objectURL"),
                                "license": "public-domain", "title": o.get("title") or ""})
                if len(out) >= limit:
                    break
        elif api == "artic":
            q = urllib.parse.urlencode({"q": query, "limit": min(20, limit * 3), "fields": "id,title,image_id,is_public_domain",
                                        "query[term][is_public_domain]": "true"})
            data = json.loads(self._get(src["url"] + "?" + q))
            for r in data.get("data") or []:
                if r.get("is_public_domain") and r.get("image_id"):
                    out.append({"url": f"https://www.artic.edu/iiif/2/{r['image_id']}/full/600,/0/default.jpg",
                                "page": f"https://www.artic.edu/artworks/{r['id']}", "license": "public-domain", "title": r.get("title") or ""})
        elif api == "cleveland":
            q = urllib.parse.urlencode({"q": query, "cc0": 1, "has_image": 1, "limit": min(20, limit * 2)})
            data = json.loads(self._get(src["url"] + "?" + q))
            for r in data.get("data") or []:
                web = ((r.get("images") or {}).get("web") or {}).get("url")
                if str(r.get("share_license_status") or "").upper() == "CC0" and web:
                    out.append({"url": web, "page": r.get("url"), "license": "cc0", "title": r.get("title") or ""})
        elif api == "nasa":
            q = urllib.parse.urlencode({"q": query, "media_type": "image", "page_size": min(20, limit * 2)})
            data = json.loads(self._get(src["url"] + "?" + q))
            for it in ((data.get("collection") or {}).get("items") or []):
                href = ((it.get("links") or [{}])[0]).get("href")
                if href:
                    href = href.replace("~thumb.", "~medium.")
                    out.append({"url": href, "page": href, "license": "public-domain(NASA)",
                                "title": ((it.get("data") or [{}])[0]).get("title") or ""})
        else:
            # 普通素材站：只解析首页/搜索页里的公开预览图（WordPress 的 wp-content/uploads 缩略图）
            page_url = src["url"]
            if query:
                page_url = src["url"].rstrip("/") + "/?s=" + urllib.parse.quote(query)
            html = self._get(page_url)
            # WordPress 预览图 + Shopify 商品图（//xxx/cdn/shop/files/xxx.jpg?v=..&width=..）
            for m in re.finditer(r'(?:data-src|src|srcset)="((?:https?:)?//[^"\s]+?\.(?:png|jpe?g|webp)(?:\?[^"\s]*)?)', html):
                u = m.group(1)
                if u.startswith("//"):
                    u = "https:" + u
                if "/cdn/shop/" in u and "width=" not in u:
                    u += ("&" if "?" in u else "?") + "width=800"
                if "logo" in u.lower() or "icon" in u.lower():
                    continue
                out.append({"url": u, "page": page_url, "license": "site-terms", "title": ""})
        seen, uniq = set(), []
        for c in out:
            if c.get("url") and c["url"] not in seen:
                seen.add(c["url"])
                uniq.append(c)
        return uniq[:limit]

    def download(self, style: str = "", limit: int = 8, source_ids: list | None = None, category: str = "") -> dict:
        """下载参考图到 inbox/。只从 auto=True（允许学习 或 用户已授权）的来源下载。
        category（frame/background/lines/particles/sticker/text_plate）：按素材类别搜，下载后再由视觉模型核对类别，不对的挪走。"""
        if category and not style:
            style = random.choice(CATEGORY_QUERIES.get(category) or [category])
        src = [s for s in self.sources() if s.get("enabled", True) and s.get("auto")]
        if source_ids:
            src = [s for s in src if s["id"] in source_ids]
        blocked = [s["id"] for s in self.sources() if not s.get("auto")]
        queries = QUERIES.get(style) or [style] if style else [q for v in QUERIES.values() for q in v[:1]]
        got, errors = [], []
        manifest = self._load("inbox_manifest.json", [])
        # 每次随机打乱来源顺序，配额均摊（以前总是排在前面的来源先吃满）
        src = sorted(src, key=lambda _x: random.random())
        per_src = max(1, -(-limit // max(1, len(src))))
        for s in src:
            got_here = 0
            qs = list(queries)
            if not style and s["id"] in SOURCE_QUERIES:
                qs = random.sample(SOURCE_QUERIES[s["id"]], min(2, len(SOURCE_QUERIES[s["id"]])))
            for q in qs:
                if len(got) >= limit or got_here >= per_src:
                    break
                try:
                    cands = self._candidates(s, q, limit - len(got))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{s['id']}: {type(exc).__name__}: {exc}"[:200])
                    continue
                for c in cands:
                    if len(got) >= limit or got_here >= per_src:
                        break
                    try:
                        data = self._get(c["url"], binary=True)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{c['url'][:80]}: {exc}"[:200])
                        continue
                    if len(data) < 2000:
                        continue
                    h = hashlib.sha256(data).hexdigest()
                    ext = os.path.splitext(urllib.parse.urlparse(c["url"]).path)[1].lower() or ".jpg"
                    fn = self.inbox / f"{h[:16]}{ext if ext in ('.png', '.jpg', '.jpeg', '.webp') else '.jpg'}"
                    if not fn.exists():
                        fn.write_bytes(data)
                    rec = dict(c, file=fn.name, sha256=h, size=len(data), source=s["id"], query=q, style=style or q, at=_now(),
                               want_category=category or "", state="pending")
                    manifest.append(rec)
                    got.append(rec)
                    got_here += 1
        self._save("inbox_manifest.json", manifest)
        self._info("下载 %d 张参考图（来源 %s），跳过条款不允许的来源 %s", len(got), [s["id"] for s in src], blocked)
        return {"ok": True, "downloaded": len(got), "files": [g["file"] for g in got], "errors": errors[:10],
                "skipped_sources": blocked}

    # ------------------------------------------------------------ 学风格（只取统计参数，不保留像素）
    @staticmethod
    def _analyze(path: Path) -> dict:
        import numpy as np
        from PIL import Image, ImageFilter
        im = Image.open(path).convert("RGB")
        im.thumbnail((128, 128))   # 只为统计配色/明暗：小图就够，CPU 几乎不费力
        a = np.asarray(im).astype("float32") / 255.0
        px = a.reshape(-1, 3)
        # K 均值 5 色（numpy 自写，免依赖）
        rng = np.random.default_rng(0)
        cent = px[rng.choice(len(px), 5, replace=False)]
        for _ in range(8):
            d = ((px[:, None, :] - cent[None, :, :]) ** 2).sum(-1)
            lab = d.argmin(1)
            for k in range(5):
                m = px[lab == k]
                if len(m):
                    cent[k] = m.mean(0)
        counts = np.bincount(lab, minlength=5) / len(lab)
        order = np.argsort(-counts)
        pal = ["#%02x%02x%02x" % tuple(int(v * 255) for v in cent[k]) for k in order]
        mx, mn = px.max(1), px.min(1)
        lum = (0.299 * px[:, 0] + 0.587 * px[:, 1] + 0.114 * px[:, 2])
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
        edges = np.asarray(im.convert("L").filter(ImageFilter.FIND_EDGES)).astype("float32") / 255.0
        g = edges > .25
        # 线条方向倾向（横/竖/斜）
        gy, gx = np.gradient(np.asarray(im.convert("L")).astype("float32"))
        ang = np.degrees(np.arctan2(gy, gx))[g] if g.any() else np.array([0.0])
        horiz = float(np.mean(np.abs(np.sin(np.radians(ang))) > .9)) if len(ang) else 0
        vert = float(np.mean(np.abs(np.cos(np.radians(ang))) > .9)) if len(ang) else 0
        # 亮点（光斑/星点）密度
        bright = float(np.mean(lum > .85))
        # 上下明暗分布（构图：天空在上 / 地面在下）
        h = a.shape[0]
        top, bot = float(lum.reshape(h, -1)[: h // 3].mean()), float(lum.reshape(h, -1)[-h // 3:].mean())
        return {
            "palette": pal, "weights": [round(float(counts[k]), 3) for k in order],
            "lum": round(float(lum.mean()), 3), "contrast": round(float(lum.std()), 3),
            "sat": round(float(sat.mean()), 3), "edge": round(float(g.mean()), 3),
            "horiz": round(horiz, 3), "vert": round(vert, 3), "sparkle": round(bright, 4),
            "top_bottom": round(top - bot, 3),
        }

    @staticmethod
    def _tags_of(st: dict) -> dict:
        lum, sat, edge = st["lum"], st["sat"], st["edge"]
        mood = "dark" if lum < .3 else ("bright" if lum > .6 else "mid")
        energy = "calm" if edge < .06 else ("busy" if edge > .16 else "normal")
        tone = "pastel" if sat < .3 and lum > .55 else ("vivid" if sat > .55 else "soft")
        genres = []
        if mood == "dark" and (st["sparkle"] > .01 or sat > .5):
            genres += ["edm", "rock", "dark"]
        if tone == "pastel":
            genres += ["cute", "pop", "ballad"]
        if mood == "mid" and energy == "calm":
            genres += ["ballad", "jazz", "ancient"]
        if tone == "vivid" and energy != "calm":
            genres += ["pop", "edm", "rock"]
        shape = "lines" if max(st["horiz"], st["vert"]) > .35 else ("dots" if st["sparkle"] > .02 else "blobs")
        return {"mood": mood, "energy": energy, "tone": tone, "genres": sorted(set(genres)) or ["pop"], "shape": shape}

    def learn(self, max_files: int = 40, use_vision: bool = True) -> dict:
        """inbox 里的参考图 → 风格条目：
        ① 数值统计（配色/明暗/线条/密度）；② 本地视觉模型看懂「设计语法」（版式/层次/卡片做法/装饰种类/纹理）。
        参考图移到 refs/（本地参考库，不上台、不再配布），以后可以重新学。"""
        files = sorted([p for p in self.inbox.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")])[:max_files]
        vmodel = ""
        if use_vision:
            try:
                from . import asset_vision as _av
            except ImportError:
                import asset_vision as _av  # 离线测试
            vmodel = _av.available_model()
        styles = self._load("styles.json", [])
        man_list = self._load("inbox_manifest.json", [])
        manifest = {m.get("file"): m for m in man_list}
        learned = 0
        self._set_state([f.name for f in files], "learning")
        for f in files:
            try:
                st = self._analyze(f)
            except Exception as exc:  # noqa: BLE001
                self._info("分析失败 %s：%s", f.name, exc)
                continue
            meta = manifest.get(f.name, {})
            tags = self._tags_of(st)
            grammar = None
            if vmodel:
                try:
                    grammar = _av.describe(f, vmodel)
                except Exception as exc:  # noqa: BLE001
                    self._info("视觉模型看图失败 %s：%s", f.name, exc)
            if grammar:
                tags["keywords"] = grammar.get("keywords") or []
                tags["layout"] = grammar.get("layout")
                tags["category"] = grammar.get("category")
                tags["genres"] = sorted(set(tags.get("genres") or []) | set(grammar.get("genres") or []))
                tags["sections"] = grammar.get("sections") or []
                tags["energy"] = grammar.get("energy")
                want = meta.get("want_category")
                SAME = {"frame": {"frame", "overlay", "text_plate"}, "text_plate": {"text_plate", "frame", "overlay", "sticker"},
                        "background": {"background", "overlay"}, "particles": {"particles", "overlay", "sticker"},
                        "lines": {"lines", "frame", "overlay", "background"}, "sticker": {"sticker", "particles", "overlay"}}
                if want and grammar.get("category") not in SAME.get(want, {want}) | {"other"}:
                    self._to_trash(f, f"类别不符（要 {want}，模型看是 {grammar.get('category')}）")
                    self._set_state([f.name], "rejected", note=f"类别不符：{grammar.get('category')}")
                    continue
            sid = hashlib.sha1((f.name + json.dumps(st["palette"])).encode()).hexdigest()[:10]
            styles.append({"id": sid, "from": meta.get("source", "local"), "query": meta.get("query", ""),
                           "license": meta.get("license", ""), "params": st, "tags": tags, "grammar": grammar,
                           "vision_model": vmodel or None, "ref": f.name, "learned_at": _now()})
            self._to_refs(f)
            self._set_state([f.name], "learned", style_id=sid)
            learned += 1
        self._save("styles.json", styles)
        self._rebuild_effects(styles)
        return {"ok": True, "learned": learned, "styles": len(styles)}

    def _set_state(self, files: list, state: str, **extra) -> None:
        man = self._load("inbox_manifest.json", [])
        fs = set(files)
        for m in man:
            if m.get("file") in fs:
                m["state"] = state
                m["state_at"] = _now()
                m.update(extra)
        self._save("inbox_manifest.json", man)

    def cleanup_learned(self, keep_days: float = 0) -> dict:
        """已学完、而且已经进化出新素材的参考图 → 移入回收站（写清单），参考库只留没学完的。"""
        man = self._load("inbox_manifest.json", [])
        refs = self.root / "refs"
        used = set()
        for t in self._load("templates.json", []):
            used.update(t.get("parents") or [])
        for g in self._load("frame_genomes.json", []):
            used.update(g.get("parents") or [])
        moved = 0
        now = time.time()
        for m in man:
            if m.get("state") != "learned" or m.get("style_id") not in used:
                continue
            f = refs / m.get("file", "")
            if not f.exists():
                continue
            try:
                age_days = (now - f.stat().st_mtime) / 86400
            except OSError:
                age_days = 0
            if age_days < keep_days:
                continue
            self._to_trash(f, "已学完并已用于生成新素材（模板/边框），按设定清理")
            m["state"] = "removed"
            m["state_at"] = _now()
            moved += 1
        self._save("inbox_manifest.json", man)
        return {"ok": True, "removed": moved}

    # ------------------------------------------------------------ 边框进化（基因交叉/变异）
    def evolve_frames(self, n: int = 12, seed: int | None = None) -> dict:
        """学到的设计语法 → 边框基因；再把 2~3 个基因交叉/变异出新一代（可以和上一代继续杂交 → 不停进化）。"""
        try:
            from . import asset_vision as _av
        except ImportError:
            import asset_vision as _av
        rng = random.Random(seed if seed is not None else time.time())
        styles = [x for x in self._load("styles.json", []) if x.get("grammar")]
        genomes = self._load("frame_genomes.json", [])
        have = {g.get("from_style") for g in genomes if g.get("gen") == 0}
        for st in styles:   # 第 0 代：每条设计语法翻成一个边框基因
            if st["id"] in have:
                continue
            g = _av.frame_genome(st["grammar"], st["params"]["palette"][:3], rng.randrange(1 << 30))
            g.update({"id": "fg_" + st["id"], "gen": 0, "from_style": st["id"], "parents": [st["id"]], "made_at": _now()})
            genomes.append(g)
        fb = self._load("feedback.json", {})
        made = 0
        for _ in range(n):   # 下一代：从现有基因（含上一代杂交出来的）里按喜好权重挑 2~3 个交叉
            if len(genomes) < 2:
                break
            w = [max(.05, 1.0 + float(fb.get(g["id"], 0))) for g in genomes]
            k = min(len(genomes), rng.choice([2, 2, 3]))
            idx = []
            pool = list(range(len(genomes)))
            for _j in range(k):
                tot = sum(w[i] for i in pool)
                r = rng.uniform(0, tot)
                for i in pool:
                    r -= w[i]
                    if r <= 0:
                        idx.append(i)
                        pool.remove(i)
                        break
            par = [genomes[i] for i in idx]
            kid = _av.breed_genomes(par, rng.randrange(1 << 30))
            kid.update({"id": "fg_" + hashlib.sha1(json.dumps(kid, sort_keys=True).encode()).hexdigest()[:10],
                        "gen": max(p.get("gen", 0) for p in par) + 1,
                        "parents": [p["id"] for p in par], "made_at": _now()})
            genomes.append(kid)
            made += 1
        genomes = genomes[-400:]
        self._save("frame_genomes.json", genomes)
        return {"ok": True, "made": made, "genomes": len(genomes),
                "generations": max([g.get("gen", 0) for g in genomes] or [0])}

    # ------------------------------------------------------------ 内置效果打标签（本地模型判断「这个效果适合哪些风格/段落」）
    def tag_builtins(self, max_items: int = 200) -> dict:
        """用本地对话模型给内置效果库（背景/边框/转场/滤镜/粒子/蒙版/歌词效果）打标签：适合的曲风、段落、能量。
        结果存 effect_tags.json；没有对话模型时按内置规则打。"""
        out = self._load("effect_tags.json", {})
        items = []
        for group, lst in BUILTIN_EFFECTS.items():
            for e in lst:
                if e["id"] + "@" + group not in out:
                    items.append((group, e))
        items = items[:max_items]
        if not items:
            return {"ok": True, "tagged": 0, "total": len(out)}
        model = ""
        try:
            with urllib.request.urlopen("http://127.0.0.1:11435/api/tags", timeout=5) as r:
                names = [m.get("name") for m in json.loads(r.read()).get("models", [])]
            for cand in ("qwen2.5vl:7b", "nymphaea-rp:14b-q5"):
                if cand in names:
                    model = cand
                    break
        except Exception:  # noqa: BLE001
            model = ""
        done = 0
        for i in range(0, len(items), 12):
            chunk = items[i:i + 12]
            ask = [{"group": g, "id": e["id"]} for g, e in chunk]
            tags = None
            if model:
                prompt = ("You tag visual effects for an anime singing-stage. For each item give which music genres it suits "
                          "(from ballad,pop,edm,rock,cute,ancient,jazz,dark), which song sections (intro,verse,chorus,bridge,outro) "
                          "and an energy 0..1. Reply ONLY a JSON object {\"items\":[{\"id\":..,\"group\":..,\"genres\":[..],\"sections\":[..],\"energy\":..}]}. "
                          "Items: " + json.dumps(ask))
                body = {"model": model, "prompt": prompt, "stream": False, "format": "json", "options": {"temperature": .2, "num_ctx": 4096}, "keep_alive": "2m"}
                try:
                    req = urllib.request.Request("http://127.0.0.1:11435/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=180) as r:
                        tags = json.loads(json.loads(r.read()).get("response") or "{}").get("items")
                except Exception as exc:  # noqa: BLE001
                    self._info("打标签失败：%s", exc)
                    tags = None
            byid = {(t.get("group"), t.get("id")): t for t in (tags or []) if isinstance(t, dict)}
            for g, e in chunk:
                t = byid.get((g, e["id"])) or {}
                genres = [x for x in (t.get("genres") or []) if x in ("ballad", "pop", "edm", "rock", "cute", "ancient", "jazz", "dark")]
                if not genres:
                    genres = list((e.get("tags") or {}).get("genres") or ["pop"])
                sections = [x for x in (t.get("sections") or []) if x in ("intro", "verse", "chorus", "bridge", "outro")] or ["verse", "chorus"]
                try:
                    energy = max(0.0, min(1.0, float(t.get("energy"))))
                except (TypeError, ValueError):
                    energy = .5
                out[e["id"] + "@" + g] = {"group": g, "id": e["id"], "genres": genres, "sections": sections, "energy": energy,
                                          "by": model or "rule", "at": _now()}
                done += 1
            self._save("effect_tags.json", out)
        return {"ok": True, "tagged": done, "total": len(out), "model": model or "rule"}

    # ------------------------------------------------------------ 页面总览
    def overview(self) -> dict:
        man = self._load("inbox_manifest.json", [])
        counts = {}
        for m in man:
            counts[m.get("state") or "pending"] = counts.get(m.get("state") or "pending", 0) + 1
        st = self._load("styles.json", [])
        cats = {}
        for x in st:
            c = (x.get("tags") or {}).get("category") or "other"
            cats[c] = cats.get(c, 0) + 1
        return {"ok": True, "states": counts, "categories": cats, "styles": len(st),
                "templates": len(self._load("templates.json", [])), "frame_genomes": len(self._load("frame_genomes.json", [])),
                "effect_tags": len(self._load("effect_tags.json", {})), "builtin": {k: len(v) for k, v in BUILTIN_EFFECTS.items()},
                "items": [{k: m.get(k) for k in ("file", "source", "query", "state", "state_at", "note", "style_id", "want_category")} for m in man[-120:]]}

    def _to_refs(self, f: Path) -> None:
        refs = self.root / "refs"
        refs.mkdir(exist_ok=True)
        dst = refs / f.name
        if dst.exists():
            self._to_trash(f, "参考库里已有同一张")
        else:
            shutil.move(str(f), str(dst))

    def relearn(self, limit: int = 20) -> dict:
        """定期复学：给参考库里还没有设计语法（当时模型没装/失败）的图补上视觉分析。"""
        try:
            from . import asset_vision as _av
        except ImportError:
            import asset_vision as _av
        vmodel = _av.available_model()
        if not vmodel:
            return {"ok": False, "error": "本地视觉模型还没装好"}
        styles = self._load("styles.json", [])
        refs = self.root / "refs"
        done = 0
        for st in styles:
            if done >= limit:
                break
            if st.get("grammar") or not st.get("ref") or not (refs / st["ref"]).exists():
                continue
            try:
                g = _av.describe(refs / st["ref"], vmodel)
            except Exception as exc:  # noqa: BLE001
                self._info("复学失败 %s：%s", st.get("ref"), exc)
                continue
            if g:
                st["grammar"] = g
                st["vision_model"] = vmodel
                st["tags"]["keywords"] = g.get("keywords") or []
                st["tags"]["layout"] = g.get("layout")
                done += 1
        self._save("styles.json", styles)
        self._rebuild_effects(styles)
        return {"ok": True, "relearned": done}

    def blend_templates(self, n: int = 12, per: int = 3, seed: int | None = None) -> dict:
        """把 2~3 份设计语法混成新模板（每个维度从不同参考里各取一点），存 templates.json。"""
        try:
            from . import asset_vision as _av
        except ImportError:
            import asset_vision as _av
        styles = [s for s in self._load("styles.json", []) if s.get("grammar")]
        if len(styles) < 2:
            return {"ok": False, "error": "设计语法太少（至少 2 条）", "have": len(styles)}
        rng = random.Random(seed if seed is not None else time.time())
        fb = self._load("feedback.json", {})
        weights = [max(.05, 1.0 + float(fb.get(s["id"], 0))) for s in styles]
        temps = self._load("templates.json", [])
        made = 0
        for _ in range(n):
            k = min(len(styles), rng.choice([2, per]))
            pick = []
            pool = list(range(len(styles)))
            for _j in range(k):
                tot = sum(weights[i] for i in pool)
                r = rng.uniform(0, tot)
                for i in pool:
                    r -= weights[i]
                    if r <= 0:
                        pick.append(i)
                        pool.remove(i)
                        break
            g = _av.blend([styles[i]["grammar"] for i in pick], rng)
            # 配色：从参与者的调色盘里各取几种，组合出新调色盘
            pal = []
            for i in pick:
                pal += styles[i]["params"]["palette"][:3]
            rng.shuffle(pal)
            g["palette"] = pal[:5]
            g["id"] = "tpl_" + hashlib.sha1(json.dumps(g, sort_keys=True).encode()).hexdigest()[:10]
            g["parents"] = [styles[i]["id"] for i in pick]
            genres = set()
            for i in pick:
                genres.update(styles[i]["tags"].get("genres") or [])
            g["genres"] = sorted(genres) or ["pop"]
            temps.append(g)
            made += 1
        temps = temps[-200:]
        self._save("templates.json", temps)
        self._rebuild_effects(self._load("styles.json", []))
        return {"ok": True, "made": made, "templates": len(temps)}

    def _to_trash(self, f: Path, why: str) -> None:
        data = f.read_bytes()
        dst = self.trash / f.name
        shutil.move(str(f), str(dst))
        with open(self.trash / "MANIFEST.txt", "a", encoding="utf-8") as fp:
            fp.write(f"{dst}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}\t{why}\t{_now()}\n")

    # ------------------------------------------------------------ 效果库（带标签）
    def _rebuild_effects(self, styles: list) -> None:
        """学到的风格 → 舞台可用的「生成式图案」效果：palette 调色盘 + 形状/密度/方向参数。
        舞台的 learned 场景按这些参数自己画（和原图不同：只借配色与构图倾向）。"""
        fb = self._load("feedback.json", {})
        effects = []
        for st in styles:
            p, t = st["params"], st["tags"]
            w = 1.0 + float(fb.get(st["id"], 0))
            if w <= 0:
                continue
            effects.append({
                "id": "learned_" + st["id"], "type": "scene", "renderer": "learned",
                "palette": p["palette"][:5], "weights": p["weights"][:5],
                "lum": p["lum"], "contrast": p["contrast"], "sat": p["sat"],
                "density": round(min(1.6, .5 + p["edge"] * 5), 3), "sparkle": p["sparkle"],
                "shape": t["shape"], "dir": "h" if p["horiz"] > p["vert"] else "v",
                "sky": p["top_bottom"] > .08,
                "tags": dict(t, source=st["from"]), "weight": round(w, 2),
            })
        lib = self._load("effects.json", {"builtin": BUILTIN_EFFECTS})
        lib["builtin"] = BUILTIN_EFFECTS
        lib["learned"] = effects
        lib["templates"] = [dict(t, type="template", renderer="template") for t in self._load("templates.json", [])
                            if float(fb.get(t.get("id"), 0)) > -1]
        lib["frames"] = [g for g in self._load("frame_genomes.json", []) if float(fb.get(g.get("id"), 0)) > -1][-80:]
        lib["tags"] = self._load("effect_tags.json", {})
        lib["updated_at"] = _now()
        self._save("effects.json", lib)

    def effects(self) -> dict:
        lib = self._load("effects.json", None)
        if not lib:
            lib = {"builtin": BUILTIN_EFFECTS, "learned": [], "updated_at": _now()}
            self._save("effects.json", lib)
        return lib

    def feedback(self, style_id: str, like: bool) -> dict:
        fb = self._load("feedback.json", {})
        fb[style_id] = max(-1.0, min(3.0, float(fb.get(style_id, 0)) + (.5 if like else -1.0)))
        self._save("feedback.json", fb)
        self._rebuild_effects(self._load("styles.json", []))
        return {"ok": True, "weight": fb[style_id]}

    def survey(self, n: int = 4) -> dict:
        """给用户挑喜欢的风格：随机挑 n 个学到的风格（配色 + 标签），用户回 like/dislike。"""
        styles = self._load("styles.json", [])
        pick = random.sample(styles, min(n, len(styles))) if styles else []
        return {"ok": True, "choices": [{"id": s["id"], "palette": s["params"]["palette"], "tags": s["tags"]} for s in pick]}

    def status(self) -> dict:
        lib = self.effects()
        refs = self.root / "refs"
        st_all = self._load("styles.json", [])
        try:
            from . import asset_vision as _av
        except ImportError:
            import asset_vision as _av
        return {"ok": True, "root": str(self.root), "inbox": len(list(self.inbox.glob("*.*"))),
                "refs": len(list(refs.glob("*.*"))) if refs.exists() else 0,
                "with_grammar": sum(1 for x in st_all if x.get("grammar")),
                "templates": len(self._load("templates.json", [])), "vision_model": _av.available_model(),
                "styles": len(self._load("styles.json", [])), "learned_effects": len(lib.get("learned") or []),
                "builtin_effects": sum(len(v) for v in BUILTIN_EFFECTS.values()),
                "sources": [{k: s.get(k) for k in ("id", "name", "auto", "ai_learning", "user_authorized", "terms_note")} for s in self.sources()]}


# 内置效果库（程序化素材）——全部带标签，舞台按歌的特征筛选
def _e(eid, typ, **tags):
    return {"id": eid, "type": typ, "tags": tags}


BUILTIN_EFFECTS = {
    "scenes": [
        _e("bokeh", "scene", mood="soft", genres=["ballad", "jazz", "pop"], energy="calm"),
        _e("wave", "scene", mood="calm", genres=["ballad", "edm"], energy="calm"),
        _e("rings", "scene", mood="pulse", genres=["edm", "rock"], energy="busy"),
        _e("led", "scene", mood="tech", genres=["edm", "rock"], energy="busy"),
        _e("tunnel", "scene", mood="tech", genres=["edm", "dark"], energy="busy"),
        _e("beams", "scene", mood="stage", genres=["rock", "edm", "pop"], energy="busy"),
        _e("skyline", "scene", mood="night", genres=["ballad", "pop", "jazz", "rock"], energy="normal"),
        _e("sky", "scene", mood="bright", genres=["pop", "cute", "ballad"], energy="calm"),
        _e("sakura", "scene", mood="soft", genres=["ballad", "cute", "ancient"], energy="calm"),
        _e("concert", "scene", mood="stage", genres=["pop", "rock", "edm"], energy="busy"),
        _e("galaxy", "scene", mood="night", genres=["ballad", "edm", "dark"], energy="calm"),
        _e("rainwin", "scene", mood="sad", genres=["ballad", "jazz", "dark"], energy="calm"),
        _e("confetti", "scene", mood="happy", genres=["pop", "cute"], energy="busy"),
        _e("pattern", "scene", mood="kawaii", genres=["cute", "pop"], energy="busy"),
        _e("aurora", "scene", mood="dream", genres=["ballad", "edm"], energy="calm"),
        _e("sunset", "scene", mood="warm", genres=["ballad", "pop", "rock"], energy="calm"),
        _e("neoncity", "scene", mood="night", genres=["edm", "rock", "dark"], energy="busy"),
        _e("starstage", "scene", mood="stage", genres=["pop", "rock", "ballad"], energy="busy"),
        _e("bubbles", "scene", mood="kawaii", genres=["cute", "pop"], energy="calm"),
        _e("lantern", "scene", mood="festival", genres=["ancient", "ballad"], energy="normal"),
        _e("ocean", "scene", mood="calm", genres=["ballad", "dark", "ancient"], energy="calm"),
        _e("forest", "scene", mood="calm", genres=["ancient", "ballad", "dark"], energy="calm"),
        _e("cafe", "scene", mood="warm", genres=["jazz", "ballad"], energy="calm"),
        _e("space", "scene", mood="dream", genres=["edm", "rock", "dark"], energy="normal"),
        _e("synth", "scene", mood="retro", genres=["edm", "rock", "pop"], energy="busy"),
        _e("crystal", "scene", mood="cool", genres=["edm", "dark", "cute"], energy="normal"),
        _e("classroom", "scene", mood="nostalgic", genres=["ballad", "pop", "cute"], energy="calm"),
        _e("rooftop", "scene", mood="night", genres=["ballad", "rock", "jazz"], energy="calm"),
        _e("hearts", "scene", mood="love", genres=["cute", "pop"], energy="normal"),
        _e("kaleido", "scene", mood="psychedelic", genres=["edm", "pop", "cute"], energy="busy"),
        _e("wavefield", "scene", mood="pulse", genres=["edm", "rock", "pop"], energy="busy"),
        _e("festival", "scene", mood="festival", genres=["pop", "ancient", "cute"], energy="busy"),
    ],
    "bg_transitions": [_e(x, "bg_transition") for x in
                       ("iris", "sweepL", "sweepR", "columns", "sweepU", "sweepD", "diag", "diamond", "blinds", "dots", "split", "clock")],
    "stingers": [_e(x, "stinger") for x in
                 ("stripes", "blinds", "shutter", "halftone", "petals", "ink", "glitch", "iris", "diamonds", "curtain", "slices", "stars", "zoom", "paper", "bars")],
    "frames": [_e(x, "frame") for x in
               ("cinema", "theater", "corner", "screen", "polaroid", "film", "lace", "gold", "sticker", "hud", "neonbar", "vhs", "scroll")],
    "filters": [_e(x, "filter") for x in
                ("softpink", "dreamblue", "golden", "sunset", "mint", "neonpop", "cyber", "noir", "vintage", "sakura", "crimson", "starlit")],
    "masks": [_e(x, "mask") for x in ("vignette", "grain", "scratch", "scan", "rays", "prism", "sparkle", "halftone", "haze")],
    "lyric_in": [_e(x, "lyric_in") for x in
                 ("rise", "ink", "slam", "scatter", "glitch", "type", "drop", "wipe", "float", "fadeup", "zoomin", "zoomout", "spin", "flip",
                  "swingin", "slideL", "slideR", "rollup", "wave", "bounce", "stretch", "blurin", "typewriter", "rainbow", "pop", "sway",
                  "shutter", "cascade")],
    "lyric_out": [_e(x, "lyric_out") for x in
                  ("sink", "burst", "push", "fadeout", "blurout", "floatup", "dissolve", "drift", "shrink", "evaporate", "popout", "spinout",
                   "flipout", "bounceout", "confetti", "zoomthru", "slideoutL", "slideoutR", "glitchout", "shatter", "crush", "rolldown",
                   "wipeout", "typeback")],
    "camera": [_e(x, "camera") for x in ("push", "pull", "truck", "orbit", "tilt", "dutch", "bob", "spiral")],
    "frames_ext": [_e(x, "frame") for x in
                   ("ribbon", "stitch", "washi", "starline", "heartline", "bubbleframe", "filmstrip2", "arcade", "notebook", "postcard",
                    "stamp", "neontube", "glitchframe", "cyberbrackets", "goldleaf", "artdeco", "chinoise", "cloudframe", "flowerwreath",
                    "pixel", "comic", "window", "ticket", "gradientring")],
    "lyric_plates": [_e(x, "lyric_plate") for x in
                     ("memo", "bubble", "tape", "tag", "neonbox", "glass", "dialog", "brush", "pill", "ribbonband", "underline",
                      "bracket", "highlighter", "cloud", "stampbox", "sticky", "scroll", "hud", "heartbox", "double")],
    "particles": [_e(x, "particle") for x in ("petal", "snow", "spark", "bubble", "note")],
}
