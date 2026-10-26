# NetAnchor-Campus

> 测试迭代中，暂不可用

莆田学院校园网 Dr.COM 断网自动重连工具。核心是 **Windows 服务**，用户注销、切换用户都不中断；托盘只是客户端。

## 当前状态（2026-10-18）

- 功能链路已完成，离线测试 95 项通过
- **尚未在校园网真机上验证**真实登录与断网重连
- **还没有安装包**：当前安装方式是在管理员终端执行 `NetAnchor.Cli.exe install`，不是双击 setup.exe

进度、验证范围与已知缺口见 [docs/audit-notes.md](docs/audit-notes.md)，更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 组成

| 可执行文件 | 作用 |
|---|---|
| `NetAnchor.Service.exe` | 核心服务：检测断网、走门户协议自动登录；以 `LocalSystem` 运行，开机自启 |
| `NetAnchor.Tray.exe` | 托盘客户端：显示状态、启停监控、改设置；退出不影响服务 |
| `NetAnchor.Ocr.exe` | 验证码识别工作进程，只在需要时被拉起 |
| `NetAnchor.Cli.exe` | 安装 / 升级 / 卸载 / 查看状态 |

## 安装

需要管理员权限（右键终端 → 以管理员身份运行）。解压发布包后，在该目录执行：

```powershell
.\NetAnchor.Cli.exe install
# 或安装到其它目录
.\NetAnchor.Cli.exe install --dir 'D:\Program Files\NetAnchor-Campus'
```

> 目前没有双击安装器；`install` 需要从管理员终端发起。安装包（setup.exe）尚未实现。
> 安装目录会记录在服务里，之后 `upgrade` / `uninstall` / `status` 都以服务实际注册的路径为准。

安装会：

1. 复制文件到安装目录（默认 `C:\Program Files\NetAnchor-Campus\`，可用 `--dir` 指定）
2. 在 `C:\ProgramData\NetAnchor-Campus\` 生成配置模板
3. 注册服务 `NetAnchorCampus`（自动启动、失败自动重启）
4. 启动服务

安装完成后启动托盘：`<安装目录>\NetAnchor.Tray.exe`。

## 配置

两种方式：

- 托盘菜单 → **设置…**（普通权限即可保存；修改门户地址时会要求重新输入密码）
- 直接编辑 `C:\ProgramData\NetAnchor-Campus\config.json`（文件 ACL 只给管理员，需要用管理员权限编辑）

关键配置项：

| 键 | 说明 |
|---|---|
| `portal.candidates[].host` | 门户地址；可配多个候选，启动时逐个探测 |
| `portal.candidates[].login_port` | 登录接口端口 |
| `portal.candidates[].eportal_port` | 取码接口端口 |
| `account.username` | 学号（不含运营商后缀） |
| `account.carrier` | `校园用户` / `校园电信` / `校园联通` / `校园其他` |
| `monitor.check_interval_seconds` | 在线检测间隔（秒） |
| `monitor.max_captcha_attempts` | 单轮登录的换码次数上限 |
| `monitor.backoff_seconds` | 失败退避阶梯（秒） |

密码以 Machine DPAPI 加密后存于 `password_protected`，不会以明文落盘；配置文件 ACL 只允许 `SYSTEM` 与 `Administrators` 读取。

密码密文与**门户地址**绑定：修改门户地址时必须同时重新输入一次密码，旧密文随即失效。这样即使有人改了地址，服务也不会把已存的密码发到新地址。

## 日常操作

```powershell
# 查看状态
.\NetAnchor.Cli.exe status

# 服务控制
sc.exe query NetAnchorCampus
sc.exe stop  NetAnchorCampus
sc.exe start NetAnchorCampus
```

托盘菜单：立即重连、启动/停止监控、设置、打开日志目录、退出托盘。

## 升级与卸载

```powershell
.\NetAnchor.Cli.exe upgrade      # 停服务 → 备份 → 替换 → 启动；失败自动回滚
.\NetAnchor.Cli.exe uninstall    # 删服务与安装目录，保留配置与日志
.\NetAnchor.Cli.exe uninstall --purge   # 连配置与日志一起删
```

## 日志

`C:\ProgramData\NetAnchor-Campus\logs\service.log`，按天轮转保留 7 份。日志不含密码与完整请求串。

## 已知限制

- 验证码识别不是 100%：单张约 85%，同一轮换码最多 3 次，仍失败则进入退避重试。
- 登录绑定发起请求的源 IP/MAC，**只有校园网所在的那台机器**安装才有意义。
- 学校升级门户系统后，接口参数可能变化。

## 开发

```powershell
python -m pytest                      # 离线套件
python -m netanchor.service_main --console   # 开发态前台运行（需在 src 下设置 PYTHONPATH）
pyinstaller build/netanchor.spec      # 打包到 dist/NetAnchor-Campus
```

开发依赖：Python 3.14、`ddddocr`、`pywin32`、`pystray`、`Pillow`、`PyInstaller`、`pytest`。

## 许可

GPL-3.0，见 `LICENSE`。
