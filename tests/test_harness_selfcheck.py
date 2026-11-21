"""S2 夹具自检：证明假镜像的每种故障模式都能被真实触发。

只依赖标准库 + ``dl_harness.fake_mirrors``，不触碰插件代码。
对应计划 D-S2 各场景：限速误差 ±20%、断连字节点准确、篡改可被 sha 发现、
HTML 200、忽略 Range 返回 200 全文，另补充 5xx / 正确 206 续传 / 首字节延迟 /
多源并行 / 动态调速 / sha256 参考值。
"""
from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request

from dl_harness.fake_mirrors import (
    KB,
    MB,
    Fault,
    make_content,
    serve_mirrors,
    sha256_of,
)

# 大文件下载的统一超时
_TIMEOUT = 60


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.status, dict(resp.headers), resp.read()


def _read_until_truncation(url: str, headers: dict | None = None) -> tuple[int, int]:
    """下载并把"中途断连"归一化为 (收到字节数, 状态码)。"""
    req = urllib.request.Request(url, headers=headers or {})
    received = 0
    status = 0
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            status = resp.status
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
    except http.client.IncompleteRead as exc:
        received += len(exc.partial)
    except (ConnectionResetError, urllib.error.URLError, OSError):
        pass
    return received, status


def _download_stream(url: str, on_chunk=None) -> tuple[int, int]:
    """流式下载，每读一块回调 on_chunk(累计字节)；返回 (总字节, 状态码)。"""
    req = urllib.request.Request(url)
    received = 0
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        status = resp.status
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if on_chunk is not None:
                on_chunk(received)
    return received, status


# ---------------------------------------------------------------------------
# 内容确定性 / sha256
# ---------------------------------------------------------------------------

def test_content_sha256_reproducible():
    a = make_content(2 * MB, seed=42)
    b = make_content(2 * MB, seed=42)
    assert a.sha256() == b.sha256()
    assert len(a.sha256()) == 64
    # 不同种子 -> 不同 sha
    c = make_content(2 * MB, seed=43)
    assert a.sha256() != c.sha256()


def test_deterministic_bytes_random_access():
    content = make_content(3 * MB, seed=7)
    whole = content.read(0, content.size)
    assert len(whole) == content.size
    # 任意区间拼接 == 全量
    assert content.read(0, 100) == whole[:100]
    assert content.read(1 * MB, 500) == whole[1 * MB:1 * MB + 500]
    assert content.read(content.size - 3, 3) == whole[-3:]
    assert sha256_of(whole) == content.sha256()


# ---------------------------------------------------------------------------
# 限速（±20%）
# ---------------------------------------------------------------------------

def test_rate_limit_within_tolerance():
    size = 1 * MB
    rate = 250 * KB  # ~4 秒，远大于 Windows sleep 粒度，误差可控
    cluster = serve_mirrors([Fault(rate_bps=rate)], content=make_content(size, seed=1),
                            path="/f.bin")
    try:
        url = cluster[0].url("/f.bin")
        t0 = time.monotonic()
        _, _, body = _get(url)
        elapsed = time.monotonic() - t0
        assert len(body) == size
        measured = size / elapsed
        # 限速误差 ±20%
        assert rate * 0.8 <= measured <= rate * 1.2, f"measured={measured:.0f} B/s"
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 中途断连（字节点准确）
# ---------------------------------------------------------------------------

def test_disconnect_exact_byte_count():
    content = make_content(2 * MB, seed=2)
    cut = 1 * MB + 12345
    cluster = serve_mirrors([Fault(disconnect_after=cut)], content=content, path="/f.bin")
    try:
        received, status = _read_until_truncation(cluster[0].url("/f.bin"))
        assert status == 200
        assert received == cut, f"expected {cut} bytes before cut, got {received}"
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 篡改内容（校验失败）
# ---------------------------------------------------------------------------

def test_tamper_changes_sha():
    content = make_content(1 * MB, seed=3)
    cluster = serve_mirrors([Fault(tamper=True)], content=content, path="/f.bin")
    try:
        _, headers, body = _get(cluster[0].url("/f.bin"))
        assert headers.get("Content-Type", "").startswith("application/octet-stream")
        assert len(body) == content.size  # 篡改不改变长度
        assert sha256_of(body) != content.sha256()  # 但内容变了 -> 校验失败
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# HTML 网页伪装
# ---------------------------------------------------------------------------

def test_html_impersonation():
    content = make_content(512 * KB, seed=4)
    cluster = serve_mirrors([Fault(html=True)], content=content, path="/f.bin")
    try:
        _, headers, body = _get(cluster[0].url("/f.bin"))
        assert "text/html" in headers.get("Content-Type", "")
        assert body.lstrip().startswith(b"<!DOCTYPE html>") or b"<html" in body
        assert body != content.read(0, min(512, content.size))  # 不是文件字节
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 忽略 Range（返回 200 全文）
# ---------------------------------------------------------------------------

