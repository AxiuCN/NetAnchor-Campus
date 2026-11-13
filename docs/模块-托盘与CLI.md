# 模块：托盘与 CLI

> 代码：`ui/tray.py`（托盘 + 设置窗口）、`ui/autostart.py`（自启）、`cli.py`（安装维护）、`ipc.py`（客户端）。
> 部署口径见 [部署与运维.md](部署与运维.md)，IPC 协议见 [架构.md](架构.md)。

## 1. 托盘

**定位**：只做 UI。状态与配置一律经 IPC 向服务要，自己不读配置文件、不碰凭据。

| 关注点 | 现状 |
|---|---|
| 单实例 | 命名互斥体 `Local\NetAnchorCampus.Tray`（会话级）；第二个实例手动启动会弹"已经在运行"，自启拉起的静默退出 |
| 状态刷新 | 后台线程每 5 秒 `status`；单次失败记日志后继续，不会让轮询线程静默死掉 |
| 菜单重建 | 只在 `(连通, state, last_error, 自启开关)` 变化时重建，图标按颜色缓存 |
| 菜单动作 | 立即重连 / 启动监控 / 停止监控 / 打开日志目录 / 设置… / 开机自启开关 / 退出托盘；**动作在后台线程执行**，不阻塞消息循环 |
| 设置窗口 | 独立的 tkinter 线程；保存走 `config.set`，改门户地址时必须同交密码（否则 `reauth_required`） |
| 日志 | `%LOCALAPPDATA%\NetAnchor-Campus\logs\tray.log` |
| 图标 | 运行时加载 `app.png`（打包在 `_internal/netanchor/ui/assets/`，用 `sys._MEIPASS` 定位）；托盘侧裁到内容边界并按比例铺满（母版的 6% 留白只属于 exe 图标），状态用**右下角角标颜色**表达（只在图标已挂进托盘时才落色：pystray 会丢弃不可见期间的更新）；素材缺失时退回纯色圆点，不影响启动 |
| 退出 | 退出托盘不影响服务；服务重建管道时托盘可能短暂显示"服务未连接"（有重试） |

**会话结束**：pystray 的窗口过程不处理 `WM_QUERYENDSESSION` / `WM_ENDSESSION`，所以托盘自己建了一个隐藏顶层窗口 `NetAnchorSessionEndWatcher`：查询阶段回 TRUE 放行，结束阶段停图标退出。没有它，系统会等到超时强杀并记一条"试图否决关机"（见 [历史与根因 · 托盘拖住关机](历史与根因.md)）。
另外，安装/升级前 CLI 会向这个窗口投递注册消息 `NetAnchor-Tray-Quit`，托盘收到后走同一条退出路径（`WM_CLOSE` 不可用：pystray 不处理它，走 `DefWindowProc` 会销毁窗口但进程留着）。

**崩溃不自愈**：托盘进程崩溃/被结束后不会自动回来，要等下次登录或手动启动。这是刻意选择——不引入守护进程（会与"不写包装器代替程序管理生命周期"冲突）；若将来确需，用系统原生的 `RegisterApplicationRestart`。

## 2. 开机自启

机制与注册项位置见 [部署与运维 · 托盘开机自启](部署与运维.md)。实现要点：

- `autostart.register_machine()` 写 `HKLM\...\Run`，带 `--autostart` 标记；只有**打包后**的 CLI 才写（从源码运行一律跳过，避免开发/测试改到真实机器）
- `autostart.ensure_registered()`：机器级项存在就复用，不存在才回退写 HKCU（兼容 0.2.3/0.2.4 装过的机器）
- 用户开关（`TrayAutostart`）关掉后，托盘被 `--autostart` 拉起时直接退出；手动双击不受影响
- 安装/升级会顺手清当前账户遗留的 HKCU 项，避免登录时拉起两份

## 3. CLI

`NetAnchor.Cli.exe` 的子命令全部需要管理员。前三个面向手动使用，后三个面向安装包（见 `docs/安装包.md`）：

| 子命令 | 作用 | 关键点 |
|---|---|---|
| `install [--source <目录>] [--dir <绝对路径>]` | 建目录、复制、建服务、写配置模板、写自启项、启动 | 已存在服务时拒绝，提示用 `upgrade` |
| `upgrade [--source <目录>]` | 干净安装：停服务 → 结束托盘/OCR → 备份 → 删旧 → 复制 → 重写自启 → 启动 | 失败回滚并恢复原运行状态；不能从安装目录里运行（会锁自身），0.2.2+ 自动改用发布包 CLI |
| `uninstall [--purge]` | 停服务 → 结束客户端 → 删自启项 → 删文件 → 删服务 | 文件删不掉就保留服务（已停止），返回 1 |
| `status` | 打印安装目录、配置文件、日志目录 | **不走 IPC**，只读服务注册信息与磁盘 |
| `--version` | 版本号 | 便于确认装的是哪一版 |
| `prepare [--install-dir <目录>]` | 停服务 → 请托盘与 OCR 退出 | 安装器升级前清场；不动文件，失败即非零 |
| `register --dir <目录> [--no-start] [--no-autostart]` | 建/修正服务、失败恢复动作、配置模板、自启项、启动 | 只登记不复制，文件由安装器铺；`--no-autostart` 会清掉旧项并复查 |
| `unregister [--purge]` | 停服务 → 结束客户端 → 删自启项 → 删服务 | 不删安装目录（文件归安装器） |

安装目录解析顺序：服务 `ImagePath` → 安装时记录的 `InstallDir` → 默认值。所以换盘符、改目录后其余命令仍然正确。

`install` 与 `register` 共用同一段服务登记实现（`_register_install`），服务生命周期只有一份代码。

升级/卸载前的进程清理只结束 **镜像路径落在安装目录里** 的 `NetAnchor.Tray.exe` / `NetAnchor.Ocr.exe`，不再按 exe 名字全局 `taskkill`；且优先请它们自己退出，超时才终止。

## 4. IPC 客户端

- 连不上时区分"服务未安装 / 未运行 / 在跑但没建管道"
- 发请求前校验对端身份（管道对端 PID == SCM 登记的进程、账户 LocalSystem、影像名匹配）；不符就拒绝发送并说明是哪一项不符
- 重试：`PIPE_BUSY`、`FILE_NOT_FOUND`（单实例重建空窗）、`PIPE_NOT_CONNECTED` / `BROKEN_PIPE`（连上后对端先关）都在超时窗口内重试；服务未安装/未运行时快速报错
- 开发态放行：`NA_IPC_INSECURE=1`（`--console` 下服务是 `python.exe`、非 SYSTEM）
