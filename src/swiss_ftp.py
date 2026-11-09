#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SwissFTP —— PC 端连 GameCube / Swiss 的文件传输工具（把文件送进 SD 卡）。

    python tools/swiss_ftp.py                        图形界面
    python tools/swiss_ftp.py --mock                连本地模拟服务（演示/自检界面）
    python tools/swiss_ftp.py --selftest            协议 + GUI 全流程自检（无实机）
    python tools/swiss_ftp.py --cli --host 192.168.1.2 --ls sdb:/games/
    python tools/swiss_ftp.py --cli --host 192.168.1.2 --put D:/a.iso sdb:/games/a.iso
    python tools/swiss_ftp.py --cli --host 192.168.1.2 --get sdb:/swiss/settings/global.ini out.ini

协议说明：Swiss 固件本身是 FTP **客户端**（用来浏览 PC 上的 FTP 服务器），
并没有对外开 FTP 服务。PC 想写 SD 卡，走的是固件 httpd(80) 的 FatFs 端点，
所以本工具是一个 FTP 风格的双栏客户端，传输后端为 Swiss httpd。
细节见 README「SwissFTP」一节。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from swiss_client import (
    DEFAULT_HOST, DEFAULT_PORT, SwissClient, SwissCancelled, SwissError,
    Entry, VOLUMES, human_size, join_remote, parent_remote, split_remote,
)
from swiss_ftp_gateway import SwissFTPGateway

APP_NAME = "SwissFTP"
APP_VER = "1.0"
CONFIG_PATH = Path.home() / ".swissftp.json"
FROZEN = bool(getattr(sys, "frozen", False))


# ------------------------------------------------------------------- frozen stdio
# PyInstaller 的 --windowed 产物是 GUI 子系统，启动时没有控制台，
# sys.stdout / sys.stderr 是 None，任何 print 都会抛 AttributeError。
class _NullWriter:
    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


class _TeeWriter:
    """把输出同时送到多个流；任何一个写失败都不影响主流程。"""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, s):
        for f in self.streams:
            try:
                f.write(s)
                f.flush()
            except Exception:
                pass
        return len(s)

    def flush(self):
        for f in self.streams:
            try:
                f.flush()
            except Exception:
                pass

    def isatty(self):
        return False


_TEXT_FLAGS = {
    "--cli", "--selftest", "--protocol-selftest", "--gateway", "--ls",
    "--put", "--get", "--rm", "--rename", "--state", "--mock",
    "--help", "-h", "--version",
}


def _wants_text(argv) -> bool:
    """带命令行开关 = 需要有输出；纯双击 / 只改主题 = 纯 GUI，绝不弹黑窗。"""
    return any(a.split("=", 1)[0] in _TEXT_FLAGS for a in argv[1:])


