"""本地假镜像 HTTP 服务（S2 测试夹具）。

纯标准库，无相对导入、不 import 宿主 SDK，便于 pytest 直接 import，
也支持以 CLI 方式独立启动/停止（供下载引擎做续传/换源/校验失败/断连测试）。

一个 ``FakeMirror`` 在一个 127.0.0.1 端口上起一个多线程 HTTP 服务，服务若干个
"路由"（route）。每个路由对应一个 ``Content``（可复现的确定性字节，附参考
sha256），并带一份 ``Fault`` 配置，可分别模拟：

* 限速（rate_bps，运行中可改，即"动态调速"）
* 忽略 Range（返回 200 全文，模拟"不支持 Range"的源；默认则是正确实现 206 续传）
* 第 K 字节后断连（发送 K 字节后 RST 连接）
* 篡改内容（把中间某个字节取反 → 校验失败）
* 返回 HTML 网页而非文件（``text/html`` 200，模拟镜像失效跳转页）
* 5xx 服务端错误
* 首字节延迟（first_byte_delay_s）

CLI 用法（在 music_synth-dev 目录下）::

    # 前台启动（Ctrl+C 停止）
    python tests/dl_harness/fake_mirrors.py serve --port 8765 --size 5MB --rate 500000

    # 后台启动 + 一条命令停止
    python tests/dl_harness/fake_mirrors.py start --port 8765 --size 10MB --pidfile _tmp_mirror.pid
    python tests/dl_harness/fake_mirrors.py stop  --port 8765
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import os
import random
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

KB = 1024
MB = 1024 * 1024

_SERVE_CHUNK = 32 * 1024        # 每次读/写的最小粒度，用于限速与断连精度
_HASH_CHUNK = 1 << 20           # 计算 sha256 时的分块
_BLOCK_SIZE = 1 << 20           # 确定性内容的分块
_DEFAULT_HTML = (
    b"<!DOCTYPE html><html><head><title>mirror</title></head>"
    b"<body><h1>502 Bad Gateway</h1><p>This mirror has been moved.</p></body></html>"
)


# ---------------------------------------------------------------------------
# 确定性内容（固定种子可复现，支持任意 offset 随机访问）
# ---------------------------------------------------------------------------

_BLOCK_CACHE: dict[tuple[int, int], bytes] = {}
_BLOCK_CACHE_LOCK = threading.Lock()


def _block(seed: int, index: int) -> bytes:
    key = (seed, index)
    with _BLOCK_CACHE_LOCK:
        hit = _BLOCK_CACHE.get(key)
    if hit is not None:
        return hit
    # 固定种子的伪随机块；同 (seed, index) 永远得到相同字节。
    rng = random.Random((seed * 0x9E3779B97F4A7C15 + index) & 0xFFFFFFFFFFFFFFFF)
    data = rng.randbytes(_BLOCK_SIZE)
    with _BLOCK_CACHE_LOCK:
        _BLOCK_CACHE[key] = data
    return data


def deterministic_bytes(offset: int, length: int, *, seed: int = 0) -> bytes:
    """返回 [offset, offset+length) 的确定性伪随机字节（只依赖 seed 与区间）。"""
    out = bytearray()
    pos = offset
    remaining = length
    while remaining > 0:
        idx = pos // _BLOCK_SIZE
        start = pos % _BLOCK_SIZE
        n = min(remaining, _BLOCK_SIZE - start)
        out += _block(seed, idx)[start:start + n]
        pos += n
        remaining -= n
    return bytes(out)


class Content:
    """一个测试负载：确定性或显式字节 + 参考 sha256。

    支持三种来源：
    * ``data`` 显式字节（内存）；
    * ``path`` 磁盘文件（按需 seek+read，适合大文件）；
    * ``size``+``seed`` 确定性内容（小文件内存生成，大文件按块即时生成）。
    """

    def __init__(
        self,
        size: Optional[int] = None,
        *,
        seed: int = 0,
        data: Optional[bytes] = None,
        path: Optional[Union[str, Path]] = None,
    ) -> None:
        self.seed = seed
        self._data: Optional[bytes] = None
        self._path: Optional[Path] = None
        if data is not None:
            self._data = bytes(data)
            self.size = len(self._data)
        elif path is not None:
            self._path = Path(path)
            self.size = self._path.stat().st_size
        else:
            assert size is not None, "需要 size / data / path 之一"
            self.size = int(size)
        self._sha: Optional[str] = None

    def read(self, offset: int, length: int) -> bytes:
        if self._data is not None:
            return self._data[offset:offset + length]
        if self._path is not None:
            with open(self._path, "rb") as fh:
                fh.seek(offset)
                return fh.read(length)
        return deterministic_bytes(offset, length, seed=self.seed)

    def sha256(self) -> str:
        if self._sha is None:
            h = hashlib.sha256()
            start = 0
            while start < self.size:
                h.update(self.read(start, min(_HASH_CHUNK, self.size - start)))
                start += _HASH_CHUNK
            self._sha = h.hexdigest()
        return self._sha


def make_content(
    size: int = 1 * MB,
    *,
    seed: int = 0,
    data: Optional[bytes] = None,
    path: Optional[Union[str, Path]] = None,
) -> Content:
    """构造测试负载；确定性内容的 sha256 用 ``content.sha256()`` 取得。"""
    return Content(size=size, seed=seed, data=data, path=path)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 故障配置与路由
# ---------------------------------------------------------------------------

@dataclass
class Fault:
    """单个路由可叠加的故障模式（全部可选，默认全部关闭）。"""

    rate_bps: Optional[int] = None          # 限速（字节/秒）；None=不限；运行中可改
    ignore_range: bool = False              # True=忽略 Range，返回 200 全文
    disconnect_after: Optional[int] = None  # 发送这么多字节后 RST 断连
    tamper: bool = False                    # 把内容中间一个字节取反 → sha 不匹配
    tamper_offset: Optional[int] = None     # 篡改位置；默认 content.size // 2
    html: bool = False                      # True=返回 HTML 网页而非文件字节
    status: int = 200                       # 覆盖为其它状态码（如 500）
    first_byte_delay_s: float = 0.0         # 响应首字节前的延时（秒）


@dataclass
class Artifact:
    """一条路由：某路径上服务的负载 + 故障配置。"""

    content: Content
    content_type: str = "application/octet-stream"
    fault: Fault = field(default_factory=Fault)


def _normalize_path(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return path


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静默访问日志
        pass

    @property
    def mirror(self) -> "FakeMirror":
        return self.server.mirror  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle(no_body=True)

    def _handle(self, no_body: bool = False) -> None:
        path = _normalize_path(self.path)
        mirror = self.mirror

        if path == "/__shutdown__":
            self._send_text(200, "ok", no_body)
            threading.Thread(target=mirror._request_shutdown, daemon=True).start()
            return
        if path == "/__health__":
            self._send_text(200, "ok", no_body)
            return

        artifact = mirror.artifacts.get(path)
        if artifact is None:
            self._send_text(404, f"no route for {path}", no_body)
            return

        fault = artifact.fault
        if fault.first_byte_delay_s > 0:
            time.sleep(fault.first_byte_delay_s)

        if fault.status >= 400:
            body = f"server error {fault.status}".encode("utf-8")
            self._send(fault.status, "text/plain", body if not no_body else b"")
            return

        if fault.html:
            self._send(200, "text/html; charset=utf-8", _DEFAULT_HTML if not no_body else b"")
            return

        self._serve_content(artifact, no_body)

    def _send_text(self, status: int, text: str, no_body: bool) -> None:
        body = text.encode("utf-8")
        self._send(status, "text/plain; charset=utf-8", body if not no_body else b"")

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _serve_content(self, artifact: Artifact, no_body: bool) -> None:
        content = artifact.content
        fault = artifact.fault
        total = content.size

        if fault.ignore_range:
            start, end, status = 0, total - 1, 200
        else:
            resolved = self._resolve_range(total)
            if resolved is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, start, end = resolved

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", artifact.content_type)
        self.send_header(
            "Accept-Ranges",
            "none" if fault.ignore_range else "bytes",
        )
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if no_body:
            return

        tamper_offset = (
            fault.tamper_offset if fault.tamper_offset is not None else total // 2
        )
        pos = start
        remaining = length
        sent = 0
        t0 = time.monotonic()
        while remaining > 0:
            if fault.disconnect_after is not None and sent >= fault.disconnect_after:
                self._abort_connection()
                return
            limit = _SERVE_CHUNK
            if fault.disconnect_after is not None:
                limit = min(limit, fault.disconnect_after - sent)
            chunk = min(limit, remaining)
            data = content.read(pos, chunk)
            if fault.tamper and pos <= tamper_offset < pos + len(data):
                data = bytearray(data)
                data[tamper_offset - pos] ^= 0xFF
                data = bytes(data)
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            n = len(data)
            pos += n
            sent += n
            remaining -= n
            if fault.rate_bps and fault.rate_bps > 0:
                expected = sent / fault.rate_bps
                elapsed = time.monotonic() - t0
                if expected > elapsed:
                    time.sleep(expected - elapsed)

    def _resolve_range(self, total: int) -> Optional[tuple[int, int, int]]:
        """解析 Range 头，返回 (status, start, end)；无法满足返回 None。

        * 无 Range -> (200, 0, total-1)；
        * ``bytes=a-b`` / ``bytes=a-`` / ``bytes=-n`` -> (206, a, b)；
        * 起点越界 / 格式非法 -> None（由调用方回 416）。
        """
        header = self.headers.get("Range")
        if not header:
            return 200, 0, total - 1
        if not header.startswith("bytes="):
            return None
        spec = header[len("bytes="):].strip()
        if spec == "" or "-" not in spec:
            return None
        left, _, right = spec.partition("-")
        left = left.strip()
        right = right.strip()
        if left == "":
            # suffix: bytes=-N
            try:
                n = int(right)
            except ValueError:
                return None
            if n <= 0:
                return None
            return 206, max(0, total - n), total - 1
        try:
            start = int(left)
        except ValueError:
            return None
        if right == "":
            end = total - 1
        else:
            try:
                end = int(right)
            except ValueError:
                return None
            end = min(end, total - 1)
        if start > end or start >= total:
            return None
        return 206, start, end

    def _abort_connection(self) -> None:
        """模拟"中途断连"：已写 ``disconnect_after`` 字节后干净地关闭连接。

        响应头的 Content-Length 仍是完整长度，客户端会读到不足的字节后遇到
        EOF（``http.client.IncompleteRead``，``.partial`` 长度即已收到的字节数），
        从而精确、可复现地触发"传输截断"。
        """
        try:
            self.wfile.flush()
        except OSError:
            pass
        try:
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 镜像服务与集群
# ---------------------------------------------------------------------------

class FakeMirror:
    """一个端口上的假镜像服务。用 ``add()`` 挂路由，``start()`` 启动。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self.artifacts: dict[str, Artifact] = {}
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def add(
        self,
        path: str,
        content: Content,
        *,
        fault: Optional[Fault] = None,
        content_type: str = "application/octet-stream",
    ) -> "FakeMirror":
        self.artifacts[_normalize_path(path)] = Artifact(
            content=content,
            content_type=content_type,
            fault=fault if fault is not None else Fault(),
        )
        return self

    def get(self, path: str) -> Optional[Artifact]:
        return self.artifacts.get(_normalize_path(path))

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str = "/file.bin") -> str:
        return self.base + _normalize_path(path)

    def start(self) -> "FakeMirror":
        if self._httpd is not None:
            return self
        httpd = _ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.mirror = self  # type: ignore[attr-defined]
        self.port = httpd.server_address[1]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def _request_shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "FakeMirror":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


