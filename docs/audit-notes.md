# 审计导览

> 面向外部评审者。想快速判断"这个项目做到哪一步、凭什么、有哪些没验证"，看这一份就够。
> 细节见 [architecture.md](architecture.md) 与 [test-plan.md](test-plan.md)。

## 1. 这是什么

校园网 Dr.COM 断网自动重连工具。运行形态是 **Windows 服务**（`LocalSystem`、开机自启）+ **独立托盘客户端**，两者通过受控的本机命名管道通信。服务负责检测连通性、探测门户、取验证码、识别、校验、登录；托盘只做界面。

## 2. 当前进度（2026-10-18）

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 架构与测试计划 | ✅ 完成 |
| P1 | 服务骨架（安装/卸载/崩溃恢复/注销存活/内存基线） | ✅ 完成（VM 实测） |
| P2 | 认证核心（门户协议、状态机、OCR 子进程、离线测试） | ✅ 完成 |
| P3 | 配置与凭据（ProgramData + ACL + DPAPI）、IPC | ✅ 完成 |
| P4 | 服务运行时、日志、最小托盘 | ✅ 完成 |
| P5 | 打包与安装链路（CLI 方式） | ✅ 完成（VM 实测，含重启自启） |
| P6 | **真机验证**（校园网那台机器上的真实登录与断网重连） | ⏳ 未开始 |
| P7 | **安装包**（双击 setup.exe、控制面板卸载项） | ⏳ 仅候选方案，未实现、未验证 |

**结论先说**：功能链路已通，VM 内可验证的部分都验证过；**真实登录从未执行**（遵守工作区约束），安装体验目前是"CLI 式"而不是安装包。

### 外部复审整改进度（2026-10-22 复审，2026-10-26 更新）

| 编号 | 问题 | 状态 |
|---|---|---|
| #1 | 客户端不验证服务端身份，冒充实例可收走新密码（P1） | ✅ 已修复（2026-10-26） |
| #2 | 停止超时后线程引用被清空，重启会并行认证（P2） | ✅ 已修复（2026-10-26） |
| #3 | 凭据绑定失效后仍提交空密码（P2） | ✅ 已修复（2026-10-26） |
| #4 | 升级备份失败后不恢复原服务（P2） | ✅ 已修复（2026-10-26） |
| #5 | 卸载不先关闭客户端，删除中断（P2） | ✅ 已修复（2026-10-26） |
| #6 | `--purge` 清理失败仍返回 0（P2） | ⏳ 待修 |

#1 的机制与验证见 [architecture.md §22](architecture.md#22-外部复审整改2026-10-22-2026-10-26-更新)。#2–#6 尚未改动。

## 3. 交付形态现状（重要）

现在的"安装"是：