def _setup_io(needs_text: bool, log_path: str | None = None):
    logf = None
    if log_path:
        try:
            logf = open(log_path, "w", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            logf = None
    if FROZEN:
        con = None
        if needs_text and os.name == "nt":
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                # 从 cmd / PowerShell 启动：复用父控制台；
                # 从快捷方式等无控制台环境启动：新建一个。
                if not k32.AttachConsole(-1):      # ATTACH_PARENT_PROCESS
                    k32.AllocConsole()
                con = open("CONOUT$", "w", encoding="utf-8",
                           errors="replace", buffering=1)
            except Exception:
                con = None
        writer = _TeeWriter(con, logf) if (con or logf) else _NullWriter()
        sys.stdout = writer
        sys.stderr = writer
    elif logf:
        sys.stdout = _TeeWriter(sys.stdout, logf)
        sys.stderr = _TeeWriter(sys.stderr, logf)
    return logf

THEMES = {
    "light": dict(
        bg="#f4f6f8", panel="#ffffff", border="#dfe4ea", text="#222831",
        muted="#6b7280", accent="#2f6feb", accent_fg="#ffffff",
        sel="#dbe8ff", ok="#15803d", warn="#b45309", err="#dc2626",
        stripe="#fafbfc", header="#eef1f5", field="#ffffff",
    ),
    "dark": dict(
        bg="#1c1f24", panel="#252a31", border="#343b45", text="#e6e9ee",
        muted="#98a2b3", accent="#7aa5e9", accent_fg="#151a20",
        sel="#33465f", ok="#5fbf83", warn="#d9a441", err="#e57474",
        stripe="#22272e", header="#2b313a", field="#1e232a",
    ),
}

COLS = ("name", "size", "kind", "mtime")
COL_LABELS = {"name": "名称", "size": "大小", "kind": "类型", "mtime": "修改时间"}


# --------------------------------------------------------------------------- utils
def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass


def fmt_eta(seconds) -> str:
    if not seconds or seconds < 0 or seconds > 86400:
        return "--:--"
    seconds = int(seconds)
    return "%02d:%02d" % (seconds // 60, seconds % 60)


def fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


@dataclass
class Item:
    name: str
    size: int
    is_dir: bool
    mtime: str = ""
    path: str = ""

    @property
    def kind(self) -> str:
        return "文件夹" if self.is_dir else (os.path.splitext(self.name)[1].lstrip(".").upper() or "文件")


@dataclass
class Task:
    id: int
    direction: str                  # "put" | "get"
    local: str
    remote: str
    size: int
    state: str = "等待中"
    done: int = 0
    rate: float = 0.0
    message: str = ""


# --------------------------------------------------------------------------- panes
class FilePane(ttk.Frame):
    """一边文件列表。只负责显示与选择，业务逻辑由 App 注入回调。"""

    def __init__(self, master, title: str, app, *, local: bool):
        super().__init__(master, style="Card.TFrame", padding=8)
        self.app = app
        self.local = local
        self.items: list[Item] = []
        self.cwd = ""
        self.path_var = tk.StringVar()
        self.stat_var = tk.StringVar(value="未连接" if not local else "0 项")
        self.on_open = None
        self.on_menu = None

        head = ttk.Frame(self, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 6))
        self.title_label = ttk.Label(head, text=title, style="Title.TLabel")
        self.title_label.pack(side="left")

        bar = ttk.Frame(self, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ent = ttk.Entry(bar, textvariable=self.path_var)
        ent.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ent.bind("<Return>", lambda _e: app.on_path_entered(self))
        ttk.Button(bar, text="↑", width=3, command=lambda: app.on_up(self)).pack(side="left", padx=2)
        ttk.Button(bar, text="⟳", width=3, command=lambda: app.on_refresh(self)).pack(side="left", padx=2)

        body = ttk.Frame(self, style="Card.TFrame")
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=COLS, show="headings",
                                 selectmode="extended", style="File.Treeview")
        # 列宽总和刻意压在 400 以内：ttk.Treeview 不会自动收窄，
        # 太宽会把整个右栏挤出窗口（125% DPI 下尤其明显）。
        for c, width in zip(COLS, (175, 68, 58, 98)):
            self.tree.heading(c, text=COL_LABELS[c])
            self.tree.column(c, width=width, minwidth=44, anchor="w",
                             stretch=(c == "name"))
        vs = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._double)
        self.tree.bind("<Button-3>", self._right)
        self.tree.bind("<<TreeviewSelect>>", self._sel)

        ttk.Label(self, textvariable=self.stat_var, style="Muted.TLabel").pack(
            anchor="w", pady=(6, 0))

    # -- plumbing
    def set_cwd(self, cwd: str):
        self.cwd = cwd
        self.path_var.set(cwd)

    def set_items(self, items: list[Item]):
        self.items = items
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(items):
            self.tree.insert("", "end", iid=str(i), values=(
                it.name,
                "" if it.is_dir else human_size(it.size),
                it.kind,
                it.mtime,
            ), tags=("dir",) if it.is_dir else ())
        self.tree.tag_configure("dir", foreground=self.app.theme["accent"])
        self._sel()

    def selected(self) -> list[Item]:
        out = []
        for iid in self.tree.selection():
            try:
                out.append(self.items[int(iid)])
            except (ValueError, IndexError):
                pass
        return out

    def _sel(self, _e=None):
        n = len(self.selected())
        self.stat_var.set("%d 项%s" % (len(self.items),
                                     (" · 已选 %d" % n) if n else ""))

    def _double(self, _e=None):
        sel = self.selected()
        if len(sel) == 1 and sel[0].is_dir and self.on_open:
            self.on_open(sel[0])

    def _right(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.on_menu:
            self.on_menu(event, self)


# --------------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self, cfg: dict, mock_port: int = 0):
        super().__init__()
        self.cfg = cfg
        self.theme_name = cfg.get("theme", "light")
        if self.theme_name not in THEMES:
            self.theme_name = "light"
        self.theme = THEMES[self.theme_name]
        self.client = None
        self.connected = False
        self.gateway = None
        self.overwrite_all = False
        self._mock_port = mock_port
        self.cancel_event = threading.Event()
        self.tasks: list[Task] = []
        self.task_q: queue.Queue = queue.Queue()
        self.task_seq = 0
        self.current: Task | None = None
        self._last_ui = 0.0
        self._closing = False

        self.title("%s %s · Swiss SD 卡文件传输" % (APP_NAME, APP_VER))
        # 按屏幕自适应，避免高 DPI 下固定尺寸被系统裁掉右栏和底栏；
        # 用户上次调整过的尺寸优先，但仍钳制在屏幕可见范围内。
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1240, sw - 120), min(800, sh - 120)
        m = re.match(r"^(\d+)x(\d+)", cfg.get("geometry", ""))
        if m:
            w = min(max(860, int(m.group(1))), sw - 40)
            h = min(max(560, int(m.group(2))), sh - 40)
        self.geometry("%dx%d" % (w, h))
        self.minsize(860, 560)
        self._dpi()
        self._style()
        self._build()

        host = "127.0.0.1" if mock_port else cfg.get("host", DEFAULT_HOST)
        port = mock_port or cfg.get("port", DEFAULT_PORT)
        self.host_var.set(host)
        self.port_var.set(str(port))
        self.local_pane.set_cwd(cfg.get("local_dir") or str(Path.home()))
        self.remote_pane.set_cwd(cfg.get("remote_dir") or cfg.get("volume") or VOLUMES[0])
        self.volume_var.set(cfg.get("volume") or VOLUMES[0])
        self.refresh_local()

        self.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self._worker, daemon=True).start()
        if mock_port:
            self.after(200, self.connect)

    # ------------------------------------------------------------------ look
    @staticmethod
    def _dpi():
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    def _style(self):
        t = self.theme
        self.configure(bg=t["bg"])
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=t["bg"], foreground=t["text"],
                    fieldbackground=t["field"], bordercolor=t["border"],
                    font=("Microsoft YaHei UI", 9))
        s.configure("Card.TFrame", background=t["panel"], relief="flat",
                    borderwidth=1)
        s.configure("TFrame", background=t["bg"])
        s.configure("TLabel", background=t["panel"], foreground=t["text"])
        s.configure("Top.TLabel", background=t["bg"], foreground=t["text"])
        s.configure("Muted.TLabel", background=t["panel"], foreground=t["muted"])
        s.configure("Title.TLabel", background=t["panel"], foreground=t["text"],
                    font=("Microsoft YaHei UI", 11, "bold"))
        s.configure("Status.TLabel", background=t["bg"], foreground=t["muted"])
        s.configure("TButton", background=t["panel"], foreground=t["text"],
                    bordercolor=t["border"], padding=(8, 5))
        s.map("TButton", background=[("active", t["sel"])],
              foreground=[("disabled", t["muted"])])
        s.configure("Accent.TButton", background=t["accent"],
                    foreground=t["accent_fg"], bordercolor=t["accent"],
                    padding=(10, 6), font=("Microsoft YaHei UI", 9, "bold"))
        s.map("Accent.TButton", background=[("active", t["sel"])])
        s.configure("TEntry", fieldbackground=t["field"], foreground=t["text"],
                    bordercolor=t["border"], padding=4)
        s.configure("TCombobox", fieldbackground=t["field"],
                    foreground=t["text"], background=t["panel"], padding=3)
        # clam 的 readonly Combobox 会另取系统亮色，必须显式 map 回来
        s.map("TCombobox",
              fieldbackground=[("readonly", t["field"]), ("disabled", t["bg"])],
              foreground=[("readonly", t["text"]), ("disabled", t["muted"])],
              background=[("readonly", t["panel"])])
        s.configure("TScrollbar", background=t["header"],
                    troughcolor=t["panel"], bordercolor=t["border"])
        s.map("TScrollbar", background=[("active", t["sel"])])
        for style_name in ("TCombobox", "TScrollbar"):
            try:        # -arrowcolor 只在较新的 Tk 上支持
                s.configure(style_name, arrowcolor=t["muted"])
            except tk.TclError:
                pass
        s.configure("File.Treeview", background=t["panel"],
                    fieldbackground=t["panel"], foreground=t["text"],
                    bordercolor=t["border"], rowheight=24)
        s.configure("File.Treeview.Heading", background=t["header"],
                    foreground=t["muted"], relief="flat", padding=4)
        s.map("File.Treeview", background=[("selected", t["sel"])],
              foreground=[("selected", t["text"])])
        s.configure("TProgressbar", background=t["accent"],
                    troughcolor=t["header"], bordercolor=t["border"])
        s.configure("TNotebook", background=t["bg"], bordercolor=t["border"])
        s.configure("TNotebook.Tab", background=t["header"],
                    foreground=t["muted"], padding=(10, 4))
        s.map("TNotebook.Tab", background=[("selected", t["panel"])],
              foreground=[("selected", t["text"])])
        s.configure("TCheckbutton", background=t["panel"], foreground=t["text"])
        self.log_text_bg = t["panel"]

    def _build(self):
        t = self.theme
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- 顶部连接栏
        top = ttk.Frame(self, padding=(12, 10))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="SwissFTP", style="Top.TLabel",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        ttk.Label(top, text="SD 卡直连 · 无需拔卡", style="Status.TLabel").pack(
            side="left", padx=(8, 18))

        ttk.Label(top, text="主机", style="Top.TLabel").pack(side="left")
        self.host_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.host_var, width=17).pack(side="left", padx=(4, 8))
        ttk.Label(top, text="端口", style="Top.TLabel").pack(side="left")
        self.port_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.port_var, width=6).pack(side="left", padx=(4, 8))
        ttk.Label(top, text="卷", style="Top.TLabel").pack(side="left")
        self.volume_var = tk.StringVar()
        cb = ttk.Combobox(top, textvariable=self.volume_var, values=list(VOLUMES),
                          width=8, state="readonly")
        cb.pack(side="left", padx=(4, 8))
        cb.bind("<<ComboboxSelected>>", self._volume_changed)

        self.connect_btn = ttk.Button(top, text="连接", style="Accent.TButton",
                                      command=self.connect)
        self.connect_btn.pack(side="left", padx=(4, 6))
        self.state_var = tk.StringVar(value="● 未连接")
        self.state_label = ttk.Label(top, textvariable=self.state_var,
                                     style="Status.TLabel")
        self.state_label.pack(side="left", padx=6)
        ttk.Button(top, text="主题", command=self.toggle_theme).pack(side="right", padx=3)
        ttk.Button(top, text="说明", command=self.show_help).pack(side="right", padx=3)
        self.gateway_btn = ttk.Button(top, text="FTP 网关", command=self.toggle_gateway)
        self.gateway_btn.pack(side="right", padx=3)

        # ---- 主体三列
        main = ttk.Frame(self, padding=(12, 2))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(2, weight=1)
        main.rowconfigure(0, weight=1)

        self.local_pane = FilePane(main, "本地磁盘", self, local=True)
        self.local_pane.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.remote_pane = FilePane(main, "Swiss SD 卡", self, local=False)
        self.remote_pane.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        mid = ttk.Frame(main, padding=(4, 24))
        mid.grid(row=0, column=1, sticky="ns")
        self._mid_buttons(mid)

        self.local_pane.on_open = self.open_local_dir
        self.remote_pane.on_open = self.open_remote_dir
        self.local_pane.on_menu = self.show_menu
        self.remote_pane.on_menu = self.show_menu

        # ---- 底部传输区
        bottom = ttk.Frame(self, padding=(12, 6))
        bottom.grid(row=2, column=0, sticky="ew")
        pr = ttk.Frame(bottom)
        pr.pack(fill="x")
        self.progress = ttk.Progressbar(pr, orient="horizontal", mode="determinate",
                                        maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.transfer_var = tk.StringVar(value="队列空闲")
        ttk.Label(pr, textvariable=self.transfer_var, style="Status.TLabel",
                  width=52).pack(side="right")

        nb = ttk.Notebook(bottom)
        nb.pack(fill="both", expand=False, pady=(6, 0))
        qf = ttk.Frame(nb, padding=2)
        self.qtree = ttk.Treeview(qf, columns=("dir", "name", "size", "pct", "state"),
                                  show="headings", height=4, style="File.Treeview")
        for c, label, width in (("dir", "方向", 60), ("name", "文件", 380),
                                ("size", "大小", 90), ("pct", "进度", 90),
                                ("state", "状态", 220)):
            self.qtree.heading(c, text=label)
            self.qtree.column(c, width=width, anchor="w", stretch=(c == "name"))
        qs = ttk.Scrollbar(qf, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qs.set)
        self.qtree.pack(side="left", fill="both", expand=True)
        qs.pack(side="right", fill="y")
        nb.add(qf, text="传输队列")

        lf = ttk.Frame(nb, padding=2)
        self.log = tk.Text(lf, height=4, wrap="none", relief="flat",
                           bg=self.log_text_bg, fg=t["text"],
                           font=("Consolas", 9), state="disabled")
        ls = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        self.log.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")
        nb.add(lf, text="日志")

        # ---- 状态栏
        self.status_var = tk.StringVar(value="就绪 · 先让 Swiss 停在主界面，再点「连接」")
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel",
                  padding=(12, 4)).grid(row=3, column=0, sticky="ew")

    def _mid_buttons(self, parent):
        def btn(text, cmd, accent=False, enabled=True):
            b = ttk.Button(parent, text=text, command=cmd,
                           style="Accent.TButton" if accent else "TButton",
                           state="normal" if enabled else "disabled", width=13)
            b.pack(fill="x", pady=3)
            return b

        btn("上传选中 →", self.upload_selected, accent=True)
        btn("← 下载选中", self.download_selected)
        btn("添加文件…", self.add_files)
        btn("添加文件夹…", self.add_folder)
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=8)
        btn("刷新两端", lambda: (self.refresh_local(), self.refresh_remote()))
        btn("删除远端", self.delete_remote)
        btn("远端改名", self.rename_remote)
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=8)
        btn("取消当前", self.cancel_current)
        btn("清空队列", self.clear_queue)

    # ------------------------------------------------------------------ log / state
    def log_line(self, msg: str, kind="info"):
        color = {"info": self.theme["text"], "ok": self.theme["ok"],
                 "warn": self.theme["warn"], "err": self.theme["err"]}.get(kind)
        stamp = datetime.now().strftime("%H:%M:%S")

        def put():
            if self._closing:
                return
            self.log.configure(state="normal")
            self.log.insert("end", "[%s] %s\n" % (stamp, msg))
            self.log.tag_add(kind, "end-2l", "end-1l")
            self.log.tag_config(kind, foreground=color)
            self.log.see("end")
            self.log.configure(state="disabled")

        self.after(0, put)

    def set_status(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def _on_error(self, exc, context=""):
        msg = "%s：%s" % (context, exc) if context else str(exc)
        self.log_line(msg, "err")
        self.set_status(msg)
        if not self._closing:
            self.after(0, lambda: messagebox.showerror(APP_NAME, msg, parent=self))

    def _set_connected(self, ok, text):
        self.connected = ok
        color = self.theme["ok"] if ok else self.theme["err"]
        self.state_var.set(("● " if ok else "● ") + text)
        self.state_label.configure(foreground=color)
        self.connect_btn.configure(text="重新连接" if ok else "连接")

    def toggle_theme(self):
        """切换主题需要重建样式；重启窗口最稳，配置会保留。"""
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.cfg["theme"] = self.theme_name
        self.persist()
        self.destroy()
        new = App(self.cfg, mock_port=getattr(self, "_mock_port", 0))
        new.mainloop()

    def persist(self):
        # --mock 演示连的是 127.0.0.1:18080，别把真机 IP 覆盖掉
        if not self._mock_port:
            self.cfg["host"] = self.host_var.get()
            self.cfg["port"] = self._port()
        self.cfg.update(
            volume=self.volume_var.get(), local_dir=self.local_pane.cwd,
            remote_dir=self.remote_pane.cwd, theme=self.theme_name,
            geometry=self.geometry(),
        )
        save_config(self.cfg)

    def close(self):
        self.persist()
        if self.gateway:
            self.gateway.stop()
            self.gateway = None
        self._closing = True
        self.cancel_event.set()
        self.destroy()

    def toggle_gateway(self):
        """把 SD 卡映射成 127.0.0.1 上的标准 FTP 站点，供 FileZilla 等使用。"""
        if self.gateway and self.gateway.running:
            self.gateway.stop()
            self.gateway = None
            self.gateway_btn.configure(text="FTP 网关")
            self.set_status("FTP 网关已停止")
            return
        if not self._need_client():
            return
        try:
            gw = SwissFTPGateway(self.client,
                                 port=int(self.cfg.get("gateway_port", 2121)),
                                 log=self.log_line)
            url = gw.start()
        except (OSError, ValueError) as exc:
            self._on_error(SwissError("FTP 网关启动失败：%s" % exc))
            return
        self.gateway = gw
        self.cfg["gateway_port"] = gw.port
        self.gateway_btn.configure(text="停止网关")
        self.log_line("FTP 网关 %s · 用户名/密码任意 · 目标目录必须已存在" % url, "ok")
        self.set_status("FTP 网关 %s · 可用 FileZilla / 资源管理器 / curl 连接" % url)

    def _port(self) -> int:
        try:
            return int(self.port_var.get().strip())
        except ValueError:
            return DEFAULT_PORT

    def _volume_changed(self, _e=None):
        vol = self.volume_var.get()
        self.remote_pane.set_cwd(vol)
        if self.connected:
            self.refresh_remote()

    # ------------------------------------------------------------------ background
    def _bg(self, fn, on_ok=None, on_err=None, context=""):
        def run():
            try:
                result = fn()
            except Exception as exc:
                self.after(0, lambda: (on_err or self._on_error)(exc, context))
            else:
                if on_ok:
                    self.after(0, lambda: on_ok(result))
        threading.Thread(target=run, daemon=True).start()

    def connect(self):
        host = self.host_var.get().strip()
        port = self._port()
        if not host:
            self._on_error(SwissError("请填写 Swiss 的 IP 地址"))
            return
        self.client = SwissClient(host, port)
        if self.gateway and self.gateway.server:
            # 网关会话持有的是旧实例，重连后换成新的（锁也随新实例走）
            self.gateway.client = self.client
            self.gateway.server.client = self.client
        self.set_status("正在连接 %s:%d …" % (host, port))
        self.state_var.set("● 连接中")
        self._bg(lambda: (self.client.ping(), self.client.detect_volume()),
                 on_ok=self._connected, context="连接 Swiss")

    def _connected(self, result):
        alive, vol = result
        if not alive:
            self._on_error(SwissError("端口通了但不是 Swiss httpd（主机可能正在跑游戏，"
                                      "请退回 Swiss 主界面再试）"), "连接 Swiss")
            self._set_connected(False, "未连接")
            return
        self.volume_var.set(vol)
        self._set_connected(True, "已连接 %s" % self.host_var.get())
        self.log_line("已连接 %s:%d，卷 %s" % (self.host_var.get(), self._port(), vol), "ok")
        self.set_status("已连接。左栏选文件 →「上传选中」写入 SD 卡")
        self.persist()
        self.refresh_remote()

    # ------------------------------------------------------------------ listings
    def list_local(self, path):
        out = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat()
                    out.append(Item(e.name, 0 if e.is_dir() else st.st_size,
                                    e.is_dir(), fmt_time(st.st_mtime), e.path))
                except OSError:
                    continue
        out.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return out

    def refresh_local(self):
        cwd = self.local_pane.cwd or str(Path.home())
        if not os.path.isdir(cwd):
            cwd = str(Path.home())
        self._bg(lambda: self.list_local(cwd),
                 on_ok=lambda items: (self.local_pane.set_cwd(cwd),
                                      self.local_pane.set_items(items)),
                 on_err=lambda exc: self.log_line("本地目录：%s" % exc, "err"))

    def refresh_remote(self):
        if not self.client:
            return
        cwd = self.remote_pane.cwd
        if not cwd.endswith("/"):
            cwd += "/"
        self._bg(lambda: self.client.listdir(cwd),
                 on_ok=lambda entries: self._fill_remote(cwd, entries),
                 on_err=lambda exc: self._on_error(exc, "列目录 %s" % cwd),
                 context="列目录 %s" % cwd)

    def _fill_remote(self, cwd, entries):
        items = [Item(e.name, e.size, e.is_dir, "", join_remote(cwd, e.name))
                 for e in entries]
        self.remote_pane.set_cwd(cwd)
        self.remote_pane.set_items(items)
        self.remote_pane.title_label.configure(
            text="Swiss SD 卡 · %s" % self.volume_var.get())
        self.set_status("远端 %s · %d 项" % (cwd, len(items)))

    def on_path_entered(self, pane):
        path = pane.path_var.get().strip()
        if pane is self.local_pane:
            if os.path.isdir(path):
                pane.set_cwd(path)
                self.refresh_local()
            else:
                self._on_error(SwissError("本地目录不存在：%s" % path))
        else:
            pane.set_cwd(path if path.endswith("/") else path + "/")
            self.refresh_remote()

    def on_up(self, pane):
        if pane is self.local_pane:
            pane.set_cwd(os.path.dirname(pane.cwd.rstrip("\\/")) or pane.cwd)
            self.refresh_local()
        else:
            pane.set_cwd(parent_remote(pane.cwd))
            self.refresh_remote()

    def on_refresh(self, pane):
        self.refresh_local() if pane is self.local_pane else self.refresh_remote()

    def open_local_dir(self, item):
        self.local_pane.set_cwd(os.path.abspath(item.path))
        self.refresh_local()

    def open_remote_dir(self, item):
        self.remote_pane.set_cwd(item.path.rstrip("/") + "/")
        self.refresh_remote()

    # ------------------------------------------------------------------ context menu
    def show_menu(self, event, pane):
        m = tk.Menu(self, tearoff=0, bg=self.theme["panel"],
                    fg=self.theme["text"], activebackground=self.theme["sel"])
        sel = pane.selected()
        if pane is self.local_pane:
            m.add_command(label="上传选中到 SD 卡", command=self.upload_selected)
            m.add_command(label="添加文件夹…", command=self.add_folder)
        else:
            m.add_command(label="下载选中到本地", command=self.download_selected)
            m.add_command(label="改名", command=self.rename_remote)
            m.add_command(label="删除", command=self.delete_remote)
            m.add_command(label="复制远端路径",
                          command=lambda: self._copy("\n".join(i.path for i in sel)))
        m.add_separator()
        m.add_command(label="刷新", command=lambda: self.on_refresh(pane))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.set_status("已复制路径到剪贴板")

    # ------------------------------------------------------------------ transfers
    def _need_client(self) -> bool:
        if not self.connected or not self.client:
            self._on_error(SwissError("请先连接 Swiss（主机要停在 Swiss 主界面，"
                                      "当前设备为 SD 卡）"))
            return False
        return True

    @staticmethod
    def _expand(paths):
        """把目录递归展开成 (绝对路径, 相对名) 列表。"""
        out = []
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isfile(p):
                out.append((p, os.path.basename(p)))
            elif os.path.isdir(p):
                base = os.path.dirname(p)
                for root, _dirs, files in os.walk(p):
                    for f in files:
                        full = os.path.join(root, f)
                        out.append((full, os.path.relpath(full, base).replace("\\", "/")))
        return out

    def _confirm_overwrite(self, name) -> str:
        """返回 'yes' / 'no' / 'all' / 'cancel'。"""
        if self.overwrite_all:
            return "all"
        result = {"v": "cancel"}

        dlg = tk.Toplevel(self)
        dlg.title("目标已存在")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.configure(bg=self.theme["panel"])
        frm = ttk.Frame(dlg, padding=16, style="Card.TFrame")
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="SD 卡上已存在同名文件：", style="TLabel").pack(anchor="w")
        ttk.Label(frm, text=name, style="Title.TLabel", wraplength=420).pack(
            anchor="w", pady=(4, 12))
        ttk.Label(frm, text="Swiss 的写入端点是覆盖式（FA_CREATE_ALWAYS），"
                            "不支持断点续传。", style="Muted.TLabel",
                  wraplength=420).pack(anchor="w", pady=(0, 12))
        bar = ttk.Frame(frm, style="Card.TFrame")
        bar.pack(fill="x")
        for label, value in (("覆盖", "yes"), ("跳过", "no"),
                             ("全部覆盖", "all"), ("取消", "cancel")):
            ttk.Button(bar, text=label, width=9,
                       command=lambda v=value: (result.update(v=v), dlg.destroy())
                       ).pack(side="left", padx=3)
        dlg.grab_set()
        self.wait_window(dlg)
        return result["v"]

    def upload_selected(self):
        if not self._need_client():
            return
        items = self.local_pane.selected()
        if not items:
            self.set_status("左栏先选中要上传的文件/文件夹")
            return
        self._queue_put([i.path for i in items])

    def add_files(self):
        if not self._need_client():
            return
        paths = filedialog.askopenfilenames(parent=self, title="选择要写入 SD 卡的文件")
        if paths:
            self._queue_put(list(paths))

    def add_folder(self):
        if not self._need_client():
            return
        path = filedialog.askdirectory(parent=self, title="选择要写入 SD 卡的文件夹")
        if path:
            self._queue_put([path])

    def _queue_put(self, paths):
        files = self._expand(paths)
        if not files:
            self.set_status("所选内容里没有文件")
            return
        cwd = self.remote_pane.cwd
        existing = {i.name for i in self.remote_pane.items}
        added = 0
        for local, rel in files:
            if "/" in rel:
                # 子目录需 SD 卡上已经存在（固件没有 mkdir 端点）
                target = join_remote(cwd, rel)
            else:
                target = join_remote(cwd, rel)
            if rel in existing or os.path.basename(rel) in existing:
                choice = self._confirm_overwrite(os.path.basename(rel))
                if choice == "cancel":
                    break
                if choice == "no":
                    continue
                if choice == "all":
                    self.overwrite_all = True
            self._add_task("put", local, target, os.path.getsize(local))
            added += 1
        if added:
            self.log_line("已加入 %d 个上传任务 → %s" % (added, cwd))
            self.set_status("队列 %d 项，开始传输" % self.task_q.qsize())

    def download_selected(self):
        if not self._need_client():
            return
        items = [i for i in self.remote_pane.selected() if not i.is_dir]
        if not items:
            self.set_status("右栏先选中要下载的文件（暂不支持整目录下载）")
            return
        local_dir = self.local_pane.cwd
        existing = set(os.listdir(local_dir)) if os.path.isdir(local_dir) else set()
        for it in items:
            if it.name in existing:
                choice = self._confirm_overwrite(it.name)
                if choice == "cancel":
                    break
                if choice == "no":
                    continue
                if choice == "all":
                    self.overwrite_all = True
            self._add_task("get", os.path.join(local_dir, it.name), it.path, it.size)
        self.set_status("队列 %d 项" % self.task_q.qsize())

    def _add_task(self, direction, local, remote, size):
        self.task_seq += 1
        task = Task(self.task_seq, direction, local, remote, size)
        self.tasks.append(task)

        def refresh_row():
            self.qtree.insert("", "end", iid=str(task.id), values=(
                "上传" if direction == "put" else "下载",
                os.path.basename(local if direction == "put" else remote),
                human_size(size), "0%", "等待中"))

        self.after(0, refresh_row)
        self.task_q.put(task)

    def _worker(self):
        while True:
            task = self.task_q.get()
            self.current = task
            self.cancel_event.clear()
            self._task_ui(task, "传输中", 0)
            started = time.time()
            last = [0.0]

            def progress(done, total, rate):
                if self.cancel_event.is_set():
                    raise SwissCancelled("已取消")
                now = time.time()
                if now - last[0] > 0.1 or done >= total:
                    last[0] = now
                    task.done, task.rate = done, rate
                    self._task_ui(task, "传输中", done / max(1, total))

            try:
                if task.direction == "put":
                    self.client.upload(task.local, task.remote,
                                       progress=progress, cancel=self.cancel_event)
                else:
                    self.client.download(task.remote, task.local,
                                         progress=progress, cancel=self.cancel_event)
                task.state = "完成"
                self.log_line("完成 %s %s（%s，%.0f KB/s）" % (
                    "上传" if task.direction == "put" else "下载",
                    os.path.basename(task.local), human_size(task.size),
                    task.size / max(0.001, time.time() - started) / 1024), "ok")
                self._task_ui(task, "完成", 1.0)
            except SwissCancelled:
                task.state = "已取消"
                self.log_line("已取消 %s" % os.path.basename(task.local), "warn")
                self._task_ui(task, "已取消", 0)
            except Exception as exc:
                task.state = "失败"
                task.message = str(exc)
                self.log_line("失败 %s：%s" % (os.path.basename(task.local), exc), "err")
                self._task_ui(task, "失败：%s" % exc, 0)
            finally:
                self.current = None
                self.task_q.task_done()
            # 完成后刷新两端列表（让新文件立刻可见）
            self.after(0, self._after_transfer)

    def _after_transfer(self):
        self.progress["value"] = 0
        self.transfer_var.set("队列空闲" if self.task_q.empty() else "队列 %d 项" % self.task_q.qsize())
        if self.connected:
            self.refresh_remote()
        self.refresh_local()

    def _task_ui(self, task: Task, state: str, ratio: float):
        def put():
            if self._closing:
                return
            task.state = state
            pct = max(0, min(100, int(ratio * 100)))
            if self.qtree.exists(str(task.id)):
                self.qtree.item(str(task.id), values=(
                    "上传" if task.direction == "put" else "下载",
                    os.path.basename(task.local if task.direction == "put" else task.remote),
                    human_size(task.size), "%d%%" % pct, state))
            self.progress["value"] = ratio * 1000
            eta = ((task.size - task.done) / task.rate) if task.rate > 1024 else 0
            self.transfer_var.set("%s  %d%%  ·  %.0f KB/s  ·  剩余 %s" % (
                os.path.basename(task.local), pct, task.rate / 1024, fmt_eta(eta)))
        try:
            self.after(0, put)
        except RuntimeError:
            pass

    def cancel_current(self):
        if self.current:
            self.cancel_event.set()
            self.log_line("正在取消当前传输…", "warn")
        else:
            self.set_status("当前没有在传的文件")

    def clear_queue(self):
        n = 0
        while not self.task_q.empty():
            try:
                t = self.task_q.get_nowait()
                self.task_q.task_done()
                t.state = "已移除"
                self._task_ui(t, "已移除", 0)
                n += 1
            except queue.Empty:
                break
        self.cancel_current()
        self.log_line("清空队列：移除 %d 项" % n)

    # ------------------------------------------------------------------ file ops
    def delete_remote(self):
        if not self._need_client():
            return
        items = self.remote_pane.selected()
        if not items:
            self.set_status("右栏先选中要删除的条目")
            return
        names = "、".join(i.name for i in items[:5])
        if not messagebox.askyesno(APP_NAME,
                                   "确定从 SD 卡删除以下 %d 项？\n%s%s\n\n此操作不可撤销。" %
                                   (len(items), names,
                                    "…" if len(items) > 5 else ""), parent=self):
            return

        def work():
            done, errors = 0, []
            for it in items:
                try:
                    self.client.delete(it.path)
                    done += 1
                except SwissError as exc:
                    errors.append("%s: %s" % (it.name, exc))
            return done, errors

        self._bg(work, on_ok=lambda r: (
            self.log_line("删除 %d 项%s" % (r[0], ("，失败：" + "; ".join(r[1])) if r[1] else ""),
                          "ok" if not r[1] else "warn"),
            self.refresh_remote()), context="删除")

    def rename_remote(self):
        if not self._need_client():
            return
        items = self.remote_pane.selected()
        if len(items) != 1:
            self.set_status("请只选中一个条目")
            return
        item = items[0]
        dlg = tk.Toplevel(self)
        dlg.title("改名 / 移动")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.configure(bg=self.theme["panel"])
        frm = ttk.Frame(dlg, padding=16, style="Card.TFrame")
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="新名称（也可写相对/完整路径来移动）", style="TLabel").pack(anchor="w")
        var = tk.StringVar(value=item.name)
        ent = ttk.Entry(frm, textvariable=var, width=52)
        ent.pack(fill="x", pady=(6, 12))
        ent.select_range(0, "end")
        ent.focus_set()
        result = {"v": None}
        bar = ttk.Frame(frm, style="Card.TFrame")
        bar.pack(fill="x")

        def accept():
            result["v"] = var.get().strip()
            dlg.destroy()

        ttk.Button(bar, text="确定", command=accept, width=9).pack(side="left", padx=3)
        ttk.Button(bar, text="取消", command=dlg.destroy, width=9).pack(side="left", padx=3)
        ent.bind("<Return>", lambda _e: accept())
        dlg.grab_set()
        self.wait_window(dlg)
        new = result["v"]
        if not new or new == item.name:
            return
        dst = new if ":/" in new else join_remote(self.remote_pane.cwd, new)
        self._bg(lambda: self.client.rename(item.path, dst),
                 on_ok=lambda r: (self.log_line(r, "ok"), self.refresh_remote()),
                 context="改名")

    # ------------------------------------------------------------------ help
    def show_help(self):
        messagebox.showinfo(APP_NAME + " 使用说明", """SwissFTP 把 PC 上的文件直接写进 GameCube 的 SD 卡。

前提
  1. Swiss 已启动并停在主界面（游戏运行时 httpd 不响应）；
  2. 主机和 PC 在同一局域网，Swiss 设置里网络已初始化；
  3. 当前设备是 SD 卡（切到 SMB/FTP/FSP 后 SD 卷会卸载）；
  4. 主机 IP 填 Swiss 的 IP（不是 PC 的 IP）。

用法
  顶部填 IP →「连接」→ 左栏选文件 →「上传选中」→ 右栏刷新可见。
  支持中文文件名、批量队列、进度/速率、下载、改名、删除。

FTP 网关
  点顶部「FTP 网关」会在本机 127.0.0.1:2121 起一个标准 FTP 服务，
  用 FileZilla、资源管理器或 curl 连 ftp://127.0.0.1:2121
  （用户名密码任意）就能直接读写 SD 卡。STOR 会先在本机缓冲整文件，
  因为固件 /upload 必须预先知道 Content-Length。
  命令行也可只跑网关：swiss_ftp.py --cli --gateway --host <ip>

为什么默认不用 FTP 协议
  Swiss 固件只有 FTP 客户端（用来浏览 PC 上的 FTP 服务器），
  没有对外开 FTP 服务。写入 SD 卡走的是固件 httpd(80) 的
  FatFs 端点 /upload /get /ls /rm /rename；网关只是把它翻译成 FTP。

限制
  · 固件没有 mkdir 端点：上传子目录前，目录要已存在；
  · 覆盖式写入，不支持断点续传；FAT32 单文件 < 4 GiB；
  · 服务端单线程，工具内所有请求自动串行。
""", parent=self)


