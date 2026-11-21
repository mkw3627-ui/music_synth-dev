"""单文件下载引擎（S3）：多源测速排序、Range 续传、尾部重叠校验、sha256、换源、磁盘满、暂停/取消。

纯标准库、无相对导入（便于 pytest 直接 import，也便于 S4 的独立守护进程调用）。
不 import 宿主 SDK，不读写插件配置，不依赖 setup.py / __init__.py。

主入口::

    from download_engine import download, EngineConfig, Progress

    res = download(
        dest=Path(r"D:\\models\\contentvec\\pytorch_model.bin"),
        sources=["https://hf-mirror.net/...", "https://huggingface.co/..."],
        size=378342945,
        sha256="d8dd400e...",
        on_progress=lambda p: print(p.state, p.bytes_done, p.speed_bps),
    )

``res`` 是 ``DownloadResult``（``ok``、``sha256``、``source``、``switches``、``bytes``）。

行为契约
--------
* **续传**：数据写 ``<dest>.part``；完成后校验 sha256 一致才 ``os.replace`` 到 ``dest``。
* **状态落盘**：``<dest>.part.json`` 每秒最多写一次，记录 ``{url, source, total,
  etag, last_modified, bytes, sha256_expected}``；进程被杀后重新调用 ``download()``
  会从 ``.part`` 的字节数继续（并对已有内容做一次增量哈希）。
* **换源**：每个源先用 Range 探 1MB 测速，从快到慢尝试；下载中每 ``slow_window_s``
  采样一次速度，满足以下任一条件就换源（保留 ``.part`` 续传）：

  - 连续 ``slow_windows`` 个窗口低于 ``slow_bps``（默认 200KB/s），且存在更快的备选源；
  - 当前 < ``slow_bps``，且当前 < 最快备选 × ``slow_ratio``（默认 0.35）；
  - 当前 < ``slow_bps``，且最快备选 ≥ 当前 × ``better_ratio``（默认 3）。

* **尾部校验**：换源续传前，向新源请求 ``[pos-64KB, pos)`` 与本地 ``.part`` 末尾
  比较；不一致 → 删 ``.part`` 从 0 重下，并把该源降权冷却。
* **卡死**：``stall_s``（默认 20 秒）内新增不足 ``stall_bytes``（默认 512KB）→ 判该源失败。
* **换源上限**：``max_switches``（默认 8），到上限后固定用历史最快源直到完成。
* **降权冷却**：被换掉/被判定损坏的源在 ``demote_cooldown_s``（默认 600 秒）内测速 ×0.5。
* **磁盘满**：写入抛 ``OSError``（``errno==28`` 或 ``winerror==112``）→ ``DiskFullError``，
  ``.part`` 保留，调用方稍后可用同一目标重试。
* **暂停/取消**：传入 ``pause``/``cancel`` 的 ``threading.Event``，每个 chunk 检查一次，
  分别抛 ``Paused`` / ``Canceled``；暂停保留 ``.part``，取消删除。
* **HTML 伪装**：响应 ``Content-Type`` 是 ``text/html`` 且体积很小 → 判该源失败。
* **单源失败不中断**：单源失败只记录并换下一个；全部失败且都像网络断开时等一会整体重试。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "EngineConfig", "Progress", "ProbeResult", "DownloadResult",
    "DownloadError", "AllSourcesFailed", "ChecksumError", "DiskFullError",
    "Paused", "Canceled", "SourceMismatch", "SourceTooSlow",
    "download", "download_artifact", "probe_sources", "read_part_state",
    "legacy_progress_string",
]

UA = "NEKO-music-synth/0.3 (+download-engine)"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class DownloadError(RuntimeError):
    """下载失败的基类。"""


class AllSourcesFailed(DownloadError):
    """所有候选源都失败，或校验失败重试次数耗尽。"""


class ChecksumError(DownloadError):
    """sha256 校验失败（重试后仍不一致）。"""


class DiskFullError(DownloadError):
    """磁盘空间不足；``.part`` 已保留，可稍后重试。"""


class Paused(DownloadError):
    """调用方请求暂停；``.part`` 已保留。"""


class Canceled(DownloadError):
    """调用方请求取消；``.part`` 已删除。"""


class SourceMismatch(DownloadError):
    """换源后尾部 64KB 对不上：该源内容与已下部分不同。"""


class SourceTooSlow(DownloadError):
    """当前源明显慢于备选源，需要换源（内部信号，不对外抛）。"""


class _Interrupted(DownloadError):
    """内部：连接中断/过短，可同源续传。"""


# ---------------------------------------------------------------------------
# 配置（默认值 = 计划书 B.3）
# ---------------------------------------------------------------------------
@dataclass
class EngineConfig:
    # 测速
    probe_bytes: int = 1 << 20
    probe_budget_s: float = 6.0
    probe_ttl_s: float = 600.0
    probe_workers: int = 8
    # 下载中的速度采样
    slow_window_s: float = 15.0
    slow_bps: float = 200 * 1024
    slow_ratio: float = 0.35
    better_ratio: float = 3.0
    #: 「当前低于此值且备选快 enough 倍就换」的下限。计划写的是 1MB/s：
    #: 200KB/s 以上但只有 300KB/s 的源也该让位给 2MB/s 的备选。
    better_floor_bps: float = 1024 * 1024
    slow_windows: int = 2
    # 卡死
    stall_s: float = 20.0
    stall_bytes: int = 512 * 1024
    # 换源
    max_switches: int = 8
    demote_cooldown_s: float = 600.0
    demote_factor: float = 0.5
    overlap_bytes: int = 64 * 1024
    resume_tries_per_source: int = 3
    checksum_retries: int = 2
    # IO
    chunk_size: int = 256 * 1024
    timeout: float = 60.0
    state_write_interval_s: float = 1.0
    # 全部源都像「网断了」时的整体重试预算
    net_wait_budget_s: float = 15 * 60.0
    # ---- 以下由 plugin.toml 的 download_* 键驱动（S12a）----
    #: 是否校验 sha256（download_verify_hash）。关掉只校验长度。
    verify_hash: bool = True
    #: 是否允许断点续传（download_resume）。关掉则总是从 0 重下。
    resume_enabled: bool = True
    #: 峰值限速（字节/秒，download_max_mbps×1MB）。0 = 不限。
    max_bps: int = 0
    #: 是否校验 TLS 证书（download_check_ssl）。关掉只用于自签镜像。
    check_ssl: bool = True
    #: 源偏好（download_mirror_preference）：auto | mirror | huggingface。
    mirror_preference: str = "auto"
    #: 源策略（download_source_strategy）：fastest | mirror_first | official_first。
    source_strategy: str = "fastest"


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    url: str
    ok: bool = False
    status: int = 0
    bps: float = 0.0
    total: int = 0
    content_type: str = ""
    range_ok: bool = False
    error: str = ""
    seconds: float = 0.0

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.url).hostname or "").lower()


@dataclass
class Progress:
    """结构化进度（替代旧的字符串回调）。"""

    state: str = "downloading"
    file: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    speed_bps: float = 0.0
    source: str = ""
    source_id: str = ""
    switches: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state, "file": self.file,
            "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
            "speed_bps": round(self.speed_bps, 1), "source": self.source,
            "source_id": self.source_id, "switches": self.switches,
            "error": self.error,
        }


@dataclass
class DownloadResult:
    ok: bool
    path: str
    bytes: int = 0
    sha256: str = ""
    source: str = ""
    switches: int = 0
    elapsed_s: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
_DISK_FULL_ERRNOS = {28, 112}      # ENOSPC / ERROR_DISK_FULL
_DISK_FULL_WINERRORS = {112, 39}   # ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL

_NET_HINTS = (
    "timed out", "timeout", "connection", "reset", "ssl", "tls", "eof",
    "incompleteread", "broken pipe", "getaddrinfo", "name or service",
    "temporary failure", "network is unreachable", "no route", "remote end closed",
    "10054", "10060", "10061", "11001", "502", "503", "504", "429",
)


def _is_disk_full(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "errno", None) in _DISK_FULL_ERRNOS:
        return True
    return getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS


def _is_html(content_type: str, size_seen: int) -> bool:
    return "text/html" in (content_type or "").lower() and size_seen < 200_000


def _looks_network(exc_or_text) -> bool:
    text = str(exc_or_text).lower()
    return any(h in text for h in _NET_HINTS)


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _parse_content_range(value: str):
    """``bytes 0-1023/4096`` → (start, end, total)；``*`` 总长记 0。"""
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value or "")
    if not m:
        return None
    total = 0 if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


def _part_paths(dest) -> tuple[Path, Path]:
    dest = Path(dest)
    return (dest.with_name(dest.name + ".part"),
            dest.with_name(dest.name + ".part.json"))


def read_part_state(dest) -> dict:
    """读取 ``<dest>.part.json``；不存在或损坏返回 {}。"""
    _, meta = _part_paths(dest)
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_catalog():
    """导入 download_catalog：包内走相对导入，独立运行走顶层导入。

    真宿主里插件是 ``plugin.plugins.music_synth`` 子包，绝对导入会失败（S12 实测）。
    """
    try:
        from . import download_catalog  # noqa: PLC0415 - 包内
    except ImportError:
        import download_catalog  # noqa: PLC0415 - 顶层
    return download_catalog


def _default_opener(check_ssl: bool = True):
    """直连 opener：不吃系统代理（测试都在 127.0.0.1，国内镜像也以直连最快）。

    ``check_ssl=False`` 只用于自签镜像（``download_check_ssl=false``）；默认校验证书。
    """
    handlers: list = [urllib.request.ProxyHandler({})]
    if not check_ssl:
        import ssl  # noqa: PLC0415
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


# ---------------------------------------------------------------------------
# 测速
# ---------------------------------------------------------------------------
class _Prober:
    """带缓存与降权冷却的源测速器。"""

    def __init__(self, cfg: EngineConfig, opener=None) -> None:
        self.cfg = cfg
        self._opener = opener or _default_opener(getattr(cfg, "check_ssl", True))
        self._cache: dict[str, tuple[float, ProbeResult]] = {}
        self._demoted: dict[str, float] = {}
        self._lock = threading.Lock()

    def demote(self, url: str) -> None:
        host = _host(url)
        if not host:
            return
        with self._lock:
            self._demoted[host] = time.time() + self.cfg.demote_cooldown_s

    def _factor(self, host: str) -> float:
        return self.cfg.demote_factor if (self._demoted.get(host) or 0.0) > time.time() else 1.0

    def probe(self, url: str) -> ProbeResult:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < self.cfg.probe_ttl_s:
            cached = ProbeResult(**vars(hit[1]))
        else:
            cached = self._probe_once(url)
            self._cache[url] = (time.time(), cached)
        out = ProbeResult(**vars(cached))
        out.bps *= self._factor(out.host)
        return out

    def _probe_once(self, url: str) -> ProbeResult:
        cfg = self.cfg
        out = ProbeResult(url=url)
        headers = {"User-Agent": UA, "Accept-Encoding": "identity",
                   "Range": f"bytes=0-{cfg.probe_bytes - 1}"}
        started = time.time()
        try:
            with self._opener.open(urllib.request.Request(url, headers=headers),
                                   timeout=cfg.probe_budget_s) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                out.status = status
                ctype = resp.headers.get("Content-Type", "") or ""
                out.content_type = ctype
                out.range_ok = status == 206
                crange = _parse_content_range(resp.headers.get("Content-Range", ""))
                if crange:
                    out.total = crange[2]
                elif status == 200:
                    clen = resp.headers.get("Content-Length", "")
                    out.total = int(clen) if clen.isdigit() else 0
                got = 0
                t0 = time.time()
                while got < cfg.probe_bytes and time.time() - t0 < cfg.probe_budget_s:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                out.bps = got / max(0.05, time.time() - t0)
                if _is_html(ctype, got):
                    out.error = "返回网页而非文件（镜像失效）"
                elif got <= 0:
                    out.error = "没有拿到字节"
                else:
                    out.ok = True
        except urllib.error.HTTPError as exc:
            out.status = int(exc.code)
            out.error = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - 测速失败即记不可用
            out.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        out.seconds = round(time.time() - started, 2)
        return out


def probe_sources(sources, *, cfg: EngineConfig | None = None,
                  opener=None) -> list[ProbeResult]:
    """并行测速，返回按速度降序的列表（不可用的排最后，仍保留）。"""
    cfg = cfg or EngineConfig()
    urls = [str(u) for u in sources]
    if not urls:
        return []
    prober = _Prober(cfg, opener)
    with ThreadPoolExecutor(max_workers=max(1, min(cfg.probe_workers, len(urls)))) as pool:
        results = list(pool.map(prober.probe, urls))
    results.sort(key=lambda r: (not r.ok, -r.bps))
    return results


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class _Engine:
    def __init__(self, dest: Path, sources, *, size: int = 0, sha256: str = "",
                 on_progress=None, cfg: EngineConfig | None = None,
                 pause=None, cancel=None, opener=None) -> None:
        self.dest = Path(dest)
        self.sources = list(dict.fromkeys(str(s) for s in sources if s))
        self.size = int(size or 0)
        self.sha256 = (sha256 or "").lower()
        self.cfg = cfg or EngineConfig()
        self.on_progress = on_progress
        self.pause = pause
        self.cancel = cancel
        self.opener = opener or _default_opener(getattr(self.cfg, "check_ssl", True))
        self.prober = _Prober(self.cfg, self.opener)
        self.part, self.meta = _part_paths(self.dest)

        self.switches = 0
        self.total = self.size
        self.etag = ""
        self.last_modified = ""
        self.current_url = ""
        self.failures: dict[str, int] = {u: 0 for u in self.sources}
        self.best_bps: dict[str, float] = {}
        self.last_rank: list[ProbeResult] = []
        self._last_state_write = 0.0
        self.digest = ""
        self._throttle_start = None
        self._throttle_bytes = 0
        # 内容校验失败 / 尾部不一致的源：本次下载内不再使用（否则每轮都会重新选中它）
        self.poisoned: set[str] = set()

    # -- 进度/状态 --------------------------------------------------------
    def _emit(self, state: str, done: int, total: int = 0, bps: float = 0.0,
              error: str = "") -> None:
        if not self.on_progress:
            return
        self.on_progress(Progress(
            state=state, file=self.dest.name, bytes_done=int(done),
            bytes_total=int(total or self.total or self.size or 0),
            speed_bps=float(bps), source=self.current_url,
            source_id=_host(self.current_url), switches=self.switches,
            error=error,
        ))

    def _save_state(self, done: int, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_state_write < self.cfg.state_write_interval_s:
            return
        self._last_state_write = now
        _write_json_atomic(self.meta, {
            "url": self.current_url, "source": _host(self.current_url),
            "total": self.total or self.size, "etag": self.etag,
            "last_modified": self.last_modified, "bytes": int(done),
            "sha256_expected": self.sha256, "updated_at": round(now, 3),
        })

    def _clear_state(self) -> None:
        try:
            self.meta.unlink(missing_ok=True)
        except OSError:
            pass

    def _reset_part(self) -> None:
        try:
            self.part.unlink(missing_ok=True)
        except OSError:
            pass
        self._clear_state()

    def _part_size(self) -> int:
        try:
            return self.part.stat().st_size
        except OSError:
            return 0

    def _file_size(self) -> int:
        try:
            return self.dest.stat().st_size
        except OSError:
            return 0

    def _throttle(self, nbytes: int) -> None:
        """峰值限速（``download_max_mbps``）：按平均速率 sleep 补差。"""
        limit = int(self.cfg.max_bps or 0)
        if limit <= 0:
            return
        now = time.time()
        if self._throttle_start is None:
            self._throttle_start = now
            self._throttle_bytes = 0
        self._throttle_bytes += nbytes
        elapsed = now - self._throttle_start
        expected = self._throttle_bytes / limit
        if expected > elapsed:
            time.sleep(min(1.0, expected - elapsed))

    def _check_gates(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Canceled("已取消")
        if self.pause is not None and self.pause.is_set():
            raise Paused("已暂停")

    # -- 主流程 -----------------------------------------------------------
    def run(self) -> DownloadResult:
        started = time.time()
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        if not self.sources:
            raise AllSourcesFailed("没有可用的下载源")
        if not self.cfg.resume_enabled and self._part_size() > 0:
            # download_resume=false：不续传，从 0 重下
            self._reset_part()

        checksum_failures = 0
        net_budget = self.cfg.net_wait_budget_s

        while True:
            self._check_gates()
            ranked = self._rank()
            if not ranked:
                raise AllSourcesFailed("所有源都测不通：" + self._error_summary())

            progressed = False
            for probe in ranked:
                try:
                    self._download_from(probe)
                    progressed = True
                    break
                except (Paused, Canceled, DiskFullError):
                    raise
                except ChecksumError as exc:
                    checksum_failures += 1
                    self._emit("verifying", self._part_size(), error=str(exc)[:200])
                    if checksum_failures > self.cfg.checksum_retries:
                        raise ChecksumError(
                            f"校验失败（文件已损坏），已从 {checksum_failures} 个源重试") from exc
                    progressed = True   # 还有重试预算，继续下一轮
                    break
                except SourceMismatch as exc:
                    self._emit("probing", 0, error=str(exc)[:200])
                except Exception as exc:  # noqa: BLE001 - 单源失败只记录
                    self.failures[probe.url] = self.failures.get(probe.url, 0) + 1
                    self._emit("probing", self._part_size(), error=str(exc)[:200])
                    if not _looks_network(exc):
                        self.prober.demote(probe.url)

            if progressed and self.dest.is_file():
                break
            if progressed:
                continue

            # 所有源都失败：都像网断了就等一会整体重试
            if net_budget > 0 and all(
                    _looks_network(f"{self.failures.get(u, 0)} {u}") or True
                    for u in self.sources):
                if self._all_failures_are_network():
                    wait_s = min(30.0, net_budget)
                    net_budget -= wait_s
                    self._emit("probing", self._part_size(),
                               error=f"网络断开，{wait_s:.0f} 秒后重试")
                    time.sleep(wait_s)
                    continue
            raise AllSourcesFailed("所有源都失败：" + self._error_summary())

        size = self._file_size()
        self._emit("done", size, size or self.total, 0.0)
        return DownloadResult(
            ok=True, path=str(self.dest), bytes=size, sha256=self.digest,
            source=self.current_url, switches=self.switches,
            elapsed_s=round(time.time() - started, 2),
        )

    def _all_failures_are_network(self) -> bool:
        return getattr(self, "_last_error_network", False) and all(
            v > 0 for v in self.failures.values())

    _last_error_network = False

    def _error_summary(self) -> str:
        parts = [f"{_host(u)}×{n}" for u, n in self.failures.items() if n]
        return "；".join(parts) or "无可用源"

    # -- 排序 -------------------------------------------------------------
    def _rank(self) -> list[ProbeResult]:
        """测速排序。内容不可信的源（poisoned）整轮排除，除非全都被排除。"""
        with ThreadPoolExecutor(
                max_workers=max(1, min(self.cfg.probe_workers, len(self.sources)))) as pool:
            results = list(pool.map(self.prober.probe, self.sources))
        for r in results:
            if r.ok:
                self.best_bps[r.url] = max(self.best_bps.get(r.url, 0.0), r.bps)

        if self.poisoned:
            clean = [r for r in results if r.url not in self.poisoned]
            if clean:
                results = clean

        pos = self._part_size()
        if pos > 0 and self.switches >= self.cfg.max_switches:
            usable = [r for r in results if r.ok]
            if usable:
                usable.sort(key=lambda r: -self.best_bps.get(r.url, r.bps))
                self.last_rank = usable
                return usable[:1]

        if pos > 0:
            ranged = [r for r in results if r.ok and r.range_ok]
            others = [r for r in results if not (r.ok and r.range_ok)]
            results = ranged + others
        else:
            results.sort(key=self._order_key)
        self.last_rank = results
        return results

    #: 国内镜像主机后缀（download_mirror_preference=mirror 时优先）
    _DOMESTIC_HINTS = ("modelscope.cn", "aliyun.com", "ustc.edu.cn",
                       "tencent.com", "tuna.tsinghua.edu.cn", "nju.edu.cn",
                       "hf-mirror.net", "gh-proxy.com", "isteed.cc", "ghproxy.net")

    def _order_key(self, r: ProbeResult):
        """按 ``source_strategy`` 排序。

        * ``fastest``（默认）：纯测速，与之前的实现一致。
        * ``mirror_first``：速度相差在 30% 以内时国内源排前。
        * ``official_first``：同理，官方源排前。
        """
        ok = not r.ok
        bps = -r.bps
        strategy = str(getattr(self.cfg, "source_strategy", "fastest") or "fastest")
        if strategy == "fastest":
            return (ok, bps)
        domestic = any(h in (r.host or "") for h in self._DOMESTIC_HINTS)
        prefer = domestic if strategy == "mirror_first" else not domestic
        # 量化到「相差 30% 视为同档」：同档内偏好优先，跨档仍按速度
        bucket = int(math.log(max(r.bps, 1.0)) / math.log(1.3))
        return (ok, -bucket, 0 if prefer else 1, bps)

    # -- 单源 -------------------------------------------------------------
    def _download_from(self, probe: ProbeResult) -> None:
        """对一个源尝试下载；成功则落盘返回，失败抛异常。"""
        self.current_url = probe.url
        tries = 0
        while True:
            self._check_gates()
            pos = self._part_size()
            if pos and not self._tail_matches(probe.url, pos):
                # 这个源与本地 .part 的末尾不同：可能是 .part 本身是旧文件/别的内容。
                # 重置 .part 后**重试同一个源**（从 0 开始），内容真伪交给最终 sha256 判定。
                self._reset_part()
                self._emit("probing", 0, error=f"{probe.host} 尾部校验不一致，重置后重下")
                continue
            try:
                self._transfer(probe, pos)
                return
            except SourceTooSlow as exc:
                # 太慢：立刻放弃这个源，交给上层换下一个（不要同源续传，否则会在两个源之间反复跳）
                raise DownloadError(str(exc)) from exc
            except _Interrupted as exc:
                tries += 1
                if tries > self.cfg.resume_tries_per_source:
                    raise DownloadError(str(exc)) from exc
                self._last_error_network = _looks_network(exc)
                continue

    def _tail_matches(self, url: str, pos: int) -> bool:
        """比对本地 .part 末尾与新源同区间；拿不到远端数据时放行（交给正式下载判定）。"""
        n = min(self.cfg.overlap_bytes, pos)
        if n <= 0:
            return True
        try:
            with self.part.open("rb") as fh:
                fh.seek(pos - n)
                local = fh.read(n)
        except OSError:
            return False
        if len(local) != n:
            return False
        headers = {"User-Agent": UA, "Accept-Encoding": "identity",
                   "Range": f"bytes={pos - n}-{pos - 1}"}
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers),
                                  timeout=self.cfg.timeout) as resp:
                if int(getattr(resp, "status", 200) or 200) != 206:
                    return True
                return resp.read(n) == local
        except Exception:  # noqa: BLE001 - 比对失败不阻断
            return True

    def _transfer(self, probe: ProbeResult, pos: int) -> None:
        url = probe.url
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if pos:
            headers["Range"] = f"bytes={pos}-"
        try:
            resp = self.opener.open(urllib.request.Request(url, headers=headers),
                                    timeout=self.cfg.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and pos:
                self._finish(pos)
                return
            self._last_error_network = _looks_network(f"{exc.code}")
            raise DownloadError(f"{_host(url)} HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            self._last_error_network = _looks_network(exc)
            raise DownloadError(f"{_host(url)} {type(exc).__name__}: {exc}") from exc

        with resp:
            status = int(getattr(resp, "status", 200) or 200)
            ctype = resp.headers.get("Content-Type", "") or ""
            if _is_html(ctype, 0):
                raise DownloadError(f"{_host(url)} 返回网页而非文件（镜像失效）")

            crange = _parse_content_range(resp.headers.get("Content-Range", ""))
            if crange:
                self.total = crange[2] or self.total
                if pos and crange[0] != pos:
                    # 服务端给的不是我们要的起点：不能盲写
                    raise DownloadError(f"{_host(url)} 返回区间起点不符（{crange[0]} != {pos}）")
            elif status == 200:
                clen = resp.headers.get("Content-Length", "")
                self.total = int(clen) if clen.isdigit() else self.total

            if status == 200 and pos:
                # 源忽略 Range：追加会写坏文件。只有一个可用源时从 0 重下，否则换源。
                if self._solo_source(url):
                    self._reset_part()
                    pos = 0
                else:
                    raise DownloadError(f"{_host(url)} 不支持断点续传（忽略 Range）")

            etag = resp.headers.get("ETag", "") or ""
            modified = resp.headers.get("Last-Modified", "") or ""
            if pos and self.etag and etag and etag != self.etag:
                self._reset_part()
                pos = 0
            self.etag, self.last_modified = etag, modified

            hasher = self._hash_prefix(pos)
            done = pos
            base = pos
            win_start = time.time()
            win_bytes = 0
            win_slow = 0
            stall_start = time.time()
            mode = "ab" if pos else "wb"
            self._emit("downloading", done, self.total)

            with self.part.open(mode) as fh:
                while True:
                    self._check_gates()
                    try:
                        chunk = resp.read(self.cfg.chunk_size)
                    except (OSError, ValueError) as exc:
                        # 连接被掐断/超时：.part 已有数据，可同源续传
                        self._save_state(done, force=True)
                        raise _Interrupted(f"{_host(url)} 连接中断：{exc}") from exc
                    if not chunk:
                        break
                    try:
                        fh.write(chunk)
                    except OSError as exc:
                        if _is_disk_full(exc):
                            self._save_state(done, force=True)
                            raise DiskFullError(
                                f"磁盘空间不足（已下 {done} 字节，.part 已保留）") from exc
                        raise
                    hasher.update(chunk)
                    done += len(chunk)
                    win_bytes += len(chunk)
                    self._throttle(len(chunk))
                    self._save_state(done)
                    self._emit("downloading", done, self.total,
                               win_bytes / max(0.001, time.time() - win_start))

                    if (time.time() - stall_start > self.cfg.stall_s
                            and done - base < self.cfg.stall_bytes):
                        self._save_state(done, force=True)
                        raise _Interrupted(
                            f"{_host(url)} 速度过慢（{self.cfg.stall_s:.0f} 秒 "
                            f"{done - base} 字节）")

                    if time.time() - win_start >= self.cfg.slow_window_s:
                        bps = win_bytes / max(0.001, time.time() - win_start)
                        win_start, win_bytes = time.time(), 0
                        win_slow = win_slow + 1 if bps < self.cfg.slow_bps else 0
                        if self._should_switch(url, bps, win_slow):
                            self._save_state(done, force=True)
                            self.switches += 1
                            self.prober.demote(url)
                            self._emit("probing", done,
                                       error=f"{_host(url)} 太慢（{bps / 1024:.0f}KB/s），换源")
                            raise SourceTooSlow(
                                f"{_host(url)} 只有 {bps / 1024:.0f}KB/s，换源")

        size = self._part_size()
        if self.total and size < self.total:
            raise _Interrupted(f"{_host(url)} 下载不完整：{size}/{self.total} 字节")
        self._finish(size, hasher)

    def _solo_source(self, url: str) -> bool:
        usable = [r for r in self.last_rank if r.ok]
        return len(usable) <= 1

    def _should_switch(self, url: str, bps: float, win_slow: int) -> bool:
        if self.switches >= self.cfg.max_switches:
            return False
        others = [r for r in self.last_rank if r.ok and r.url != url]
        if not others:
            return False
        best_other = max((r.bps for r in others), default=0.0)
        if best_other <= 0:
            return False
        # 规则 1：连续 N 个窗口低于 slow_bps 且存在更快的备选
        if win_slow >= self.cfg.slow_windows and bps < best_other:
            return True
        # 规则 2：明显低于最快备选的比例（沿用 0.35），且本身确实慢
        if bps < self.cfg.slow_bps and bps < best_other * self.cfg.slow_ratio:
            return True
        # 规则 3（计划原文）：当前 < 1MB/s 且备选源实测快 better_ratio 倍以上就换。
        # 这一条不受 slow_bps 限制——「200KB/s 以上但仍很慢」正是它要解决的场景。
        if bps < self.cfg.better_floor_bps and                 best_other >= max(bps, 1.0) * self.cfg.better_ratio:
            return True
        return False

    def _hash_prefix(self, pos: int):
        hasher = hashlib.sha256()
        if pos <= 0:
            return hasher
        try:
            with self.part.open("rb") as fh:
                left = pos
                while left > 0:
                    block = fh.read(min(4 << 20, left))
                    if not block:
                        break
                    hasher.update(block)
                    left -= len(block)
        except OSError:
            pass
        return hasher

    def _finish(self, size: int, hasher=None) -> None:
        if hasher is None:
            hasher = self._hash_prefix(size)
        self._emit("verifying", size, self.total or size)
        digest = hasher.hexdigest()
        if self.cfg.verify_hash and self.sha256 and digest != self.sha256:
            self._reset_part()
            self.prober.demote(self.current_url)
            # 这个源的内容确定不对，本次下载内不再用
            if self.current_url:
                self.poisoned.add(self.current_url)
            raise ChecksumError(
                f"{self.dest.name} 校验失败：{digest[:16]}… != {self.sha256[:16]}…")
        try:
            os.replace(self.part, self.dest)
        except OSError as exc:
            if _is_disk_full(exc):
                raise DiskFullError("磁盘空间不足（改名阶段）") from exc
            raise
        self.digest = digest
        self._clear_state()


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def download(dest, sources, *, size: int = 0, sha256: str = "", on_progress=None,
             cfg: EngineConfig | None = None, pause=None, cancel=None,
             opener=None) -> DownloadResult:
    """下载单个文件到 ``dest``。

    ``sources`` 是同一文件的多个 URL（顺序仅供参考，实际按测速排序）。
    ``sha256`` 给定时校验内容；不给则只校验长度。
    """
    engine = _Engine(Path(dest), list(sources), size=size, sha256=sha256,
                     on_progress=on_progress, cfg=cfg, pause=pause,
                     cancel=cancel, opener=opener)
    try:
        return engine.run()
    except Canceled:
        engine._reset_part()
        raise


def download_artifact(key: str, paths: dict, *, cfg: EngineConfig | None = None,
                      on_progress=None, pause=None, cancel=None,
                      catalog=None) -> dict:
    """按 ``download_catalog`` 的条目把一个下载物下完。

    支持单文件（``kind`` 为 file/zip/dlc）与多文件（``sep``）条目。
    ``repo`` 型（ACE / SoulX）需要「先列文件清单再逐文件下」，由 S4 的守护进程
    负责；这里直接报错提示调用方逐文件调用 :func:`download`。

    返回 ``{"key", "ok", "files": [DownloadResult...], "error"}``。
    """
    if catalog is None:            # 延迟导入：本模块不硬依赖 catalog
        catalog = _load_catalog()

    spec = catalog.ARTIFACTS[key]
    if spec["kind"] == "repo":
        raise DownloadError(
            f"{key} 是 repo 型（{spec.get('repo_id')}）：请先列文件清单，再逐文件调用 download()")

    target = Path(catalog.target_path(key, paths))
    results: list[DownloadResult] = []
    errors: list[str] = []

    def _one(dest: Path, urls: list[str], size: int, sha: str, label: str) -> None:
        try:
            results.append(download(
                dest, urls, size=size, sha256=sha or "", cfg=cfg,
                on_progress=on_progress, pause=pause, cancel=cancel))
        except (Paused, Canceled, DiskFullError):
            raise
        except Exception as exc:  # noqa: BLE001 - 多文件时收集，最后统一报
            errors.append(f"{label}: {exc}")

    files = spec.get("files")
    if files:
        # 多文件：每个文件一套源（见 download_catalog.file_entries）
        target.mkdir(parents=True, exist_ok=True)
        for name, fsha, fsize, urls in _load_catalog().file_entries(spec):  # 纯函数，测试替身 catalog 也能用
            _one(target / name, urls, fsize, fsha, name)
    else:
        _one(target, list(spec.get("sources") or []),
             int(spec.get("size") or 0), spec.get("sha256") or "", target.name)

    return {
        "key": key,
        "ok": bool(results) and all(r.ok for r in results) and not errors,
        "files": [r.__dict__ for r in results],
        "error": "；".join(errors),
    }


def legacy_progress_string(label: str, p: Progress) -> str:
    """旧格式进度串，供 ``setup._setup_progress`` 的正则反解：

    必须匹配 ``r"：\\d+/\\d+ MB"``（或 GB 形式），与原实现一致。
    """
    if p.bytes_total:
        return f"{label}：{p.bytes_done / 2 ** 20:.0f}/{p.bytes_total / 2 ** 20:.0f} MB"
    if p.bytes_done:
        return f"{label}：{p.bytes_done / 2 ** 30:.2f} GB"
    return f"{label}：准备下载"
