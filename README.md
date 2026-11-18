<p align="center">
  <img src="src/netanchor/ui/assets/app.png" width="132" alt="NetAnchor-Campus 图标">
</p>

<h1 align="center">NetAnchor-Campus</h1>

<p align="center">莆田学院校园网 Dr.COM 断网自动重连 · Windows 服务 + 托盘</p>

<p align="center">
  <a href="https://github.com/AxiuCN/NetAnchor-Campus/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/AxiuCN/NetAnchor-Campus/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="版本" src="https://img.shields.io/badge/version-0.5.0-2ecc71">
  <img alt="平台" src="https://img.shields.io/badge/platform-Windows%2010%201607%2B-0078d4">
  <img alt="许可" src="https://img.shields.io/badge/license-GPL--3.0-blue">
</p>

---

核心是一个 **Windows 服务**：注销、切换用户、锁屏都不会中断；托盘只是它的客户端，关掉托盘不影响重连。断网后由服务自己探测门户、取验证码、识别、登录，并把结果以状态色显示在托盘角标上。

## 托盘图标即状态

角标颜色跟着服务状态走：

| 角标 | 状态 | 含义 |
|---|---|---|
| 🟢 | 已连接 | 在线，无需动作 |
| 🔴 | 重连中 | 正在登录，通常几秒内结束 |
| 🟠 | 门户不可达 | 探测不到认证门户（真离线、代理劫持或被 AC 拦） |
| 🟠 | 缺少配置 | 还没填账号或密码 |
| ⚪️ | 已停止 | 服务未运行或托盘连不上服务 |

## 组成

| 可执行文件 | 会话 / 账户 | 作用 |
|---|---|---|
| `NetAnchor.Service.exe` | Session 0 / LocalSystem | 检测断网、走门户协议登录、IPC 服务端；开机自启 |
| `NetAnchor.Tray.exe` | 交互用户 | 状态显示、启停监控、改设置；退出不影响服务 |
| `NetAnchor.Ocr.exe` | Session 0 / LocalSystem | 验证码识别，按需拉起、识别一张即退出 |
| `NetAnchor.Cli.exe` | 管理员终端 | 安装 / 升级 / 卸载 / 状态，以及安装包用的内部命令 |

## 安装

到 [Releases](https://github.com/AxiuCN/NetAnchor-Campus/releases/latest) 下载
`NetAnchor-Campus-<版本>-setup.exe`，双击运行（会请求 UAC）。

安装会：

1. 复制文件到 `C:\Program Files\NetAnchor-Campus`（向导里可改）
2. 注册服务 `NetAnchorCampus`（LocalSystem、开机自启）与机器级托盘自启项
3. 在控制面板「程序和功能」里登记卸载项

> 安装包**未签名**，SmartScreen 会提示“未知发布者”，需要手动选择“仍要运行”。

> 批量部署或无法使用安装包时，可用命令行（`NetAnchor.Cli.exe`），
> 步骤见 [docs/部署与运维.md](docs/部署与运维.md) 的“命令行（维护与自动化）”。

## 首次配置

安装后托盘会自动起来（也可手动运行 `<安装目录>\NetAnchor.Tray.exe`），右键 → **设置…**，填：

| 项 | 说明 |
|---|---|
| 学号 | 不含运营商后缀 |
| 密码 | 需与门户地址同时提交；改地址后旧密码作废，必须重输 |
| 运营商 | `校园用户` / `校园电信` / `校园联通` / `校园其他` |
| 门户地址 / 端口 | 认证网关的地址与登录、取码端口 |
| 检测间隔 | 在线检测周期，默认 30 秒 |

密码用 **Machine DPAPI** 加密后写入 `C:\ProgramData\NetAnchor-Campus\config.json`，且**密文与门户地址绑定**——即使有人改了地址，服务也不会把已存的密码发到新地址。配置文件 ACL 只给 `SYSTEM` 与 `Administrators`。

## 日常操作

托盘菜单：立即重连、启动/停止监控、设置…、打开日志目录、开机自启托盘开关、退出托盘。

```powershell
& '<安装目录>\NetAnchor.Cli.exe' status
sc.exe query NetAnchorCampus
sc.exe stop  NetAnchorCampus
sc.exe start NetAnchorCampus
```

托盘自启由安装程序写 `HKLM\...\Run`（`"<安装目录>\NetAnchor.Tray.exe" --autostart`），**所有本地用户登录都会拉起**；单个用户可以在菜单里关掉，关闭只写该用户自己的开关，手动双击仍可用。

## 升级与卸载

```powershell
# 有安装包时：直接运行新版本 setup.exe
# 或命令行：
& '<安装目录>\NetAnchor.Cli.exe' uninstall      # 保留配置与日志
.\NetAnchor.Cli.exe install --dir '<安装目录>'   # 再装新版
```

安装包升级会先停服务、把旧目录改名为 `<安装目录>.previous` 再铺新文件，失败时自动回滚；卸载时会询问是否同时删除配置与日志，**默认保留**。

## 日志

| 来源 | 位置 | 轮转 |
|---|---|---|
| 服务 | `C:\ProgramData\NetAnchor-Campus\logs\service.log` | 按天，保留 7 份 |
| 托盘 | `%LOCALAPPDATA%\NetAnchor-Campus\logs\tray.log` | 按天，保留 7 份 |

日志不含密码原文与完整请求参数串，账号只留前 3 位。排障顺序见 [docs/部署与运维.md](docs/部署与运维.md)。

## 已知限制

- 验证码识别不是 100%：单张约 85%，同一轮最多换码 3 次，仍失败则进入退避重试
- 登录绑定发起请求的源 IP/MAC，**只有在校园网那台机器上安装才有意义**
- 学校升级门户系统后接口参数可能变化
- 未签名，SmartScreen 会拦一次
- 约 216 MB（含 OCR 模型与 ONNX 运行时）

## 从源码开发

需要 **Python 3.12+**（Windows x64）。

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest        # 离线套件，不需要管理员
```

打包与安装器：

```powershell
python -m PyInstaller build\netanchor.spec    # onedir → dist\NetAnchor-Campus
pwsh -File build\make_installer.ps1           # 安装包 → dist\installer（需 Inno Setup 6）
```

开发态前台跑服务：`$env:PYTHONPATH='src'; python -m netanchor.service_main --console`。

## 文档

| 想了解 | 读 |
|---|---|
| 全部文档索引 | [docs/README.md](docs/README.md) |
| 设计决策、进程模型、IPC、安全边界 | [docs/架构.md](docs/架构.md) |
| 门户协议、参数与错误码 | [docs/协议-门户.md](docs/协议-门户.md) |
| 安装升级卸载、排障 | [docs/部署与运维.md](docs/部署与运维.md) |
| 安装包构建与发布 | [docs/安装包.md](docs/安装包.md) |
| 测试分层与门禁 | [docs/测试.md](docs/测试.md) |
| 已知风险与明确不做的事 | [docs/待办.md](docs/待办.md) |
| 更新记录 | [CHANGELOG.md](CHANGELOG.md) |

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。

## 图标与第三方内容

`src/netanchor/ui/assets/` 下的应用图标是《原神》角色（胡桃）的同人二创，由 AI 图像生成工具产出，仅用于本项目标识。角色形象版权归米哈游所有，本项目与之无从属或授权关系。图标不在 GPL-3.0 的授权范围内，请勿单独提取使用。