# --------------------------------------------------------------------------- selftest
def run_selftest(with_gui: bool) -> int:
    """起 mock 服务，跑协议层全流程；with_gui 时再跑一遍 GUI 逻辑。"""
    import hashlib
    import tempfile
    import mock_swiss
    import swiss_client as sc

    srv, port = mock_swiss.start(0)
    c = sc.SwissClient("127.0.0.1", port)
    checks = []

    def check(label, cond, extra=""):
        checks.append((label, bool(cond), extra))
        print("  %-32s %s %s" % (label, "PASS" if cond else "FAIL", extra))

    tmp = Path(tempfile.gettempdir())
    src = tmp / "swissftp_selftest.bin"
    payload = bytes(range(256)) * 4097
    src.write_bytes(payload)
    try:
        check("ping", c.ping())
        check("detect_volume", c.detect_volume() == "sdb:/", c.volume)
        check("state", c.state().get("dev", "").startswith("SD Card"))
        check("listdir", any(e.name == "games" for e in c.listdir("sdb:/")))
        check("parent_remote", sc.parent_remote("sdb:/games/sub") == "sdb:/games/" and
              sc.parent_remote("sdb:/games") == "sdb:/")
        ticks = []
        c.upload(str(src), "sdb:/games/中文 测试.iso",
                 progress=lambda d, t, r: ticks.append(d), verify=True)
        check("upload CJK + verify",
              c.exists("sdb:/games", "中文 测试.iso").size == len(payload))
        check("progress callbacks", len(ticks) > 3 and ticks[-1] == len(payload))
        dst = tmp / "swissftp_selftest_out.bin"
        c.download("sdb:/games/中文 测试.iso", str(dst))
        check("download sha256", hashlib.sha256(dst.read_bytes()).digest() ==
              hashlib.sha256(payload).digest())
        c.rename("sdb:/games/中文 测试.iso", "sdb:/games/renamed.iso")
        check("rename", c.exists("sdb:/games", "renamed.iso") is not None)
        c.delete("sdb:/games/renamed.iso")
        check("delete", c.exists("sdb:/games", "renamed.iso") is None)
        try:
            c.listdir("sdb:/nope/")
            check("error propagation", False)
        except sc.SwissError:
            check("error propagation", True)

        print("  -- FTP 网关（标准 ftplib 客户端）--")
        import swiss_ftp_gateway as gateway
        check("FTP gateway end-to-end", gateway.selftest() == 0)

        if with_gui:
            print("  -- GUI 冒烟测试 --")
            # 自检不能碰用户的 ~/.swissftp.json：直接改本模块的全局。
            # （脚本以 __main__ 运行时 import swiss_ftp 会是另一个模块实例。）
            old_cfg = globals().get("CONFIG_PATH")
            globals()["CONFIG_PATH"] = tmp / "swissftp_selftest_config.json"
            out_dir = tmp / "swissftp_gui_out"
            out_dir.mkdir(exist_ok=True)
            old_yesno, old_error = messagebox.askyesno, messagebox.showerror
            messagebox.askyesno = lambda *a, **k: True
            messagebox.showerror = lambda *a, **k: None
            app = App({"host": "127.0.0.1", "port": port, "theme": "light",
                       "local_dir": str(out_dir), "gateway_port": 0}, mock_port=port)
            app.overwrite_all = True
            results = {"connect": False, "upload": False, "download": False,
                       "delete": False}

            def step():
                # 连接是异步的，等它完成后再走一遍真实 GUI 按钮逻辑
                if not app.connected:
                    app.after(150, step)
                    return
                results["connect"] = len(app.remote_pane.items) > 0
                check("GUI connect + list", results["connect"],
                      "%d 项" % len(app.remote_pane.items))
                # 顶部「FTP 网关」按钮：起服务并用标准 ftplib 客户端探一次
                try:
                    import ftplib
                    app.toggle_gateway()
                    f = ftplib.FTP()
                    f.connect("127.0.0.1", app.gateway.port, timeout=5)
                    f.login("gc", "gc")
                    names = f.nlst()
                    f.quit()
                    check("GUI FTP gateway", "games" in names, str(names))
                    app.toggle_gateway()
                except Exception as exc:
                    check("GUI FTP gateway", False, str(exc))
                # 直接构造任务（避开文件对话框），走真实队列 worker
                app.local_pane.set_items([Item("selftest.bin", len(payload), False,
                                               "", str(src))])
                app.local_pane.tree.selection_set("0")
                app._queue_put([str(src)])
                app.after(2500, finish)

            def finish():
                task = app.tasks[-1] if app.tasks else None
                results["upload"] = bool(task and task.state == "完成")
                check("GUI queue upload", results["upload"],
                      task.state if task else "no task")
                # 下载刚上传的文件
                app.refresh_remote()
                app.after(600, finish2)

            def finish2():
                entries = [i for i in app.remote_pane.items
                           if i.name == "swissftp_selftest.bin"]
                if entries:
                    app.remote_pane.tree.selection_set(
                        str(app.remote_pane.items.index(entries[0])))
                    app.download_selected()
                app.after(2500, finish3)

            def finish3():
                gets = [t for t in app.tasks if t.direction == "get"]
                results["download"] = bool(gets and gets[-1].state == "完成")
                check("GUI queue download", results["download"],
                      gets[-1].state if gets else "no task")
                # 删除（异步）
                app.refresh_remote()
                app.after(600, finish4)

            def finish4():
                try:
                    item = next(i for i in app.remote_pane.items
                                if i.name == "swissftp_selftest.bin")
                    app.remote_pane.tree.selection_set(
                        str(app.remote_pane.items.index(item)))
                    app.delete_remote()        # askyesno 已在自检里打桩为 True
                    app.after(900, finish5)
                except StopIteration:
                    check("GUI delete", False)
                    app._closing = True
                    app.cancel_event.set()
                    app.destroy()

            def finish5():
                gone = not any(i.name == "swissftp_selftest.bin"
                               for i in app.remote_pane.items)
                check("GUI delete", gone)
                app._closing = True
                app.cancel_event.set()
                app.destroy()

            app.after(300, step)
            app.mainloop()
    finally:
        if with_gui:
            globals()["CONFIG_PATH"] = old_cfg
            messagebox.askyesno, messagebox.showerror = old_yesno, old_error
        srv.shutdown()
        srv.server_close()

    failed = [n for n, ok, _ in checks if not ok]
    print("\n%d/%d PASS" % (len(checks) - len(failed), len(checks)))
    return 1 if failed else 0