def test_ignore_range_returns_200_full_body():
    content = make_content(2 * MB, seed=5)
    cluster = serve_mirrors([Fault(ignore_range=True)], content=content, path="/f.bin")
    try:
        status, headers, body = _get(
            cluster[0].url("/f.bin"), headers={"Range": "bytes=100000-199999"}
        )
        assert status == 200
        assert "Content-Range" not in headers
        assert len(body) == content.size  # 全文，而非 100000 字节片段
        assert sha256_of(body) == content.sha256()
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 正确 206 续传语义
# ---------------------------------------------------------------------------

def test_correct_range_206_partial():
    content = make_content(2 * MB, seed=6)
    cluster = serve_mirrors(1, content=content, path="/f.bin")
    try:
        start, end = 100000, 199999
        status, headers, body = _get(
            cluster[0].url("/f.bin"), headers={"Range": f"bytes={start}-{end}"}
        )
        assert status == 206
        assert headers.get("Content-Range") == f"bytes {start}-{end}/{content.size}"
        assert len(body) == end - start + 1
        assert body == content.read(start, end - start + 1)
    finally:
        cluster.stop()


def test_range_suffix_and_open_ended():
    content = make_content(1 * MB, seed=8)
    cluster = serve_mirrors(1, content=content, path="/f.bin")
    try:
        # 后缀：bytes=-100
        status, _, body = _get(cluster[0].url("/f.bin"), headers={"Range": "bytes=-100"})
        assert status == 206
        assert body == content.read(content.size - 100, 100)
        # 开区间：bytes=500000-
        status, _, body = _get(cluster[0].url("/f.bin"), headers={"Range": "bytes=500000-"})
        assert status == 206
        assert body == content.read(500000, content.size - 500000)
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 5xx 服务端错误
# ---------------------------------------------------------------------------

def test_5xx_status():
    content = make_content(64 * KB, seed=9)
    cluster = serve_mirrors([Fault(status=500)], content=content, path="/f.bin")
    try:
        with urllib.request.urlopen(cluster[0].url("/f.bin"), timeout=_TIMEOUT) as resp:
            status = resp.status
            _ = resp.read()
        assert status == 500
    except urllib.error.HTTPError as exc:
        assert exc.code == 500
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 首字节延迟
# ---------------------------------------------------------------------------

def test_first_byte_delay():
    content = make_content(128 * KB, seed=10)
    delay = 1.0
    cluster = serve_mirrors([Fault(first_byte_delay_s=delay)], content=content, path="/f.bin")
    try:
        t0 = time.monotonic()
        _get(cluster[0].url("/f.bin"))
        elapsed = time.monotonic() - t0
        assert elapsed >= delay
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 多源并行（同内容，不同故障，可同时取）
# ---------------------------------------------------------------------------

def test_multiple_sources_parallel_same_sha():
    content = make_content(1 * MB, seed=11)
    cluster = serve_mirrors(
        [Fault(), Fault(rate_bps=4 * MB), Fault(ignore_range=True)],
        content=content,
        path="/f.bin",
    )
    try:
        assert len(cluster) == 3
        assert len(cluster.urls("/f.bin")) == 3
        for url in cluster.urls("/f.bin"):
            _, _, body = _get(url)
            assert sha256_of(body) == content.sha256()
    finally:
        cluster.stop()


# ---------------------------------------------------------------------------
# 动态调速（运行中改速度生效）
# ---------------------------------------------------------------------------

def test_dynamic_rate_change():
    content = make_content(1536 * KB, seed=12)  # 1.5MB
    cluster = serve_mirrors(1, content=content, path="/f.bin")
    artifact = cluster[0].get("/f.bin")
    artifact.fault.rate_bps = 1 * MB  # 阶段 1：1MB/s

    def on_chunk(received):
        if received >= 1 * MB:
            artifact.fault.rate_bps = 128 * KB  # 阶段 2：降到 128KB/s

    t0 = time.monotonic()
    received, status = _download_stream(cluster[0].url("/f.bin"), on_chunk=on_chunk)
    elapsed = time.monotonic() - t0
    try:
        assert status == 200
        assert received == content.size
        # 阶段 1 约 1s + 阶段 2 (512KB @128KB/s) 约 4s；若调速未生效会远快于 4s。
        assert elapsed >= 4.0, f"elapsed={elapsed:.2f}s，说明调速未生效"
    finally:
        cluster.stop()
