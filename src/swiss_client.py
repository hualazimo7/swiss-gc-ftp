#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Swiss (GameCube) 传输协议层 —— PC 端文件传输工具的底座。

Swiss 固件里跑的是 httpd（80 端口，单线程、每连接一个请求），我们复用它已经
实现的 FatFs 端点，把 SD 卡变成一个可浏览、可读写的文件系统：

    GET  /ls?path=sdb:/games/        列目录，UTF-8，行格式 "D 0 name" / "F 123 name"
    POST /upload?name=..&root=..      写文件（body 为原始字节，必须带 Content-Length）
    GET  /get?path=..                 读文件（binary，带 Content-Length）
    GET  /rm?path=..                  删除
    GET  /rename?from=..&to=..        改名 / 移动
    GET  /state                       当前界面状态（busy=1 时表示 UI 正在扫描）

注意事项（都是实机踩过的坑）：
  * httpd 用一次 net_recv(1024) 读请求头，所以请求头必须一次发完且 < 1000 字节。
  * 查询串是固件自己用 strstr + url_decode 解析的，参数名大小写、顺序都敏感；
    /rename 必须是 "from=...&to=..."。
  * 所有路径参数必须 percent-encode，中文名才能以 UTF-8 原样落到 FatFs
    （FF_LFN_UNICODE=2）。
  * FatFs 卷只在 Swiss 当前设备是那块存储时挂载；切到 SMB/FTP/FSP 后
    /upload /get /rename 会返回 FR_NOT_ENABLED(12)。
  * 服务端不提供 mkdir / 断点续传 / 并发；本模块用一把锁把所有请求串行化。

只依赖标准库，可单独用于脚本：

    from swiss_client import SwissClient
    c = SwissClient("192.168.1.2")
    print(c.detect_volume())
    for e in c.listdir(c.volume): print(e.name, e.size)
    c.upload("D:/game.iso", "sdb:/games/game.iso")
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass

__all__ = [
    "SwissClient", "SwissError", "SwissCancelled", "Entry", "Response",
    "DEFAULT_HOST", "DEFAULT_PORT", "VOLUMES", "join_remote", "parent_remote",
    "split_remote", "human_size",
]

DEFAULT_HOST = "192.168.1.2"
DEFAULT_PORT = 80
VOLUMES = ("sdb:/", "sda:/", "sdc:/")  # 与 httpd.c: pick_upload_root 顺序一致
MAX_HEADER = 1000                       # 固件 temp[1026]，留点余量
FAT32_MAX = 0xFFFFFFFF                  # 单文件 4 GiB - 1


class SwissError(Exception):
    """与 Swiss 通信失败，或 Swiss 返回 error: 开头的说明。"""


class SwissCancelled(SwissError):
    """用户取消了本次传输。"""


@dataclass
class Entry:
    name: str
    size: int
    is_dir: bool

    @property
    def kind(self) -> str:
        return "文件夹" if self.is_dir else "文件"


@dataclass
class Response:
    status: str
    headers: dict
    body: bytes
    written: int = 0            # 上传时实际发给服务端的字节数

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.status.startswith("HTTP/1.1 200")


def quote(value: str, safe: str = "") -> str:
    """固件 url_decode 会在 & / 空格 / CR / LF 处截断，所以一律编码。"""
    return urllib.parse.quote(value, safe=safe, encoding="utf-8", errors="strict")


def join_remote(base: str, name: str) -> str:
    """base 既可以是卷根 sdb:/ 也可以是子目录，返回不带尾斜杠的路径。"""
    if not base.endswith("/"):
        base += "/"
    return (base + name.lstrip("/")).rstrip("/")


def parent_remote(path: str) -> str:
    """返回上一级目录，始终带卷前缀和尾斜杠（卷根形如 sdb:/）。"""
    p = path.rstrip("/")
    prefix = split_remote(p)[0]     # 形如 sdb:/，本身已带斜杠
    rest = p[len(prefix):]
    if not rest or "/" not in rest:
        return prefix
    return prefix + rest.rsplit("/", 1)[0] + "/"


def split_remote(path: str) -> tuple:
    """("sdb:/", "games/foo.iso")。"""
    m = re.match(r"^([A-Za-z0-9]+:/)(.*)$", path)
    if not m:
        raise SwissError("远端路径格式不对（应形如 sdb:/games/）：%s" % path)
    return m.group(1), m.group(2)


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return "-"