class MirrorCluster:
    """一组 ``FakeMirror``（每个一个端口），统一管理生命周期。"""

    def __init__(self) -> None:
        self.mirrors: list[FakeMirror] = []

    def add(self, mirror: FakeMirror) -> None:
        self.mirrors.append(mirror)

    def __len__(self) -> int:
        return len(self.mirrors)

    def __getitem__(self, i: int) -> FakeMirror:
        return self.mirrors[i]

    def __iter__(self):
        return iter(self.mirrors)

    @property
    def bases(self) -> list[str]:
        return [m.base for m in self.mirrors]

    def urls(self, path: str = "/file.bin") -> list[str]:
        return [m.url(path) for m in self.mirrors]

    def stop(self) -> None:
        for m in self.mirrors:
            m.stop()

    def __enter__(self) -> "MirrorCluster":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def serve_mirrors(
    specs: Union[int, list[Optional[Fault]]],
    *,
    content: Optional[Content] = None,
    path: str = "/file.bin",
    size: Optional[int] = None,
    seed: int = 0,
    data: Optional[bytes] = None,
) -> MirrorCluster:
    """起 1..N 个假镜像，每个在随机端口服务同一负载（不同故障配置）。

    ``specs``：整数 N（N 个无故障镜像），或 Fault/dict/None 列表（逐镜像配置）。
    返回 ``MirrorCluster``，可用 ``with serve_mirrors(...) as m:`` 或 ``m.stop()``。
    """
    if content is None:
        content = make_content(size if size is not None else 1 * MB, seed=seed, data=data)
    if isinstance(specs, int):
        specs = [Fault()] * specs
    cluster = MirrorCluster()
    for spec in specs:
        if isinstance(spec, Fault):
            fault = spec
        elif isinstance(spec, dict):
            fault = Fault(**spec)
        else:
            fault = Fault()
        mirror = FakeMirror().add(path, content, fault=fault).start()
        cluster.add(mirror)
    return cluster


