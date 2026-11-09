#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Swiss 的 SD 卡映射成一个标准 FTP 站点（PC 本机 127.0.0.1:2121）。

SwissFTP 自带的双栏界面已经能传文件；这个网关让 FileZilla、Windows 资源管理器、
curl、ftplib 等任意 FTP 客户端也能直接读写同一张 SD 卡：

    ftp://127.0.0.1:2121    用户/密码任意（默认 anonymous 也行）

网关把 FTP 命令翻译成 Swiss httpd 的 FatFs 端点（/ls /get /upload /rm /rename），
自己不碰 SD 卡。因为固件 /upload 要求预先给出 Content-Length，而 FTP 的 STOR
是流式、事先不知道长度，STOR 会先落盘到临时文件再整文件转发（所以大文件
会占用等量本地磁盘、且没有实时进度）。

限制（都是固件侧）：
  * 没有 mkdir/RMD：目标目录必须已存在；
  * 单控制连接、单数据连接、被动模式；
  * 覆盖式写入，不支持断点续传 / REST；
  * 所有请求经 SwissClient 串行化（httpd 是单线程）。
"""

from __future__ import annotations

import os
import socket
import socketserver
import tempfile
import threading
import time

from swiss_client import SwissClient, SwissError, join_remote, parent_remote, split_remote

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 2121


def _unix_listing(entries) -> bytes:
    lines = []
    for e in entries:
        if e.is_dir:
            perm, size = "drwxr-xr-x", 0
        else:
            perm, size = "-rw-r--r--", e.size
        lines.append("%s 1 gc gc %d Jan 01 00:00 %s" % (perm, size, e.name))
    return ("\r\n".join(lines) + ("\r\n" if lines else "")).encode("utf-8")


class GatewayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, client: SwissClient, log=print):
        self.client = client
        self.log = log
        super().__init__(addr, SessionHandler)


class SessionHandler(socketserver.StreamRequestHandler):
    """一个控制连接一个实例；PASV 的数据连接在命令处理里现开现收。"""

    def setup(self):
        super().setup()
        self.cwd = self.server.client.volume       # 形如 sdb:/，本身已带斜杠
        self.pasv_sock = None
        self.rename_from = None
        self.utf8 = True
        self.username = None

    def finish(self):
        try:
            if self.pasv_sock:
                self.pasv_sock.close()
        except OSError:
            pass
        super().finish()

    # ------------------------------------------------------------------ helpers
    def reply(self, code, text=""):
        msg = "%d %s\r\n" % (code, text) if text else "%d\r\n" % code
        try:
            self.wfile.write(msg.encode("utf-8", "replace"))
            self.wfile.flush()
        except OSError:
            pass

    def remote(self, arg: str) -> str:
        """把 FTP 客户端给的路径映射到 FatFs 完整路径（相对当前 CWD）。"""
        arg = (arg or "").strip().strip('"')
        vol, cur = split_remote(self.cwd)
        if arg.startswith("/"):
            parts = []                       # 绝对路径从卷根开始
        else:
            parts = [p for p in cur.split("/") if p and p != "."]
        # 禁止 .. 越出卷根
        for part in arg.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return vol + "/".join(parts)

    def dir_of(self, arg: str) -> str:
        p = self.remote(arg)
        return p.rstrip("/") + "/"

    def open_data(self, timeout=25.0):
        """取用 PASV 监听 socket，等客户端连上来。"""
        if self.pasv_sock is None:
            self.reply(425, "Use PASV/EPSV first.")
            return None
        sock = self.pasv_sock
        self.pasv_sock = None
        try:
            sock.settimeout(timeout)
            conn, _addr = sock.accept()
            conn.settimeout(timeout)
            sock.close()
            return conn
        except OSError as exc:
            try:
                sock.close()
            except OSError:
                pass
            self.reply(425, "Data connection failed: %s" % exc)
            return None

    def send_data(self, payload: bytes) -> bool:
        conn = self.open_data()
        if conn is None:
            return False
        try:
            conn.sendall(payload)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            conn.close()
        self.reply(226, "Transfer complete.")
        return True

    def recv_data(self, dest: str) -> bool:
        conn = self.open_data()
        if conn is None:
            return False
        try:
            with open(dest, "wb") as fh:
                while True:
                    block = conn.recv(65536)
                    if not block:
                        break
                    fh.write(block)
        except OSError:
            pass
        finally:
            conn.close()
        return True

    # ------------------------------------------------------------------ commands
    def handle(self):
        self.request.settimeout(300)
        self.reply(220, "SwissFTP gateway ready (SD card via Swiss httpd)")
        while True:
            try:
                raw = self.rfile.readline(8192)
            except (OSError, socket.timeout):
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            arg = arg.strip()
            try:
                if not self.dispatch(cmd, arg):
                    break
            except SwissError as exc:
                self._drop_pasv()
                self.reply(550, str(exc))
            except Exception as exc:                 # 单个命令出错不该踢掉连接
                self._drop_pasv()
                self.server.log("gateway error %s %s: %s" % (cmd, arg, exc))
                self.reply(451, "Local error: %s" % exc)

    def _drop_pasv(self):
        if self.pasv_sock:
            try:
                self.pasv_sock.close()
            except OSError:
                pass
            self.pasv_sock = None

    def dispatch(self, cmd: str, arg: str) -> bool:
        c = self.server.client
        log = self.server.log
        if cmd == "USER":
            self.username = arg or "anonymous"
            self.reply(331, "Password required.")
        elif cmd == "PASS":
            self.reply(230, "Logged in. Root is %s" % split_remote(self.cwd)[0])
        elif cmd in ("AUTH", "PBSZ", "PROT", "CCC"):
            self.reply(502 if cmd == "AUTH" else 200, "Not needed / unsupported.")
        elif cmd == "SYST":
            self.reply(215, "UNIX Type: L8")
        elif cmd == "FEAT":
            self.wfile.write(b"211-Features:\r\n UTF8\r\n SIZE\r\n PASV\r\n EPSV\r\n"
                             b" MDTM\r\n211 End\r\n")
            self.wfile.flush()
        elif cmd == "OPTS":
            self.utf8 = "UTF8" in arg.upper()
            self.reply(200, "OK")
        elif cmd == "TYPE":
            self.reply(200, "Type set to %s" % (arg or "I"))
        elif cmd == "NOOP":
            self.reply(200, "OK")
        elif cmd == "PWD" or cmd == "XPWD":
            vol, rest = split_remote(self.cwd)
            self.reply(257, '"/%s" is the current directory' % rest.rstrip("/"))
        elif cmd == "CWD" or cmd == "XCWD":
            target = self.dir_of(arg)
            c.listdir(target)                       # 打不开会抛 SwissError -> 550
            self.cwd = target
            self.reply(250, "Directory changed.")
        elif cmd == "CDUP":
            self.cwd = parent_remote(self.cwd)
            self.reply(250, "Directory changed.")
        elif cmd == "PASV":
            if self.pasv_sock:
                self.pasv_sock.close()
            self.pasv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.pasv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.pasv_sock.bind((self.server.server_address[0], 0))
            self.pasv_sock.listen(1)
            port = self.pasv_sock.getsockname()[1]
            host = self.server.server_address[0].replace(".", ",")
            self.reply(227, "Entering Passive Mode (%s,%d,%d)." %
                       (host, port >> 8, port & 0xFF))
        elif cmd == "EPSV":
            if self.pasv_sock:
                self.pasv_sock.close()
            self.pasv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.pasv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.pasv_sock.bind((self.server.server_address[0], 0))
            self.pasv_sock.listen(1)
            self.reply(229, "Entering Extended Passive Mode (|||%d|)" %
                       self.pasv_sock.getsockname()[1])
        elif cmd == "LIST" or cmd == "NLST" or cmd == "MLSD":
            target = self.dir_of(arg)
            entries = c.listdir(target)
            self.reply(150, "Opening data connection for %s" % cmd)
            if cmd == "NLST":
                payload = ("\r\n".join(e.name for e in entries) + "\r\n").encode("utf-8") \
                    if entries else b""
            elif cmd == "MLSD":
                payload = ("\r\n".join(
                    "type=%s;size=%d; %s" %
                    ("dir" if e.is_dir else "file", e.size, e.name)
                    for e in entries) + "\r\n").encode("utf-8") if entries else b""
            else:
                payload = _unix_listing(entries)
            self.send_data(payload)
        elif cmd == "SIZE":
            path = self.remote(arg)
            name = path.rstrip("/").rsplit("/", 1)[-1]
            parent = path.rstrip("/").rsplit("/", 1)[0]
            hit = next((e for e in c.listdir(parent + "/") if e.name == name), None)
            if hit is None or hit.is_dir:
                self.reply(550, "No such file.")
            else:
                self.reply(213, str(hit.size))
        elif cmd == "MDTM":
            self.reply(550, "Swiss does not expose timestamps.")
        elif cmd == "RETR":
            path = self.remote(arg)
            parent, _, name = path.rstrip("/").rpartition("/")
            hit = next((e for e in c.listdir(parent + "/")
                        if e.name == name and not e.is_dir), None)
            if hit is None:
                self.reply(550, "No such file: %s" % arg)
                return True
            fd, tmp = tempfile.mkstemp(prefix="swissftp_retr_")
            os.close(fd)
            try:
                c.download(path, tmp)
                self.reply(150, "Opening data connection for RETR")
                with open(tmp, "rb") as fh:
                    self.send_data(fh.read())
                log("gateway RETR %s" % path)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        elif cmd == "STOR" or cmd == "APPE":
            path = self.remote(arg)
            fd, tmp = tempfile.mkstemp(prefix="swissftp_stor_")
            os.close(fd)
            try:
                self.reply(150, "Opening data connection for STOR (buffering locally)")
                if not self.recv_data(tmp):
                    return True
                if os.path.getsize(tmp) == 0:
                    self.reply(550, "Refusing empty file (Swiss httpd cannot confirm it).")
                    return True
                c.upload(tmp, path)
                self.reply(226, "Transfer complete, written to %s" % path)
                log("gateway STOR %s (%d bytes)" % (path, os.path.getsize(tmp)))
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        elif cmd == "DELE":
            c.delete(self.remote(arg))
            self.reply(250, "Deleted.")
        elif cmd == "RNFR":
            self.rename_from = self.remote(arg)
            self.reply(350, "Ready for RNTO.")
        elif cmd == "RNTO":
            if not self.rename_from:
                self.reply(503, "RNFR first.")
            else:
                c.rename(self.rename_from, self.remote(arg))
                self.rename_from = None
                self.reply(250, "Renamed.")
        elif cmd in ("MKD", "XMKD", "RMD", "XRMD"):
            self.reply(550, "Swiss httpd has no mkdir/rmdir endpoint; create it on the console.")
        elif cmd == "REST":
            self.reply(550, "Resume is not supported (overwrite-only upload).")
        elif cmd == "QUIT":
            self.reply(221, "Bye.")
            return False
        else:
            self.reply(502, "%s not implemented." % cmd)
        return True


class SwissFTPGateway:
    """GUI 用的启停封装。"""

    def __init__(self, client: SwissClient, bind=DEFAULT_BIND, port=DEFAULT_PORT,
                 log=print):
        self.client = client
        self.bind = bind
        self.port = port
        self.log = log
        self.server = None
        self.thread = None

    @property
    def url(self) -> str:
        return "ftp://%s:%d" % (self.bind, self.port or DEFAULT_PORT)

    @property
    def running(self) -> bool:
        return self.server is not None

    def start(self):
        if self.server:
            return self.url
        self.server = GatewayServer((self.bind, self.port or 0), self.client, self.log)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True, name="SwissFTP-Gateway")
        self.thread.start()
        self.log("FTP 网关已启动：%s（用户名/密码任意）" % self.url)
        return self.url

    def stop(self):
        if not self.server:
            return
        srv, self.server = self.server, None
        try:
            srv.shutdown()
            srv.server_close()
        except OSError:
            pass
        self.log("FTP 网关已停止")


def selftest() -> int:
    """ftplib 连 mock Swiss + 网关，跑一遍标准 FTP 客户端流程。"""
    import hashlib
    import ftplib
    import mock_swiss
    from swiss_client import SwissClient

    msrv, mport = mock_swiss.start(0)
    client = SwissClient("127.0.0.1", mport)
    client.detect_volume()
    gw = SwissFTPGateway(client, port=0, log=lambda m: print("  " + m))
    url = gw.start()
    results = []

    def check(label, cond, extra=""):
        results.append(bool(cond))
        print("  %-30s %s %s" % (label, "PASS" if cond else "FAIL", extra))

    payload = bytes(range(256)) * 777
    try:
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", gw.port, timeout=8)
        ftp.encoding = "utf-8"      # ftplib 默认 latin-1，中文文件名必须改
        ftp.login("gc", "gc")
        check("login", ftp.getwelcome().startswith("220"))
        names = ftp.nlst()
        check("NLST", "games" in names and "IPL.dol" in names, str(names))
        ftp.cwd("games")
        check("CWD", ftp.pwd() == "/games", ftp.pwd())
        listing = []
        ftp.retrlines("LIST", listing.append)
        check("LIST", any("metroid_prime.iso" in x for x in listing), str(len(listing)))

        import io
        bio = io.BytesIO()
        ftp.retrbinary("RETR metroid_prime.iso", bio.write)
        check("RETR", len(bio.getvalue()) == 12345, str(len(bio.getvalue())))

        ftp.storbinary("STOR 中文 测试.iso", io.BytesIO(payload))
        check("STOR CJK", client.exists("sdb:/games", "中文 测试.iso").size == len(payload))

        bio2 = io.BytesIO()
        ftp.retrbinary("RETR 中文 测试.iso", bio2.write)
        check("RETR back sha256", hashlib.sha256(bio2.getvalue()).digest() ==
              hashlib.sha256(payload).digest())
        check("SIZE", int(ftp.size("中文 测试.iso")) == len(payload))
        ftp.rename("中文 测试.iso", "renamed.iso")
        check("RNFR/RNTO", client.exists("sdb:/games", "renamed.iso") is not None)
        ftp.delete("renamed.iso")
        check("DELE", client.exists("sdb:/games", "renamed.iso") is None)
        ftp.quit()
    except Exception as exc:
        check("exception", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        gw.stop()
        msrv.shutdown()
        msrv.server_close()
    print("\n%d/%d PASS" % (sum(results), len(results)))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
