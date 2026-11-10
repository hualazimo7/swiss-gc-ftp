# 更新记录

## 发布规则（2026-11-10 起）

- 本库与 `swiss-gc-CN` 一样采用**不可变发布**：已发布的 tag / 附件 / Release 说明一律冻结，
  每次改动发新的 `vX.Y`，旧版本永久保留；禁止 `gh release upload --clobber`、
  `delete-asset`、改写已发布说明、强推 tag
- 完整规则见 [swiss-gc-CN 的发布规则](https://github.com/hualazimo7/swiss-gc-CN/blob/main/docs/RELEASE_POLICY.md)
- 现有 Release：`v1.0`（当前版本）；后续版本各自独立发布，旧版不覆盖

## 2026-11-09 — 增加禁止商用许可（非商业使用许可 v1.0）

- 仓库主许可改为《非商业使用许可 v1.0》：本工具全部源码、EXE、图标与文档仅限非商业用途，禁止出售、预装到售卖的硬件/存储卡、付费代刷、广告变现等商业使用
- 新增 [`LICENSE`](LICENSE) 正文与 [`NOTICE.md`](NOTICE.md)（范围界定、商用示例、授权申请方式）；原 GPL-2.0 全文原样留存为 `LICENSE-GPL-2.0.txt`
- 依 GPL-2.0 第 6 条，不对上游 Swiss / GNU Unifont 的 GPL 材料追加任何限制，该部分权利（含商业使用）不受影响
- 已按 GPL-2.0 分发的历史版本与既有 Release 附件，其授权不可撤销；本次变更对当前及后续版本生效
- README 顶部与许可章节已同步标注禁止商用

## v1.0（2026-11-08）— 单文件 EXE

- 用 PyInstaller 6 + Python 3.14 打成 `dist/SwissFTP.exe`（13.11 MiB，onefile + GUI 子系统）
- 双击只弹程序窗口，无控制台黑窗；自带 Python 运行时与 tkinter/Tcl-Tk，目标电脑**不需要装 Python**
- 内嵌程序图标与中文版本资源（产品名 / 描述 / 版本）
- 带命令行参数时自动挂到父控制台输出；无控制台环境则新建控制台；任何模式可加 `--log 文件.log`
- 验证在中文路径、PATH 里没有 Python 的环境下均可启动
- `--selftest` 实测 17/17 PASS（直接跑 EXE，不是跑源码）

## v1.0（2026-11-08）— 协议层与 GUI

- `src/swiss_client.py`：纯标准库实现 Swiss httpd 协议层，可单独 import
- `src/swiss_ftp.py`：双栏 GUI + CLI；本地 ⇄ SD 卡、批量上传队列、进度与速率、覆盖确认、下载/改名/删除、浅色与深色主题
- `src/swiss_ftp_gateway.py`：在 `127.0.0.1:2121` 起标准 FTP 服务，把 FTP 命令翻译成 httpd 端点，供 FileZilla / 资源管理器 / curl 使用；支持 PASV/EPSV、MLSD、UTF-8、RNFR/RNTO 等
- `src/mock_swiss.py`：照抄固件解析逻辑的本地模拟服务，无实机也能跑完整自检
- 卷自适应：按固件 `pick_upload_root` 顺序探测 `sdb:/ → sda:/ → sdc:/`

## 踩过的坑

- 固件 httpd 是**单线程**，一次只 accept 一个连接；工具内部用锁 + 单 worker 队列串行化
- `/upload` 必须先给 `Content-Length` 且用 `FA_CREATE_ALWAYS`，所以 FTP `STOR` 要先把整文件缓冲到本机临时文件再转发
- 固件没有 mkdir 端点，网关的 `MKD` 只能回 550；路径里不能含 `..`
- 请求头必须 < 1024 字节（固件 `temp[1026]`），空文件拒绝上传
- 切到 SMB/FTP/FSP 设备后 SD 卷卸载，`/upload` `/get` `/rename` 会返回 `FR_NOT_ENABLED(12)`
- 当前浏览目录内的文件被 FatFs 句柄占用时，`/rename` 返回 `FR_LOCKED(16)`

## 实机验证（2026-11-08）

在 GameCube 真机上完成：连通、卷探测、列目录、中文/空格文件名上传与回读 SHA-256、下载、改名、删除、标准 `ftplib` 走网关的全套命令，以及冷启动后的完整链路，全部 PASS。