# ---------------------------------------------------------------------------
# CLI：serve / start / stop
# ---------------------------------------------------------------------------

def _parse_size(text: str) -> int:
    text = text.strip().lower()
    if text.endswith("mb"):
        return int(float(text[:-2]) * MB)
    if text.endswith("kb"):
        return int(float(text[:-2]) * KB)
    if text.endswith("b"):
        return int(text[:-1])
    return int(text)


def _fault_from_args(args: argparse.Namespace) -> Fault:
    return Fault(
        rate_bps=args.rate,
        ignore_range=args.ignore_range,
        disconnect_after=args.disconnect_after,
        tamper=args.tamper,
        html=args.html,
        status=args.status,
        first_byte_delay_s=args.delay,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="本地假镜像 HTTP 服务")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--port", type=int, default=8765)
        sp.add_argument("--size", type=_parse_size, default=5 * MB, help="如 5MB/100KB")
        sp.add_argument("--seed", type=int, default=0)
        sp.add_argument("--path", default="/file.bin")
        sp.add_argument("--rate", type=int, default=None, help="限速 字节/秒")
        sp.add_argument("--ignore-range", action="store_true")
        sp.add_argument("--disconnect-after", type=int, default=None, help="发送这么多字节后断连")
        sp.add_argument("--tamper", action="store_true")
        sp.add_argument("--html", action="store_true")
        sp.add_argument("--status", type=int, default=200)
        sp.add_argument("--delay", type=float, default=0.0, help="首字节延迟秒")

    sp_serve = sub.add_parser("serve", help="前台启动（Ctrl+C 停止）")
    common(sp_serve)

    sp_start = sub.add_parser("start", help="后台启动（配合 stop 停止）")
    common(sp_start)
    sp_start.add_argument("--pidfile", default="_tmp_mirror.pid")

    sp_stop = sub.add_parser("stop", help="通过 /__shutdown__ 停止已启动的服务")
    sp_stop.add_argument("--port", type=int, default=8765)
    sp_stop.add_argument("--pidfile", default="_tmp_mirror.pid")
    return p


