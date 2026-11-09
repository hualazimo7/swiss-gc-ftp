#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Swiss httpd 的本地模拟器 —— 供 --selftest 在没有 GameCube 时验证协议层。

刻意照抄 cube/swiss/source/httpd.c 的解析方式（一次 recv(1024) 读请求头、
strstr 找参数、url_decode 截断规则、响应头格式），所以只要自检通过，
请求格式就能被真机接受；反过来，参数顺序写错也会在这里立刻暴露。

    python tools/mock_swiss.py            # 监听 127.0.0.1:18080
    python tools/mock_swiss.py --port 18080 --self-test
"""

from __future__ import annotations

import argparse
import os
import re
import socketserver
import threading
import urllib.parse

HTTP_200 = b"HTTP/1.1 200 OK\r\n"
TEXT_HDR = b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
HTML_HDR = b"Content-Type: text/html\r\n\r\n"
INDEX = (b"<html><head><title>Swiss httpd page</title></head><body>"
         b"POST /upload to write a file</body></html>")


def url_decode(src: str) -> str:
    """固件版：遇 & / 空格 / CR / LF 停止，%XX 按字节解码（中文靠 UTF-8 还原）。"""
    raw = bytearray()
    i = 0
    while i < len(src) and src[i] not in "& \r\n":
        if src[i] == "%" and i + 2 < len(src):
            try:
                raw.append(int(src[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        raw += src[i].encode("utf-8", "replace")
        i += 1
    return raw.decode("utf-8", "replace")


class MockFS:
    """极简内存 FatFs：{完整路径: bytes 或 None(目录)}。"""

    def __init__(self):
        self.files = {
            "sdb:/": None,
            "sda:/": None,
            "sdb:/games": None,
            "sdb:/swiss": None,
            "sdb:/swiss/settings": None,
            "sdb:/games/塞尔达传说 风之杖.iso": b"WII" * 4096,
            "sdb:/games/metroid_prime.iso": b"\x00" * 12345,
            "sdb:/swiss/settings/global.ini": b"[Swiss]\nFTPHostIP=192.168.1.100\n",
            "sdb:/IPL.dol": b"DOL\0" * 1000,
        }

    def norm(self, p: str) -> str:
        p = p.rstrip("/")
        if p.endswith(":"):
            p += "/"                    # 卷根 sdb:/ 去掉尾斜杠后要补回来
        return p

    @staticmethod
    def vol_of(p: str) -> str:
        m = re.match(r"^([A-Za-z0-9]+:/)", p)
        return m.group(1) if m else ""

    def listdir(self, path: str):
        path = self.norm(path)
        if path not in self.files or self.files[path] is not None:
            return None
        out = []
        for k, v in self.files.items():
            if k == path or not k.startswith(path.rstrip("/") + "/"):
                continue
            rest = k[len(path.rstrip("/")) + 1:]
            if "/" in rest:
                continue
            out.append((rest, 0 if v is None else len(v), v is None))
        return sorted(out, key=lambda e: (not e[2], e[0].lower()))

    def read(self, path: str):
        return self.files.get(self.norm(path))


class MockHandler(socketserver.StreamRequestHandler):
    fs: MockFS = None

    def send(self, data: bytes):
        try:
            self.request.sendall(data)
        except OSError:
            pass

    def handle(self):
        self.request.settimeout(5)
        try:
            head = self.request.recv(1024)
        except OSError:
            return
        if not head:
            return
        text = head.decode("latin-1", "replace")
        body_off = len(head)
        m = re.search(r"\r\n\r\n", text)
        hdrend = m.start() + 4 if m else None
        if hdrend is not None:
            body_off = hdrend
        line = text.split("\r\n", 1)[0]

        # ---- GET / ----------------------------------------------------------
        if line.startswith("GET / HTTP/1.1"):
            self.send(HTTP_200 + HTML_HDR + INDEX)
        # ---- GET /state -----------------------------------------------------
        elif line.startswith("GET /state"):
            self.send(HTTP_200 + TEXT_HDR +
                      b"dev=SD Card - Slot B\npath=sdb:/games/\n"
                      b"sel=0 count=2\nselname=metroid_prime.iso\n"
                      b"seltype=1\nloc=0 menu=0\n")
        # ---- GET /ls?path= --------------------------------------------------
        elif line.startswith("GET /ls"):
            q = text.find("path=")
            path = url_decode(text[q + 5:]) if q >= 0 else "sdb:/"
            if q >= 0 and q > body_off:
                path = "sdb:/"
            items = self.fs.listdir(path)
            if items is None:
                self.send(HTTP_200 + TEXT_HDR +
                          ("error: cannot open %s\n" % path).encode("utf-8"))
            else:
                body = "".join("%s %d %s\n" % ("D" if d else "F", size, name)
                               for name, size, d in items).encode("utf-8")
                self.send(HTTP_200 + TEXT_HDR + body)
        # ---- POST /upload?name=&root= ---------------------------------------
        elif line.startswith("POST /upload"):
            qn, qr, qc = text.find("name="), text.find("root="), text.find("Content-Length:")
            name = url_decode(text[qn + 5:]) if qn >= 0 and qn < body_off else ""
            root = url_decode(text[qr + 5:]) if qr >= 0 and qr < body_off else "sdb:/"
            clen = int(text[qc + 15:].split("\r\n", 1)[0].strip()) if qc >= 0 and qc < body_off else 0
            body = head[body_off:] if hdrend is not None else b""
            while len(body) < clen:
                chunk = self.request.recv(min(2048, clen - len(body)))
                if not chunk:
                    break
                body += chunk
            if not name or ".." in name or ".." in root:
                resp = b"error: bad name\r\n"
            else:
                path = self.fs.norm(root.rstrip("/") + "/" + name.lstrip("/"))
                parent = path.rsplit("/", 1)[0]
                if parent.endswith(":"):
                    parent += "/"                       # sdb:/xxx.iso -> sdb:/
                if self.fs.vol_of(path) not in ("sdb:/", "sda:/") or \
                        parent not in self.fs.files or \
                        self.fs.files[parent] is not None:
                    resp = ("error: f_open(%s) = 5\r\n" % path).encode("utf-8")
                else:
                    self.fs.files[path] = body[:clen]
                    resp = ("ok %d/%d bytes -> %s\r\n" %
                            (len(body[:clen]), clen, path)).encode("utf-8")
            self.send(HTTP_200 + HTML_HDR + resp)
        # ---- GET /get?path= -------------------------------------------------
        elif line.startswith("GET /get"):
            q = text.find("path=")
            path = url_decode(text[q + 5:]) if q >= 0 and q < body_off else ""
            data = self.fs.read(path)
            if data is None:
                self.send(HTTP_200 + TEXT_HDR +
                          ("error: f_open(%s) = 4\n" % path).encode("utf-8"))
            else:
                hdr = ("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                       "Content-Length: %d\r\n\r\n" % len(data)).encode("ascii")
                self.send(hdr + data)
        # ---- GET /rm?path= --------------------------------------------------
        elif line.startswith("GET /rm"):
            q = text.find("path=")
            path = url_decode(text[q + 5:]) if q >= 0 and q < body_off else ""
            if path in self.fs.files:
                del self.fs.files[path]
                self.send(HTTP_200 + TEXT_HDR +
                          ("ok removed %s\n" % path).encode("utf-8"))
            else:
                self.send(HTTP_200 + TEXT_HDR +
                          ("error: f_unlink(%s) = 4\n" % path).encode("utf-8"))
        # ---- GET /rename?from=&to= ------------------------------------------
        elif line.startswith("GET /rename"):
            qf, qt = text.find("from="), text.find("&to=")
            src = url_decode(text[qf + 5:]) if qf >= 0 and (qt < 0 or qf < qt) else ""
            dst = url_decode(text[qt + 4:]) if qt >= 0 else ""
            if not src or not dst or ".." in src or ".." in dst:
                self.send(HTTP_200 + TEXT_HDR + b"error: bad path\n")
            elif src not in self.fs.files:
                self.send(HTTP_200 + TEXT_HDR +
                          ("error: f_rename(%s) = 4\n" % src).encode("utf-8"))
            else:
                self.fs.files[dst] = self.fs.files.pop(src)
                self.send(HTTP_200 + TEXT_HDR +
                          ("ok %s -> %s\n" % (src, dst)).encode("utf-8"))
        else:
            self.send(HTTP_200 + TEXT_HDR + b"error: unknown endpoint\n")


class MockServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, fs=None):
        self.fs = fs or MockFS()
        MockHandler.fs = self.fs
        super().__init__(addr, MockHandler)


def start(port=0, fs=None):
    """在后台线程启动，返回 (server, port)。port=0 由系统分配。"""
    srv = MockServer(("127.0.0.1", port), fs)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        import swiss_client
        srv, port = start(0)
        try:
            c = swiss_client.SwissClient("127.0.0.1", port)
            print("ping:", c.ping())
            print("volume:", c.detect_volume())
            print("root:", [(e.name, e.size, e.is_dir) for e in c.listdir("sdb:/")])
            print("state:", c.state())
        finally:
            srv.shutdown()
        return 0
    srv, port = start(args.port)
    print("mock Swiss httpd on 127.0.0.1:%d (Ctrl+C to stop)" % port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