# --------------------------------------------------------------------------- cli
def run_cli(args) -> int:
    c = SwissClient(args.host or DEFAULT_HOST, args.port or DEFAULT_PORT)
    if args.gateway:
        gw = None
        try:
            vol = c.detect_volume()
            gw = SwissFTPGateway(c, port=args.gateway_port,
                                 log=lambda m: print("[gateway] " + m, flush=True))
            print("SD 卡卷：%s\nFTP 网关：%s  （用户名/密码任意，Ctrl+C 退出）"
                  % (vol, gw.start()), flush=True)
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在停止…")
        except SwissError as exc:
            print("错误：%s" % exc, file=sys.stderr)
            return 1
        finally:
            if gw:
                gw.stop()
        return 0
    try:
        if args.ls:
            for e in c.listdir(args.ls):
                print("%s %10d %s" % ("D" if e.is_dir else "F", e.size, e.name))
        elif args.put:
            local, remote = args.put
            def pct(done, total, rate):
                sys.stderr.write("\r  %5.1f%%  %.0f KB/s" % (done * 100 / total, rate / 1024))
                sys.stderr.flush()
            print(c.upload(local, remote, progress=pct, verify=True))
            sys.stderr.write("\n")
        elif args.get:
            remote, local = args.get
            print("下载 %d 字节 -> %s" % (c.download(remote, local), local))
        elif args.rm:
            print(c.delete(args.rm))
        elif args.rename:
            print(c.rename(*args.rename))
        elif args.state:
            print(json.dumps(c.state(), ensure_ascii=False, indent=2))
        else:
            print("ping=%s volume=%s" % (c.ping(), c.detect_volume()))
        return 0
    except SwissError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    log_arg = None
    for i, a in enumerate(argv):
        if a == "--log" and i + 1 < len(argv):
            log_arg = argv[i + 1]
        elif a.startswith("--log="):
            log_arg = a.split("=", 1)[1]
    _setup_io(_wants_text([""] + argv), log_arg)

    ap = argparse.ArgumentParser(description="SwissFTP · PC 连 Swiss 传文件到 SD 卡")
    ap.add_argument("--version", action="version",
                    version="%s %s  (%s, Python %d.%d)" % (
                        APP_NAME, APP_VER,
                        "单文件 EXE" if FROZEN else "源码运行",
                        sys.version_info[0], sys.version_info[1]))
    ap.add_argument("--log", metavar="FILE",
                    help="把运行输出同时写入日志文件（自检/排错用）")
    ap.add_argument("--mock", nargs="?", const=18080, type=int, default=0,
                    metavar="PORT", help="启动本地模拟服务并用 GUI 连它")
    ap.add_argument("--selftest", action="store_true", help="协议层 + GUI 全流程自检")
    ap.add_argument("--protocol-selftest", action="store_true", help="只跑协议层自检")
    ap.add_argument("--cli", action="store_true", help="命令行模式")
    ap.add_argument("--host", default=None, help="Swiss 的 IP（不填则用上次保存的）")
    ap.add_argument("--port", type=int, default=None, help="默认 80")
    ap.add_argument("--theme", choices=("light", "dark"), default=None,
                    help="浅色 / 深色磨砂主题")
    ap.add_argument("--ls", metavar="REMOTE_DIR")
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--rm", metavar="REMOTE")
    ap.add_argument("--rename", nargs=2, metavar=("FROM", "TO"))
    ap.add_argument("--state", action="store_true")
    ap.add_argument("--gateway", action="store_true",
                    help="命令行只跑 FTP 网关（不启动 GUI）")
    ap.add_argument("--gateway-port", type=int, default=2121)
    args = ap.parse_args(argv)

    if args.selftest or args.protocol_selftest:
        return run_selftest(with_gui=args.selftest)
    if args.cli:
        return run_cli(args)

    mock_port = 0
    if args.mock:
        import mock_swiss
        srv, mock_port = mock_swiss.start(args.mock)
        print("mock Swiss httpd: 127.0.0.1:%d" % mock_port, flush=True)

    cfg = load_config()
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    if args.theme:
        cfg["theme"] = args.theme
    app = App(cfg, mock_port=mock_port)
    app._mock_port = mock_port
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
