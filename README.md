# SwissFTP（swiss-gc-ftp）

> ⚠️ **禁止商业使用 / NON-COMMERCIAL USE ONLY**：本仓库的原创内容仅限非商业用途，禁止出售、预装到售卖的硬件/存储卡、付费代刷、广告变现等。详见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。
> 本仓库不含上游 GPL 源码，整个工具（含 EXE）适用该条款。

不用拔卡，把 PC 上的 ISO / DOL / 封面 / 补丁直接写进 GameCube 的 SD 卡。当前版本 **v1.0**。

**不需要改固件、不需要刷 `IPL.dol`**，用的是 Swiss 现行版本里已有的 httpd(80) FatFs 端点。配合 [swiss-gc-CN](https://github.com/hualazimo7/swiss-gc-CN) 汉化固件使用。

## 直接使用 EXE

下载 [`dist/SwissFTP.exe`](dist/SwissFTP.exe)（13.11 MiB，64 位 Windows 10/11），双击即可，**无需安装 Python**。图标与中文版本资源已内嵌；PyInstaller onefile + GUI 子系统，双击不会弹控制台黑窗。也可以双击 `src/启动 SwissFTP.bat`。

配置保存在 `~/.swissftp.json`（IP、端口、卷、主题、最近目录），EXE 与源码共用同一份。

## 为什么界面叫 FTP、协议却不是 FTP

Swiss 固件里只有 FTP **客户端**（用于浏览 PC 上开的 FTP 服务），**没有对外提供 FTP 服务**。PC 想写 SD 卡，现成通路只有 httpd 的 FatFs 端点。因此：

| 层 | 实现 |
|---|---|
| 双栏 GUI | FTP 客户端式操作（本地 ⇄ SD 卡、队列、进度、覆盖确认） |
| 传输后端 | Swiss httpd：`/ls` `/upload` `/get` `/rm` `/rename` `/state` |
| FTP 网关（可选） | 在 PC 本机 `127.0.0.1:2121` 起标准 FTP 服务，把 FTP 命令翻译成上述端点，供 FileZilla / 资源管理器 / curl 使用 |

## 快速开始

```text
# 1. 图形界面（推荐）
双击 dist\SwissFTP.exe
#    顶部填 Swiss 的 IP →「连接」→ 左栏选文件 →「上传选中」

# 2. 让 FileZilla / 资源管理器直接连 SD 卡
双击 src\启动 FTP 网关.bat
#    ftp://127.0.0.1:2121  用户名密码任意

# 3. 自检（无需实机，17 项）
SwissFTP.exe --selftest --log selftest.log
```

## 功能

| 功能 | 说明 |
|---|---|
| 双栏浏览 | 本地磁盘 / SD 卡，中文文件名、大小、类型、修改时间，双击进目录 |
| 批量上传 | 多选、选文件夹递归、覆盖/跳过/全部覆盖、串行队列、进度与速率 |
| 下载 / 改名 / 删除 | `/get` `/rename` `/rm`，带确认；下载先写 `.part` 再原子替换 |
| 卷自适应 | 按固件 `pick_upload_root` 顺序探测 `sdb:/ → sda:/ → sdc:/` |
| FTP 网关 | PASV/EPSV、LIST/NLST/MLSD、RETR/STOR、SIZE、DELE、RNFR/RNTO、UTF-8 |
| 主题 | 浅色 / 低饱和深色磨砂，一键切换并记忆 |

## 命令行

```text
SwissFTP.exe --version
SwissFTP.exe --selftest                       # 协议层 + GUI 全流程自检（无需实机）
SwissFTP.exe --protocol-selftest              # 只跑协议层
SwissFTP.exe --mock                           # 连本地模拟服务看界面

SwissFTP.exe --cli --host 192.168.1.2 --ls sdb:/games/
SwissFTP.exe --cli --put D:/game.iso sdb:/games/game.iso
SwissFTP.exe --cli --get sdb:/swiss/settings/global.ini out.ini
SwissFTP.exe --cli --rename sdb:/games/a.iso sdb:/games/b.iso
SwissFTP.exe --cli --rm sdb:/games/old.iso
SwissFTP.exe --cli --state
SwissFTP.exe --cli --gateway --host 192.168.1.2 --gateway-port 2121
```

其它开关：`--port`（默认 80）、`--theme light|dark`、`--log 文件.log`。带命令行参数时会自动挂到父控制台输出；从无控制台环境启动则新建一个。

## 固件侧限制（都不是本工具能绕过的）

- **前提**：Swiss 停在主界面、网络已初始化、当前设备是 SD 卡。切到 SMB/FTP/FSP 后 SD 卷卸载，`/upload` `/get` `/rename` 会返回 `FR_NOT_ENABLED(12)`；跑游戏时 httpd 不响应。
- **没有 mkdir 端点**：上传子目录前该目录必须已存在；网关的 `MKD` 会回 550。
- **覆盖式写入、无续传**：`/upload` 用 `FA_CREATE_ALWAYS` 且必须先给 `Content-Length`，所以 FTP `STOR` 会先把整文件缓冲到本机临时文件再转发（占等量磁盘）。
- **单线程 httpd**：一次只 accept 一个连接；工具内部用锁和单 worker 队列串行化。
- FAT32 单文件 < 4 GiB；空文件拒绝上传；请求头必须 < 1024 字节；路径里不能含 `..`。

详见 [`docs/协议与限制.md`](docs/协议与限制.md)。

## 改名前的 `FR_LOCKED(16)` 注意

Swiss 文件浏览器会持有**当前目录**内所有文件的 FatFs 句柄，导致该目录里任何 `/rename` 返回 `FR_LOCKED(16)`（根目录、其它目录正常）。按键注入切不走目录，单纯重启又会因 `recent.ini` 指回该目录重新上锁。批量改名前需临时把 `Recent_0` 指向别的目录并重启 Swiss。完整步骤见 [swiss-gc-CN 的相关文档](https://github.com/hualazimo7/swiss-gc-CN/blob/main/docs/%E6%B8%B8%E6%88%8F%E5%90%8D%E4%B8%AD%E6%96%87%E5%8C%96.md)。

## 目录

```
dist/SwissFTP.exe          成品，免装 Python
src/swiss_ftp.py            GUI + CLI 主程序
src/swiss_client.py         httpd 协议层（纯标准库，可单独 import）
src/swiss_ftp_gateway.py    标准 FTP 服务端（被动模式）
src/mock_swiss.py           照抄固件解析逻辑的本地模拟服务，用于无实机自检
src/build_exe.py / make_icon.py / SwissFTP.spec / version_info.txt   打包资源
src/assets/                 图标与预览
src/启动 SwissFTP.bat / 启动 FTP 网关.bat
docs/                       协议、功能、限制与开发记录
SHA256SUMS.txt               EXE 校验值
```

## 从源码运行 / 重新打包

```bash
pip install -r requirements.txt
py -3 src/swiss_ftp.py                  # GUI
py -3 src/swiss_ftp.py --selftest       # 自检
py -3 src/build_exe.py                  # 重新生成 dist/SwissFTP.exe
```

GUI、协议层与网关只用 Python 标准库；`requirements.txt` 里的 PyInstaller 仅用于打包（图标生成另需 Pillow）。EXE 是打包那一刻的源码快照，改过 `src/*.py` 后必须重新打包。

少数杀毒软件对 PyInstaller 单文件有启发式误报，若被拦截请加白名单，或从源码运行。

## 自检覆盖

`--selftest` 用 `mock_swiss.py` 起本地 httpd，依次验证：连通、卷探测、列目录、中文/空格文件名的上传与回读 SHA-256、上传校验、进度回调、改名、删除、错误传播、标准 `ftplib` 走网关的 LOGIN/NLST/CWD/LIST/RETR/STOR/SIZE/RENAME/DELE，以及 GUI 的连接、网关按钮、上传队列、下载队列、删除。实测 **17/17 PASS**。

## 许可与致谢

> ⚠️ 本仓库**全部原创内容禁止商业使用**。完整条款见 [`LICENSE`](LICENSE)，
> 范围界定与商用示例见 [`NOTICE.md`](NOTICE.md)。

- **本工具**（`src/`、`dist/SwissFTP.exe`、`docs/`）：《非商业使用许可 v1.0》，**禁止商用**
- **上游 Swiss**：[emukidid/swiss-gc](https://github.com/emukidid/swiss-gc)，GPL-2.0 全文见 [`LICENSE-GPL-2.0.txt`](LICENSE-GPL-2.0.txt)；本工具仅通过其 httpd 端点通信，未复制其源码
- 打包使用 [PyInstaller](https://pyinstaller.org/)
- 配套：[swiss-gc-CN](https://github.com/hualazimo7/swiss-gc-CN)（汉化固件）、[swiss-gc-Imager](https://github.com/hualazimo7/swiss-gc-Imager)（背景图工具）