def _serve_forever(args: argparse.Namespace) -> int:
    content = make_content(args.size, seed=args.seed)
    mirror = FakeMirror(port=args.port).add(
        args.path, content, fault=_fault_from_args(args)
    ).start()
    print(f"fake mirror at {mirror.url(args.path)}  size={content.size}  sha256={content.sha256()}")
    print(f"admin: {mirror.base}/__health__  |  {mirror.base}/__shutdown__")
    print("Ctrl+C to stop", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        mirror.stop()
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    # 在 Windows 上用独立进程后台启动 serve，写 pidfile。
    argv = [
        sys.executable,
        "-I",
        os.path.abspath(__file__),
        "serve",
        "--port", str(args.port),
        "--size", str(args.size),
        "--seed", str(args.seed),
        "--path", args.path,
    ]
    if args.rate:
        argv += ["--rate", str(args.rate)]
    if args.ignore_range:
        argv += ["--ignore-range"]
    if args.disconnect_after:
        argv += ["--disconnect-after", str(args.disconnect_after)]
    if args.tamper:
        argv += ["--tamper"]
    if args.html:
        argv += ["--html"]
    if args.status != 200:
        argv += ["--status", str(args.status)]
    if args.delay:
        argv += ["--delay", str(args.delay)]

    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    Path(args.pidfile).write_text(str(proc.pid), encoding="utf-8")
    print(f"started pid={proc.pid} port={args.port} (stop with: {sys.argv[0]} stop --port {args.port})")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{args.port}/__shutdown__", timeout=3).read()
        print(f"stopped server on port {args.port}")
    except Exception as exc:  # noqa: BLE001 - 服务可能已停
        print(f"shutdown request failed (maybe already stopped): {exc}")
    try:
        Path(args.pidfile).unlink(missing_ok=True)
    except OSError:
        pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "serve":
        return _serve_forever(args)
    if args.cmd == "start":
        return _cmd_start(args)
    if args.cmd == "stop":
        return _cmd_stop(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