class SwissClient:
    """线程安全的 Swiss httpd 客户端（内部串行，因为固件是单线程 accept）。"""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 connect_timeout=4.0, io_timeout=30.0, volume=None):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self.volume = volume or VOLUMES[0]
        self._lock = threading.RLock()
        self.last_error = ""

    # ------------------------------------------------------------------ low level
    def _request(self, method: str, path: str, *, body_path: str = None,
                 body_size: int = None, progress=None, cancel=None,
                 sink=None, chunk: int = 65536) -> Response:
        """发一个请求并读回完整响应。sink 为可写文件对象时，响应体直接落盘。"""
        with self._lock:
            head = "%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n" % (
                method, path, self.host)
            if body_size is not None:
                head += "Content-Length: %d\r\n" % body_size
            head += "\r\n"
            try:
                raw = head.encode("ascii")
            except UnicodeEncodeError as exc:
                raise SwissError("请求行必须全部是 ASCII（路径要 percent-encode）：%s" % exc)
            if len(raw) > MAX_HEADER:
                raise SwissError("请求头 %d 字节，超过 Swiss httpd 的 1024 字节上限；"
                                 "请缩短目录层级或文件名" % len(raw))

            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.connect_timeout)
            sock.settimeout(self.io_timeout)
            self.last_error = ""
            try:
                sock.sendall(raw)

                # ---- 请求体（只有 /upload 用得到）
                sent = 0
                started = time.time()
                if body_path is not None:
                    with open(body_path, "rb") as fh:
                        while sent < body_size:
                            if cancel is not None and cancel.is_set():
                                raise SwissCancelled("已取消")
                            block = fh.read(min(chunk, body_size - sent))
                            if not block:
                                raise SwissError("本地文件在传输中被截断：%s" % body_path)
                            sock.sendall(block)
                            sent += len(block)
                            if progress:
                                progress(sent, body_size,
                                         sent / max(1e-6, time.time() - started))

                # ---- 响应头：逐字节读，避免把二进制 body 读进解析缓冲
                hdr = bytearray()
                while len(hdr) < 8192:
                    b = sock.recv(1)
                    if not b:
                        break
                    hdr += b
                    if hdr.endswith(b"\r\n\r\n"):
                        break
                if not hdr.endswith(b"\r\n\r\n"):
                    raise SwissError("Swiss 没有返回完整 HTTP 响应头（连接被提前关闭）")

                lines = bytes(hdr).decode("latin-1").split("\r\n")
                status = lines[0] if lines else ""
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                if not status.startswith("HTTP/1.1 200"):
                    raise SwissError("Swiss 返回 %s" % status.strip())

                # ---- 响应体
                length = headers.get("content-length")
                want = int(length) if length and length.isdigit() else None
                ctype = headers.get("content-type", "")
                streamed = sink is not None and "octet-stream" in ctype
                buf = bytearray()
                got = 0
                while True:
                    if want is not None and got >= want:
                        break
                    n = chunk if want is None else min(chunk, want - got)
                    block = sock.recv(n)
                    if not block:
                        break
                    got += len(block)
                    if streamed:
                        sink.write(block)
                        if progress:
                            progress(got, want or got,
                                     got / max(1e-6, time.time() - started))
                    else:
                        buf += block
                        if len(buf) > 16 * 1024 * 1024:
                            raise SwissError("响应体超过 16 MiB，疑似协议错乱")
                if want is not None and got < want:
                    raise SwissError("响应体不完整：%d/%d 字节" % (got, want))

                body = b"" if streamed else bytes(buf)
                if not streamed:
                    text = body.decode("utf-8", "replace")
                    if text.lstrip().startswith("error:"):
                        raise SwissError(text.strip())
                return Response(status, headers, body, sent)
            except socket.timeout:
                raise SwissError("与 Swiss 通信超时（%g 秒无数据）。"
                                 "主机可能已退出 Swiss 或正在读盘" % self.io_timeout)
            except OSError as exc:
                raise SwissError("网络错误：%s" % exc)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _expect_ok(resp: Response, what: str) -> str:
        text = resp.text.strip()
        if not text.startswith("ok "):
            raise SwissError("%s 失败：%s" % (what, text or "(空响应)"))
        return text

    # ------------------------------------------------------------------ discovery
    def get(self, path: str, **kw) -> Response:
        return self._request("GET", path, **kw)

    def ping(self, timeout=None) -> bool:
        """仅确认 httpd 活着（/ 的标题页）。"""
        old = self.connect_timeout
        try:
            if timeout is not None:
                self.connect_timeout = timeout
            r = self.get("/")
            return "Swiss httpd" in r.text
        except SwissError:
            return False
        finally:
            self.connect_timeout = old

    def state(self) -> dict:
        """{'dev':..,'path':..,'sel':..,'count':..} 或 {'busy':'1'}。"""
        r = self.get("/state")
        out = {}
        for line in r.text.splitlines():
            # path= / selname= 的值可能含空格，整行取值；最后一行的
            # "sel=N count=M" 和 "loc=N menu=M" 才按空格拆 token。
            if line.startswith(("path=", "selname=", "dev=")):
                k, v = line.split("=", 1)
                out[k] = v
                continue
            for token in line.split():
                if "=" in token:
                    k, v = token.split("=", 1)
                    out[k] = v
        return out

    def detect_volume(self) -> str:
        """按固件 pick_upload_root 的顺序找第一个可用 FatFs 卷。"""
        for vol in VOLUMES:
            r = self.get("/ls?path=" + quote(vol, safe=":/"))
            if not r.text.lstrip().startswith("error:"):
                self.volume = vol
                return vol
        raise SwissError("找不到可用的 SD 卡卷（%s）。"
                         "请确认 Swiss 主界面当前设备是 SD 卡" % ", ".join(VOLUMES))

    # ------------------------------------------------------------------ operations
    def listdir(self, remote_dir: str) -> list:
        """返回 [Entry]，目录在前、按名称排序。"""
        path = remote_dir if remote_dir.endswith("/") else remote_dir + "/"
        r = self.get("/ls?path=" + quote(path, safe=":/"))
        entries = []
        for line in r.text.splitlines():
            line = line.rstrip("\r")
            if not line or len(line) < 4:
                continue
            if line.startswith("error:"):
                raise SwissError(line.strip())
            parts = line.split(" ", 2)
            if len(parts) != 3 or parts[0] not in ("D", "F"):
                continue
            try:
                size = int(parts[1])
            except ValueError:
                size = 0
            name = parts[2]
            if name in (".", ".."):
                continue
            entries.append(Entry(name, size, parts[0] == "D"))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def upload(self, local_path: str, remote_path: str, *, progress=None,
               cancel=None, verify=False) -> str:
        """把本地文件写到远端。remote_path 为完整路径（含卷前缀）。"""
        if not os.path.isfile(local_path):
            raise SwissError("本地文件不存在：%s" % local_path)
        size = os.path.getsize(local_path)
        if size == 0:
            raise SwissError("拒绝上传空文件（固件 f_open 会建出 0 字节文件且无法确认写回）：%s"
                             % local_path)
        if size > FAT32_MAX:
            raise SwissError("单文件超过 FAT32 上限 4 GiB：%s" % local_path)
        vol, rest = split_remote(remote_path)
        if rest.endswith("/"):
            raise SwissError("上传目标必须是完整文件名：%s" % remote_path)
        if "/" in rest:
            root = vol + rest.rsplit("/", 1)[0] + "/"
            name = rest.rsplit("/", 1)[1]
        else:
            root, name = vol, rest
        if ".." in name or ".." in root:
            raise SwissError("固件拒绝含 '..' 的路径：%s" % remote_path)
        path = "/upload?name=%s&root=%s" % (quote(name), quote(root, safe=":/"))
        r = self._request("POST", path, body_path=local_path, body_size=size,
                          progress=progress, cancel=cancel)
        reply = self._expect_ok(r, "上传")
        m = re.search(r"^ok (\d+)/(\d+) bytes", reply)
        if m and (int(m.group(1)) != size or int(m.group(2)) != size):
            raise SwissError("Swiss 只写入了 %s/%d 字节：%s" % (m.group(1), size, reply))
        if verify:
            entries = self.listdir(root)
            hit = next((e for e in entries if e.name == name), None)
            if hit is None or hit.size != size:
                raise SwissError("上传后校验失败：%s 远端大小 %s，应为 %d 字节"
                                 % (remote_path, hit.size if hit else "缺失", size))
        return reply

    def download(self, remote_path: str, local_path: str, *, progress=None,
                 cancel=None) -> int:
        """读回远端文件，返回写入字节数。"""
        parent = os.path.dirname(os.path.abspath(local_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = local_path + ".part"
        try:
            with open(tmp, "wb") as sink:
                r = self._request("GET", "/get?path=" + quote(remote_path, safe=":/"),
                                  progress=progress, cancel=cancel, sink=sink)
            if "octet-stream" not in r.headers.get("content-type", ""):
                # 出错时固件返回 text/plain 的 error: 说明
                raise SwissError(r.text.strip() or "Swiss 没有返回文件数据")
            os.replace(tmp, local_path)
            return os.path.getsize(local_path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def delete(self, remote_path: str) -> str:
        r = self.get("/rm?path=" + quote(remote_path, safe=":/"))
        return self._expect_ok(r, "删除")

    def rename(self, src: str, dst: str) -> str:
        if ".." in src or ".." in dst:
            raise SwissError("固件拒绝含 '..' 的路径")
        # 固件 strstr(temp, "&to=")，顺序不能反
        path = "/rename?from=%s&to=%s" % (quote(src, safe=":/"), quote(dst, safe=":/"))
        return self._expect_ok(self.get(path), "改名")

    def exists(self, remote_dir: str, name: str) -> Entry | None:
        try:
            return next((e for e in self.listdir(remote_dir) if e.name == name), None)
        except SwissError:
            return None