1. 把 `dist\NetAnchor-Campus\` 整个目录拷到目标机
2. 以**管理员终端**执行 `NetAnchor.Cli.exe install`

`install` 会复制到 `C:\Program Files\NetAnchor-Campus\`、写配置模板、注册服务、启动。

**还没有**：双击即用的 `setup.exe`、自带 UAC 提权、控制面板"程序和功能"卸载项、安装向导、单文件分发。这些属于 P7 的候选方案，**尚未验证**，当前不承诺一定做。

## 4. 代码地图

| 模块 | 职责 | 审计关注点 |
|---|---|---|
| `src/netanchor/http.py` | 显式直连 HTTP、JSONP 解析 | `ProxyHandler({})` 是否真的绕过系统代理 |
| `src/netanchor/portal.py` | 探测、取码、校验、登录 | 登录参数、URL 拼接、是否有凭据进日志 |
| `src/netanchor/monitor.py` | 状态机、换码重试、退避 | 失败是否会无限重试、日志是否刷屏 |
| `src/netanchor/config.py` | 配置模型、校验、门户指纹 | 校验是否真的拦截占位符/非法端口 |
| `src/netanchor/config_store.py` | ProgramData、ACL、原子写、加解密 | ACL 内容、备份、原子替换 |
| `src/netanchor/secrets.py` | Machine DPAPI，上下文绑定 | 加密上下文是否随地址变化 |
| `src/netanchor/ipc.py` | 管道、帧、鉴权 | SDDL、客户端 PID 校验、帧上限 |
| `src/netanchor/service_api.py` | 命令分发、`reauth_required`、审计 | 写操作是否可能把密码送出去 |
| `src/netanchor/runtime.py` | 装配与热加载 | 配置变更是否真的重启监控 |
| `src/netanchor/service_main.py` | SCM 宿主 | 停止/关机处理、启动失败是否可见 |
| `src/netanchor/cli.py` | 安装/升级/卸载 | 部分失败是否回滚、卸载是否残留 |
| `src/netanchor/ui/tray.py` | 托盘与设置窗口 | 是否把敏感字段误显示、错误提示是否清楚 |
| `build/netanchor.spec` | 打包 | 是否误把模型/ffmpeg 打进去、入口是否四个 |

## 5. 安全设计摘要

| 资产 | 控制 |
|---|---|
| 账号密码 | Machine DPAPI 加密，**加密上下文绑定门户地址指纹**；配置文件 ACL 仅 `SYSTEM` + `Administrators` |
| 改地址 = 拿不到密码 | 地址变更后旧密文不可解；服务进入"缺少凭据"，不会向新地址发送密码 |
| IPC | Named Pipe，DACL 限 `SYSTEM`/`Administrators`/`INTERACTIVE`；逐请求校验客户端 PID |
| 写操作 | **不要求提权**（2026-10-18 修订，理由见 architecture §20）；记录审计日志 |
| 日志 | 不含密码、`upass`、完整请求串；账号只留前 3 位 |
| 仓库 | 无真实账号、内网地址、MAC、机器名、本机绝对路径 |

**已知残余风险**：本机低权限进程可通过 IPC 修改配置，导致自动重连失效（DoS）或设置一个它知道的新密码；但**无法获得已存储的密码**。这是明确接受的取舍。

## 6. 验证情况

| 项 | 结果 | 证据位置 |
|---|---|---|
| 离线测试 | `95 passed, 1 skipped`（2026-10-18 安全模型修订后的结果；P5 阶段记录为 91 项，新增 4 项覆盖 `reauth` / 重新绑定 / 上下文绑定） | 本机 `pytest` 输出；测试代码随安全修订一并提交 |
| 服务生命周期 | 安装/启动/停止/卸载、崩溃 5 s 自动拉起、注销与切换用户 PID 不变 | `.dsh/spike/svc/alive-*.json`（本机，未入库） |
| 安装与升级 | 安装、升级、崩溃恢复、卸载、`--purge` 全部实测 | architecture §19 |
| 内存 | S1 25.7 MB / S3 子进程 25.9 MB（工作进程峰值 87.2 MB） | architecture §10 |
| 重启自启 | 服务 04:09:40 启动，用户 04:15:18 登录 | architecture §19 |
| **真实登录 / 断网重连** | **未验证** | 需在校园网机器上执行 |

> 测试数的追溯方式：`95` 对应包含 `src/` 与 `tests/` 的初始提交；此后只提交过文档，测试数不变。若后续代码变更，请以当时的 `pytest` 输出为准。

## 7. 已知问题与取舍

1. 验证码单张识别约 85%，靠同轮换码 3 次重试；仍失败则退避。
2. 升级时需要先结束托盘进程（已自动处理），否则 exe 被占用。
3. 体积约 216 MB（onedir）；onefile 更小但启动慢、杀软误报风险更高。
4. 服务以 `LocalSystem` 运行，权限高于必要值（为了不依赖任何用户账户）。
5. 安装体验目前是 CLI，不是安装包（P7 仅为候选方案，尚未验证）。
6. **托盘开机自启未实现**：架构里的 D8 写了 `HKCU\...\Run`，但 `ui/tray.py` 没有对应逻辑，托盘目前要手动启动。
6. 未在真实校园网环境跑过，接口行为以旧实现与只读探测为准。

## 8. 给审计者的检查点

- 凭据是否会进日志：搜 `password` / `upass` / `logging` 的调用点
- 直连是否生效：`http.py` 的 `ProxyHandler({})`，以及测试里的代理环境变量用例
- ACL 是否真的只给两个 SID：`config_store.py` 与 `tests/integration/test_config_store_acl.py`
- 改地址能否带走密码：`secrets.entropy_for`、`config.portal_fingerprint`、`service_api._set_config`
- 失败是否可见：`runtime` 的异常处理、CLI 的退出码、服务事件日志
- 卸载是否残留：`cli.uninstall` 的删除重试与 `.bak` 处理

## 9. 如何复现

```powershell
python -m pytest                     # 需要 Python 3.14 + 依赖
pyinstaller build/netanchor.spec     # 产物在 dist/NetAnchor-Campus
.NetAnchor.Cli.exe install           # 管理员终端
```

依赖：Python 3.14、`ddddocr`、`pywin32`、`pystray`、`Pillow`、`PyInstaller`、`pytest`。

## 10. 文档索引

- [architecture.md](architecture.md)：决策、协议、安全模型、各阶段实测结论
- [test-plan.md](test-plan.md)：四层测试与阶段门禁
- [../README.md](../README.md)：安装、配置、日常操作
