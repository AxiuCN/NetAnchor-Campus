# 架构设计

> 状态：已评审（2026-10-18，P0 产出）。
> 本文件随实现推进更新；行为变化在同一轮同步修改。
> 技术细节以本文件为准，未决项在 §14 单列。

## 1. 目标与边界

**目标**：在校园网所在机器上以 Windows 服务形态常驻，检测断网并按 Dr.COM 门户协议自动完成登录；用户注销、切换用户、托盘退出均不中断核心服务。

**非目标**：多账号管理、流量统计、远程控制、跨平台、修改学校侧任何配置。

## 2. 关键决策（ADR）

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | Windows 服务 + 独立托盘客户端 + 按需 OCR 子进程 | 只有服务能跨注销存活；托盘只做 UI；OCR 是否子进程待实测（D7） |
| D2 | 服务账户 `LocalSystem` | 校园认证绑定源 IP/MAC，与用户身份无关；不需要任何用户凭据 |
| D3 | 配置与凭据在 `%ProgramData%\NetAnchor-Campus\`，Machine DPAPI + ACL | 不绑定任何 Windows 用户，注销/切换用户后仍可读 |
| D4 | IPC 用 Named Pipe + 长度前缀 JSON；读写都对 `INTERACTIVE` 开放，安全边界改由**凭据密文绑定门户地址**承担（见 §20） | 提权门槛对"会被骗点 UAC 的用户"无效、对熟练用户纯摩擦；结构性绑定才是有效控制 |
| D5 | PyInstaller `onedir`，多个 exe 共享 `_internal` | 体积可接受（约 200 MB），启动快；裁剪只删确定无用的资源 |
| D6 | 验证码同轮换码重试（默认 3 次） | 单张识别成功率不足；且校验是消耗性的 |
| D7 | 内存占用作为独立指标；OCR 按需跑在独立子进程 | 实测：进程内驻留使服务 WS 从 25.7 MB 升到 72.7 MB（峰值 92.5 MB、Private 144 MB）；子进程模式下服务稳定在 25.9 MB，工作进程峰值 87.2 MB 且只在识别期间存在 |
| D8 | 安装目录可配置（`install --dir`，默认 `C:\Program Files\NetAnchor-Campus`，之后以服务 ImagePath 为准）；托盘自启由托盘自身写 `HKCU\...\Run`（默认开启，菜单可关） | 部署环境各异，路径不应写死；服务随开机启动，托盘随该用户登录启动，两者解耦但体验上不缺位 |

## 3. 组件与部署形态

```
┌──────────────────────────────────────────────────────────────┐
│ Windows Service（auto-start，LocalSystem，Session 0）          │
│  NetAnchor.Service.exe                                        │
│   ├── 配置与凭据管理（ProgramData + Machine DPAPI + ACL）       │
│   ├── 监控状态机（检测 → 门户探测 → 取码 → OCR → 校验 → 登录）   │
│   ├── 日志（ProgramData\logs）                                 │
│   ├── IPC 服务端（Named Pipe，SDDL 受控）                       │
│   └── OCR 子进程管理（按需拉起 / 空闲回收）                      │
└───────────────▲──────────────────────────────────────────────┘
                │ Named Pipe：长度前缀 JSON，单请求单响应
┌───────────────┴──────────────────────────────────────────────┐
│ 用户会话（可多个）                                             │
│  NetAnchor.Tray.exe —— 仅 UI：状态、设置、手动重连、打开日志目录 │
└──────────────────────────────────────────────────────────────┘

旁路命令行（需管理员）：
  NetAnchor.Cli.exe install | upgrade | uninstall | status | service-start | service-stop
```

安装后的目录：

```
C:\Program Files\NetAnchor-Campus\
├── NetAnchor.Cli.exe
├── NetAnchor.Service.exe
├── NetAnchor.Tray.exe
├── NetAnchor.Ocr.exe
├── _internal\               # 共享运行时
└── ocr\common_old.onnx      # 只保留分类模型
```

## 4. 进程与账户模型

| 进程 | 账户 | 会话 | 生命周期 |
|---|---|---|---|
| Service | LocalSystem | Session 0 | SCM 管理；注销、切换用户不影响 |
| Tray | 交互用户 | 各自会话 | 用户退出即退出；不影响服务 |
| Ocr | LocalSystem | Session 0 | 服务按需拉起，空闲超时回收 |

约束：

- 服务**不依赖任何用户环境**：不读 `%USERPROFILE%`、不依赖 HKCU、不写用户 AppData。
- 托盘与服务的唯一通道是 IPC；托盘退出、注销、切换用户都不终止服务。
- 同一会话允许多个托盘实例（只读状态）；写操作按 D4 鉴权。

## 5. 配置与凭据

**位置**：`%ProgramData%\NetAnchor-Campus\config.json`

**ACL**：仅 `SYSTEM` 与 `Administrators` 完全控制；普通用户不可读写。托盘读写配置一律经 IPC，不直接访问文件。

**凭据**：密码用 Machine DPAPI 加密（`CryptProtectData` + `CRYPTPROTECT_LOCAL_MACHINE`），并把**门户地址指纹**作为加密上下文（entropy）一起参与派生。地址一变，旧密文不可解 —— 这是"改地址"与"拿到密码"不能同时成立的机制（见 §20）。

**写入**：临时文件 + 原子替换；保留 `config.json.bak`；`config_version` 供升级迁移。

```jsonc
{
  "config_version": 1,
  "portal": {
    "candidates": [
      { "name": "主用", "host": "<portal-host>", "login_port": 80, "eportal_port": 801 }
    ],
    "probe_timeout_seconds": 3
  },
  "account": {
    "username": "",
    "carrier": "校园其他",
    "password_protected": "",
    "password_context": ""
  },
  "monitor": {
    "check_url": "http://connect.rom.miui.com/generate_204",
    "check_interval_seconds": 30,
    "connect_timeout_seconds": 5,
    "max_captcha_attempts": 3,
    "backoff_seconds": [5, 15, 30, 60, 120, 300]
  },
  "ocr": { "mode": "subprocess", "idle_exit_seconds": 60 },
  "logging": { "level": "INFO", "max_files": 7 }
}
```

- `portal.candidates` 不预置任何真实地址；仓库示例一律占位。
- `login_port` 与 `eportal_port` 分开配置，不假设同端口。

## 6. IPC 协议

- 管道名：`\\.\pipe\NetAnchor.Campus.v1`
- 管道就绪会写日志 `IPC 管道已就绪`；服务启动后 2 秒内没就绪会记 `IPC 管道未就绪`（含原因）——避免"服务 RUNNING 但 IPC 不可用"这种沉默失败
- 客户端连接失败会区分原因：服务未安装 / 服务未运行 / 服务在跑但管道未创建
- DACL：`SYSTEM` / `Administrators` 完全控制；`INTERACTIVE`（S-1-5-4）只有 `0x100083`（读数据、写数据、读属性、同步），**不含 `FILE_CREATE_PIPE_INSTANCE`**——普通用户无法再造同名实例冒充服务端
- 服务端首实例带 `FILE_FLAG_FIRST_PIPE_INSTANCE`：名字已被占用时启动即失败并记日志，不与冒充实例共存
- **客户端校验服务端**：发请求前取 `GetNamedPipeServerProcessId`，要求对端令牌为 `S-1-5-18`（LocalSystem）且镜像为 `NetAnchor.Service.exe`；任何一步失败就拒绝发送（开发态可用 `NA_IPC_INSECURE=1` 放行）
- 服务端逐请求校验客户端：`GetNamedPipeClientProcessId` + `OpenProcessToken`，记录调用者 SID / 会话 ID / PID；提权状态只写审计日志，不再作为放行条件
- 授权：读写命令都对 `INTERACTIVE` 开放。敏感组合（凭据 ↔ 门户地址）由密文绑定保护，而不是由提权门槛保护
- 帧：4 字节小端长度 + UTF-8 JSON；上限 64 KB；超时 5 s

命令集（首版）：

| 命令 | 类型 | 说明 |
|---|---|---|
| `status` | 读 | 状态、最近检测时间、下次检测时间、连续失败次数、最近错误、在用门户、运行时长、版本 |
| `config.get` | 读 | 返回配置，密码字段脱敏 |
| `logs.path` | 读 | 日志目录 |
| `config.set` | 写 | 更新配置（不含密码） |
| `creds.set` | 写 | 更新账号与密码 |
| `monitor.start` / `monitor.stop` | 写 | 启停监控 |
| `reconnect` | 写 | 立即触发一次重连 |

状态取值：`ONLINE` / `OFFLINE` / `RECONNECTING` / `PORTAL_UNREACHABLE` / `CONFIG_MISSING` / `STOPPED`。

错误码：`reauth_required`（改门户地址未同时提交密码）、`invalid_request`（缺 cmd）、`unknown_command`、`invalid_config`（配置校验失败）、`bad_frame`（帧非法）、`internal`（处理器异常）。

## 7. 服务生命周期与恢复

| 事件 | 行为 |
|---|---|
| 开机 | 服务自动启动；网络未就绪时按退避重试，不退出 |
| 崩溃 | SCM 失败恢复：重启 5 s → 10 s → 30 s，1 天后计数重置 |
| 卡死 | 内部看门狗：监控线程心跳超阈值 → 记录并重建线程 |
| 停止 | 处理 `STOP` / `SHUTDOWN` / `PRESTOP`：停线程、关 IPC、回收 OCR 子进程，退出码 0 |
| 配置变更 | IPC 写入后热加载，无需重启服务 |
| 注销 / 切换用户 | 无影响（Session 0） |

## 8. 安装 / 升级 / 卸载

三者均需管理员权限（UAC）。

**安装目录可配置**：`install --dir <绝对路径>`，默认 `C:\Program Files\NetAnchor-Campus`。
安装目录不以常量写死——已安装后，`upgrade` / `uninstall` / `status` 一律以**服务注册的 ImagePath** 为准（辅以安装时写入的 HKLM 记录），因此换盘符或自定义路径后其余命令仍然正确。

| 操作 | 步骤 | 失败处理 |
|---|---|---|
| install | 建目录 → 复制文件 → 创建服务（LocalSystem、auto-start、指向实际目录）→ 设置失败恢复动作 → 写初始配置模板 → 记录安装目录 → 启动 | 任一步失败回滚已做步骤，非零退出码 |
| upgrade | 停服务 → 备份当前目录 → 替换文件 → 迁移配置版本 → 启动 | 启动失败还原文件并恢复原运行状态 |
| uninstall | 停服务 → 结束客户端 → 删文件 → 删服务 → 询问后删 `%ProgramData%` | 文件删不掉时保留服务，状态可恢复 |

- 需要管理员的动作：创建目录与服务、写 ACL、设置恢复策略
- 安装不写任何用户目录、不注册计划任务、不创建快捷方式、不依赖 vbs/bat

## 9. 网络与认证流程

```
IDLE
 └─ 每 check_interval_seconds：GET check_url
      204 ──────────────► ONLINE（继续睡）
      非 204 ───────────► 探测门户候选（GET http://host:login_port/，期望 200 且 Server 含 DrcomServer1.0）
                            全部不可达 ──► PORTAL_UNREACHABLE → 退避 → 下一轮
                            有可用 ─────► 取码（eportal_port）
                                             │
                                             ▼
                                           OCR 识别
                                             │
                                             ▼
                             check_captcha（consumable：一次尝试只判一次）
                                   result == "1" ──► 登录 /drcom/login
                                                      result == "1" ──► 复检 check_url（204）──► ONLINE
                                                      否则 ──► 记录 msga → 退避
                                   否则 ──► 重新取码（≤ max_captcha_attempts 次）→ 仍失败则退避
```

硬性规则：

- 所有校内请求**显式直连**（`ProxyHandler({})`），不读系统代理、不读 `HTTP(S)_PROXY`
- 网络失败（超时/拒绝/DNS）与协议失败（状态码、JSON 异常）分开记录，均不触发进程退出
- 退避：`5, 15, 30, 60, 120, 300` 秒阶梯，300 封顶；同一失败原因不做逐条刷屏日志
- 每次校验失败必须重新取码，不复用同一张验证码

## 10. 内存与性能指标

**指标**：`WorkingSet64`、`PrivateMemorySize64`、`PeakWorkingSet64`；每秒采样 60 s，取稳定段均值。

| 状态 | 定义 |
|---|---|
| S1 服务空闲 | 服务已启动、监控停止 |
| S2 持续监控 | 监控运行、在线、OCR 从未加载 |
| S3 OCR 认证 | 强制一次取码与识别（测试钩子，不提交登录） |
| S4 回收后 | OCR 子进程退出后 |

**判定口径**：S2 与 S1 差值 < 30 MB 时，进程内懒加载更简单；否则默认子进程。**以实测数据为准，不预设结论。**

**P2 实测（2026-10-18，Python 3.14 + ddddocr 1.6.1）**：

| 形态 | 服务 WorkingSet | 服务 Private | OCR 工作进程峰值 |
|---|---|---|---|
| S1 仅核心模块 | 25.7 MB | 17.7 MB | — |
| S3 进程内 OCR 驻留 | 72.7 MB（峰值 92.5） | 144.4 MB | — |
| S3 OCR 子进程 | 25.9 MB | 17.8 MB | 87.2 MB |

结论：**采用子进程（`ocr.mode = "subprocess"`）**。按需拉起的单次开销实测约 **0.5 s**（含进程启动 + 模型加载 + 推理），一次断网重连可接受。

已知基线（spike，仅作参考，不是承诺）：ddddocr 导入+初始化约 0.25 s，单张识别约 10–15 ms。

## 11. 日志与可观测

- `%ProgramData%\NetAnchor-Campus\logs\service.log`，按天轮转保留 7 份
- 关键事件必记：启动/停止、配置变更、状态迁移、每次登录尝试的参数摘要、重试与退避、OCR 子进程启动与退出
- 脱敏：密码、`upass`、完整请求串不写；账号只留前 3 位
- 致命错误：服务写日志；托盘对用户可见提示

## 12. 安全边界

| 资产 | 保护 |
|---|---|
| 账号密码 | Machine DPAPI 加密，且加密上下文绑定门户地址指纹；配置文件 ACL 仅 SYSTEM/Administrators |
| 改地址 ≠ 拿密码 | 地址变更后旧密文不可解，服务进入"缺少凭据"，不会向新地址发送密码 |
| 配置变更 | 写审计日志（命令、是否提权、是否改了地址/密码）；用户名只记前 3 位 |
| 日志 | 不含凭据明文与完整请求串 |
| 仓库 | 不含真实账号、内网地址、MAC、机器名、本机绝对路径；实测记录只写 `.dsh/` |

服务以 LocalSystem 运行，权限高于必要值：这是为了"不依赖任何用户账户"而做的取舍，代价是服务进程被利用时影响面较大。缓解：服务只做与本项目相关的网络请求，不暴露网络监听端口；凭据不因地址变更而外泄。

**残余风险（明确接受）**：本机低权限进程仍可以通过 IPC 修改配置，造成自动重连失效（DoS）或设置一个它自己知道的新密码；但它**无法获得已存储的密码**。这在单用户个人机器上比"每次改设置都要求提权"更合理。

## 13. 仓库结构

```
NetAnchor-Campus/
├── AGENTS.md
├── README.md
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
├── config.example.json
├── docs/
│   ├── architecture.md          # 本文件
│   └── test-plan.md
├── src/netanchor/
│   ├── __init__.py
│   ├── errors.py                # NetworkError / ProtocolError
│   ├── http.py                  # 显式直连、JSONP 解析
│   ├── config.py                # 配置模型与校验
│   ├── config_store.py          # ProgramData + ACL + 原子写
│   ├── secrets.py               # Machine DPAPI
│   ├── portal.py                # 门户探测、取码、校验、登录
│   ├── monitor.py               # 状态机 + 退避
│   ├── ocr.py                   # 进程内识别器
│   ├── ocr_worker.py            # OCR 子进程入口
│   ├── ocr_client.py            # 服务侧调用子进程
│   ├── ipc.py                   # Named Pipe 服务端/客户端/鉴权
│   ├── service_api.py           # IPC 命令分发
│   ├── runtime.py               # 运行时装配：配置/日志/监控线程/IPC
│   ├── service_main.py          # 服务入口（SCM 交互 + --console）
│   ├── logging_setup.py         # 日志配置
│   ├── cli.py                   # install/upgrade/uninstall    [P5]
│   └── ui/
│       ├── tray.py              # 托盘客户端
│       └── dialogs.py           # 预留（设置窗口暂在 tray.py 内）
├── tests/
│   ├── unit/
│   └── integration/
├── tools/
│   └── measure_memory.py
└── build/
    └── netanchor.spec
```

## 14. 已知风险与未决项

| 编号 | 风险 / 未决 | 处理 |
|---|---|---|
| R1 | pywin32 对 Python 3.14 是否有 wheel | **已解决（P1）**：pywin32 312 提供 cp314 wheel，`win32serviceutil`/`win32service`/`servicemanager` 均可用 |
| R2 | PyInstaller 打包服务进程的已知坑（`sys.frozen`、工作目录、日志路径） | **已解决（P1）**：打包 exe 安装/启动/停止/卸载均通过；需 `--hidden-import win32timezone`；**安装必须调用打包 exe 自身**，`python xx.py install` 会注册成无参数的 `pythonservice.exe` 并导致 1053 |
| R3 | Machine DPAPI + ACL 组合是否真的挡住普通用户 | P3 用普通用户身份实测 |
| R4 | Named Pipe DACL 与 UAC 交互 | P3 实测 |
| R5 | OCR 子进程每次拉起约 0.3 s，失败重试会放大耗时 | S3 实测后决定 |
| R6 | 约 200 MB 体积、服务形态二进制的杀软误报 | 真机观察 |
| R7 | 验证码单张成功率仅约 85% | 换码重试 3 次；真机再抽 100–200 张复核 |

## 15. P1 实测结论（2026-10-18）

测试件：PyInstaller onedir 打包的最小服务（仅心跳与连通性轮询），`LocalSystem`，`AUTO_START`。

| 项 | 结果 |
|---|---|
| 安装 / 启动 / 停止 / 卸载 | 全部成功；卸载时服务收到 `STOP`，正常退出（`STOPPED uptime=1739s`），无残留进程 |
| 失败恢复动作 | `reset=86400`，`restart/5s → 10s → 30s` 生效 |
| 崩溃恢复 | `taskkill /f` 后 SCM 约 5 s 拉起新 PID，日志可见新的 `START` |
| 注销存活 | 注销（03:19:15）→ 重新登录（03:20:54）：**PID 不变**，心跳 5 s 间隔连续 |
| 切换用户 | 切到另一账户（03:22:51）→ 切回（03:26:16）：**PID 不变**，心跳连续 |
| S1 空闲内存 | WorkingSet 21.2 MB / Private 14.7 MB |
| S2 监控态内存（30 s 轮询，无 OCR） | WorkingSet 28.9 MB / Private 19.0 MB |
| S2−S1 | +7.7 MB（低于 30 MB 阈值，但 S3 的 OCR 增量待 P2 实测） |

遗留：S3/S4 内存、托盘退出存活已在 P2、P4 验证；重启自启已于 P5 由用户重启验证（见 §19）。

## 16. P2 实测结论（2026-10-18）

认证核心（门户客户端 + 监控状态机 + OCR 子进程）完成，离线套件 41 项通过、1 项按需跳过。

| 项 | 结果 |
|---|---|
| 真实门户只读联调 | 候选探测 `True`；不可达候选按 `NetworkError` 归类（不崩溃）；取码 855 B `GIF87a`；错值校验返回 `False` |
| 直连有效性 | `HTTP_PROXY` 指向死端口时，显式直连仍能访问本地桩服务 |
| OCR 单次开销（子进程） | 约 **0.5 s**（进程启动 + 模型加载 + 推理），三次实测 0.48–0.52 s |
| OCR 内存 | 见 §10：子进程模式服务稳定 25.9 MB，工作进程峰值 87.2 MB |
| 状态机 | 在线短路、门户不可达、换码重试上限、位数异常不消耗校验、登录失败立即退避，均有测试覆盖 |

设计取舍：登录被拒（密码错误/互踢/频控）时**不再连续换码重试**，直接交给外层退避，避免放大失败次数。

## 17. P3 实测结论（2026-10-18）

配置存储、凭据加密与 IPC 完成，离线套件 **67 项通过、1 项按需跳过**。

| 项 | 结果 |
|---|---|
| 目录 ACL | 仅 `SYSTEM` 与 `Administrators` 两个 ACE，继承被移除 |
| 配置文件 ACL | 同上；临时文件先设 ACL 再原子替换 |
| 密码落盘 | 文件里只有 `password_protected`（Machine DPAPI + 固定 entropy），无明文 |
| 解密 | 服务侧加载可还原明文；密文被篡改时抛 `SecretError`，不静默降级 |
| 原子写 | 覆盖前生成 `config.json.bak` |
| IPC 帧 | 长度前缀 JSON；拒绝截断、超长声明、非 JSON、非对象 |
| IPC 往返 | 本地管道实测 `status` / `creds.set` 成功 |
| 授权（P3 时点） | 注入式测试：非管理员只能读状态，5 个写命令全部返回 `forbidden`；该策略已在 §20 修订为"读写都开放，凭据绑定地址" |
| 真实令牌判定 | 提权进程 `True`；未提权的 `explorer.exe` 进程 `False` |

安全边界说明：Machine DPAPI 的作用域是整机，任何账户只要能读到密文就能解密，**真正的保护来自文件 ACL**。测试里同时断言了 ACL 只含两个 SID，而不是只测"能解密"。

IPC 鉴权实现要点：服务端用 `GetNamedPipeClientProcessId` 取调用者 PID，再读其进程令牌的 `TokenGroups` 判断调用者是否提权。§20 之后该结果**只写审计日志，不再作为放行条件**；判断时要求 `BUILTIN\Administrators` 组处于启用状态（UAC 过滤令牌里该组是 deny-only）。

## 18. P4 实测结论（2026-10-18）

服务运行时、日志与最小托盘完成，离线套件 **82 项通过、1 项按需跳过**。

| 项 | 结果 |
|---|---|
| 运行时启动（`--console`） | 正常；日志落到 `C:\ProgramData\NetAnchor-Campus\logs\service.log` |
| IPC 状态 | `status` 返回 `online`、`last_check_at`、`next_check_at`、`fails=0`；`version`、`logs.path` 均正确 |
| 真实 ProgramData ACL | 仅 `S-1-5-18`（SYSTEM）与 `S-1-5-32-544`（Administrators） |
| 托盘运行 | 进程存活，按 5 s 轮询 `status` |
| **托盘退出不影响服务** | 结束托盘进程后：运行时进程存活，`last_check_at` 由 `03:58:14` 前进到 `03:58:24`，`uptime=28s` 继续增长 |
| 配置热加载 | `config.set` 触发 `reload`，监控线程重启并采用新间隔 |

**未覆盖**：托盘的视觉呈现与设置窗口的实际点击交互需要人眼确认（VM 桌面，自动化无法验证）。留待 P5 安装后由用户检查。

## 19. P5 实测结论（2026-10-18）

打包与安装链路完成，离线套件 **91 项通过、1 项按需跳过**。

| 项 | 结果 |
|---|---|
| 构建 | 单个 onedir，四个入口共享 `_internal`；**215.9 MB** |
| 裁剪 | `_internal\ddddocr` 只保留 `common_old.onnx`；`cv2` 无 ffmpeg 解码器 |
| install | 复制 → 写配置模板 → 建服务（`LocalSystem`、`AUTO_START`）→ 启动；`sc query` 为 `RUNNING` |
| 安装态 IPC | `status` 返回 `online`；日志落 `ProgramData\...\logs\service.log` |
| 托盘（安装态） | `NetAnchor.Tray.exe` 启动并轮询；结束托盘后服务 PID `7572` 不变，`uptime` 继续增长 |
| upgrade | 停 → 备份 → 替换 → 启动；PID `7572 → 8424`，配置保留 |
| 崩溃恢复（安装态） | `taskkill` 后 12 s 内新 PID `8424 → 6264`，服务回到 `RUNNING` |
| uninstall | 服务删除、安装目录删除（含 `.bak`）、配置与日志保留 |
| uninstall --purge | 配置与日志一并删除；`status` 返回"未安装" |

过程中修的三处：

1. `win32service.CreateService` 在 pywin32 312 只接受 **13 个参数**（没有 description），描述改用 `ChangeServiceConfig2(SERVICE_CONFIG_DESCRIPTION)`。
2. 卸载时 `shutil.rmtree(ignore_errors=True)` 会静默留下被占用的文件（实测残留 8 个 pywin32 pyd）；改为**带重试的删除**，失败则报错而不是假装成功。
3. 升级时托盘进程仍会锁住 `NetAnchor.Tray.exe`，导致复制失败（实测）→ 升级前先结束 `Tray`/`Ocr` 进程、单文件复制带重试、回滚改用合并复制而不是 `copytree`。

**用户确认（2026-10-18）**：重启 VM 后 `sc query NetAnchorCampus` 为 `RUNNING`；日志中服务启动于 `04:09:40`，用户登录在 `04:15:18` —— **服务先于登录启动**。托盘显示与设置窗口正常（当时的写操作仍需管理员，该策略已在 §20 修订）。

验证时使用本地 204 桩作为 `check_url`，运行时全程 `online`、**未触发任何真实登录**。

## 20. 安全模型修订：用绑定代替提权（2026-10-18）

### 为什么改

原 D4 规定"写操作要求 Administrators"。实践与评审后确认这个控制不成立：

- UAC 是**知情同意**型控制：它只弹一个"是否允许 NetAnchor-Campus 修改设置"，**不会显示"要把门户地址改成 xxx"**。
- 会被社工引导的用户照样会点"是"；不会中招的熟练用户则每次改设置都多一步。
- 真正危险的是**组合**：改门户地址 + 服务持有明文密码 = 可以把密码送到任意地址。

### 改成了什么

1. 凭据密文的 DPAPI **entropy 绑定门户地址指纹**（`portal_fingerprint`：候选地址、两个端口的规范化串）。
2. 请求改地址时，必须**同时提交密码**（原子操作）；否则返回 `reauth_required`。
3. 地址改完后旧密文不可解；如果绕过 IPC 直接改文件，加载时 `password_context` 与当前指纹不符，凭据直接判定为不可用（`credentials_ready() == False`）。
4. 写操作不再要求提权；调用者是否提权只记入审计日志（`netanchor.audit`）。

### 效果

| 攻击 | 结果 |
|---|---|
| 低权限进程静默把地址改成自己的服务器 | 服务没有可用密码，不会外发；状态变为"缺少凭据" |
| 低权限进程改密码 | 登录失败（DoS），拿不到旧密码 |
| 用户主动改地址 | 界面要求重输密码，保存后重新绑定 |

### 验收

- `test_portal_change_requires_password`：改地址不交密码 → `reauth_required`
- `test_portal_change_with_password_rebinds_ciphertext`：交密码后密文变化且可解
- `test_changing_portal_invalidates_stored_password`：手工改文件 → 密码不可用
- `test_secret_context_binding`：不同上下文无法解密

## 21. 未完成项与后续计划

### P6 真机验证（未开始）

在校园网所在机器上执行：真实登录、断网 → 自动重连、日志核对。这是唯一还没做、且无法在开发机上替代的验证。

### P7 安装包（仅为候选，未定）

**现状**：交付形式是 onedir 目录 + 管理员终端执行 `NetAnchor.Cli.exe install`。功能完整，但分发体验是"开发者式"的：

- 需要手动复制整个目录
- 需要先开管理员终端
- 控制面板"程序和功能"里没有卸载项
- 没有安装向导、没有单文件分发

**候选方案（未验证，不作为承诺）**：`NetAnchor.Setup.exe`（PyInstaller onefile 内嵌 payload），双击 → 自身请求 UAC → 解压到临时目录 → 复用 `cli.install()`；同时写注册表卸载项。

**尚未验证的点**：安装器体积、UAC 与解压时序、失败回滚、卸载项的维护、杀软误报。

**当前结论**：**先维持 CLI 安装方式**，等 P6 真机验收有结论后再评估是否值得投入。不为了形式上的完整而叠加一套未经验证的安装器。

### 托盘开机自启（0.2.3 已实现）

托盘启动时按用户开关同步 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`：

- 开关存 `HKCU\Software\NetAnchor-Campus\TrayAutostart`，默认开启
- 开启时写入 `"<安装目录>\NetAnchor.Tray.exe"`；每次启动都会重写，换目录后能自愈
- 托盘菜单提供「开机自启托盘: [已启用]/[未启用]」切换
- 仅对打包后的 exe 生效；开发态（python 直接运行）不写注册表

这是"调用程序自身能力"的实现方式：没有快捷方式、没有 vbs，也不代替程序管理生命周期。

### 其他待办

- 长时间运行观察（数天）与日志轮转验证
- 杀软对服务形态二进制的误报观察
- 门户接口变化后的适配策略（当前以参数外置为主）

## 22. 外部复审整改（2026-10-22，2026-10-26 更新）

外部复审在固定提交 `447c2f7` 上确认了 1 项 P1 与 6 项 P2。**本轮只处理 P1（#1）**，其余保持待修，避免一次改动面过大。

### #1 客户端不验证服务端身份（P1）— 已修复

**原问题**：`INTERACTIVE` 的 DACL 用的是 `GRGW`，而 `GENERIC_WRITE` 在管道上包含 `FILE_CREATE_PIPE_INSTANCE`，普通用户可再造一个同名实例；客户端按名字直连、不校验对端，保存密码时可能把新密码交给冒充实例。

**修复**：

1. DACL 改为 `0x100083`（读数据 / 写数据 / 读属性 / 同步），去掉创建实例权限。
2. 服务端实例带 `FILE_FLAG_FIRST_PIPE_INSTANCE`，名字被抢占时启动失败并记日志。
3. 客户端改为 `CreateFile` + `GetNamedPipeServerProcessId`，发送前校验对端令牌为 `S-1-5-18` 且镜像是 `NetAnchor.Service.exe`；不通过就不发任何字节。
4. 开发态用 `NA_IPC_INSECURE=1` 显式放行（`--console` 下服务是 `python.exe`、非 SYSTEM）。

**验证**：

- 冒充实例（当前用户身份）→ 客户端拒绝、冒充端收不到任何请求（`tests/integration/test_pipe_identity.py`）
- 预期镜像不匹配 → 拒绝
- 放行模式 → 正常往返（证明拒绝原因是身份而非链路）
- 真实服务（SYSTEM + `NetAnchor.Service.exe`）→ 默认安全客户端连接成功、`status=online`

**未采用的选项**：`PIPE_REJECT_REMOTE_CLIENTS` 在本机 Windows 10 21H2 上被 `CreateNamedPipe` 以 `ERROR_INVALID_PARAMETER` 拒绝（实测），因此未使用；远程访问由 DACL 排除 NETWORK/ANONYMOUS 主体来限制。

**残余**：拿到 SYSTEM 令牌的攻击者不受这层保护——但那已经超出本工具的安全边界。

### #2 停止超时后线程引用被清空（P2）— 已修复

**原问题**：`_stop_monitor` 在 `join(10)` 之后无条件把引用置空；而 OCR 子进程超时是 30 s，线程可能仍在认证。再点"启动"就会起第二个线程，出现并行认证。

**修复**：

1. `Monitor` 持有停止事件，并在**取码前后、校验前、登录前**检查取消；取消时返回 `STOPPED`，不累加失败次数。
2. `_stop_monitor(wait)` 返回真实结果：没用退出就返回 `stopping`，**保留线程引用**。
3. 线程仍存活时 `_start_monitor` 拒绝启动（返回 `stopping`），不会出现第二个认证线程。
4. IPC `monitor.stop` 只等 1 s（客户端超时 5 s），服务停止与热加载则等满 OCR 宽限（默认 35 s）；旧线程未退出时热加载直接放弃，保留旧配置。
5. 线程结束时由 `_run_monitor` 的 `finally` 清理引用，状态由 `Monitor` 的最终回调置为 `stopped`。

**验证**：

- `test_stop_with_blocking_ocr_keeps_thread_and_blocks_restart`：阻塞识别器 → 停止返回 `stopping`、线程引用不变且仍存活、启动被拒；释放后线程退出、状态 `stopped`，再次启动正常。
- `test_cancel_during_validation_skips_login`：校验阶段置位取消 → 不再调用登录。
- `test_cancelled_before_cycle_returns_stopped`：已取消时不探测门户。
- 真实服务（IPC）：`monitor.stop → stopped`、`monitor.start → running`、状态回到 `online`。

### #3 凭据绑定失效后仍提交空密码（P2）— 已修复

**原问题**：`cycle()` 判断的是 `password or password_protected`，而不是"凭据是否真的可用"。地址变更后 `password_protected` 仍非空，于是继续探测、取码、校验，最后 `login(..., "")` 提交空密码，状态也不是 `config_missing`。

**修复**：

1. `cycle()` 改用 `Config.credentials_ready()`——它会校验 `password_context` 与当前门户指纹一致；不可用时直接返回 `CONFIG_MISSING`，不碰门户。
2. `_authenticate()` 入口再检查一次明文密码，缺失就抛内部信号 `_CredentialsUnavailable`，由 `cycle()` 归为 `CONFIG_MISSING`。
3. 登录调用前保留最后一道检查：没有明文密码绝不调用 `login`。

**验证**：

- `test_cipher_bound_to_other_portal_returns_config_missing`：密文绑定旧地址 → `CONFIG_MISSING`，探测/取码/登录调用数均为 0。
- `test_empty_password_never_reaches_login`：`credentials_ready` 为真但明文缺失的边界 → 仍然不取码、不校验、不登录。

### #4 升级失败后不恢复原服务（P2）— 已修复

**原问题**：`_stop()`、结束客户端、删除旧备份、`copytree` 备份都在 `try` 之外。备份这一步抛异常时，服务已经停了，既不起回来也不回滚。

**修复**：

1. 记录升级前的运行状态（`was_running`）。
2. 从 `_stop()` 之后的**所有步骤**（备份、删除旧备份、复制新文件、启动）都放进同一个 `try`。
3. 失败时调用 `_restore_previous_install()`：有备份就还原文件，然后**只在升级前服务是运行中时**才启动；升级前本就停止的，保持停止。
4. 恢复过程本身失败也会逐条打印，不会吞掉错误。

**验证**（`tests/unit/test_cli_upgrade.py`，注入文件系统失败）：

- 备份阶段失败 → 原安装目录不被破坏、服务被起回来。
- 复制新文件阶段失败 → 从备份还原旧文件、服务恢复运行。
- 升级前服务是停止的 → 失败后不擅自启动。

### #5 卸载不先关闭客户端，删除中断（P2）— 已修复

**原问题**：`uninstall()` 只停服务、删服务，没有结束托盘/OCR 进程；托盘占着 `NetAnchor.Tray.exe` 时 `_remove_tree` 耗尽重试后抛错，此时服务已删、目录残留，`--purge` 也走不到。

**修复**：

1. 停止服务后、删除文件前，先调用 `_close_running_clients()` 结束 `NetAnchor.Tray.exe` / `NetAnchor.Ocr.exe`。
2. **调整顺序**：先删文件、再删服务。文件删不掉时服务仍然存在（已停止），状态可恢复；用户关掉占用程序后重跑 `uninstall` 即可。
3. 删除失败时列出原因并返回 1，不再继续往下走。

**验证**（`tests/unit/test_cli_uninstall.py`）：

- 顺序断言：`stop → close → remove → delete_service`
- 删除失败：返回 1、**不删服务**、输出"未清理干净"
- 未安装服务但目录存在：仍能清理文件，不尝试删除服务

### #6 `--purge` 清理失败仍返回 0（P2）— 已修复

**原问题**：配置目录清理用 `shutil.rmtree(ignore_errors=True)`，随后无条件打印"已删除配置与日志"并返回 0。文件被占用时静默失败、谎报成功。

**修复**：

1. 改用带重试的 `_remove_tree()`（失败会抛 `CliError`）。
2. 删除后再检查目录是否真的不存在。
3. 只有确认删除成功才打印成功信息；失败时打印未删除路径并返回 1。

**验证**：

- `tests/unit/test_cli_purge.py`：成功、删除抛错、删除"成功"但目录仍在、无 `--purge` 四种路径。
- 真实文件锁：锁住 `config.json` 后执行 `uninstall --purge` → **退出码 1**、目录保留、输出"清理失败/仍存在"；释放锁后重跑 → 退出码 0、目录消失。

### 测试基建：非管理员也能跑

复审用普通令牌执行 pytest 得到 25 项 PermissionError，原因是测试里 `ConfigStore` 会顺手收紧 DACL。

- `ConfigStore` 新增 `apply_acl` 参数；非 ACL 用例用 `apply_acl=False`，不触碰 DACL。
- 只有两个真正验证 ACL 的用例保留默认值，并在非提权环境下 `skip`（而不是失败）。

这样外部审计用普通账户跑测试时，结果是"通过 + 少量 skip"，而不是一片红色错误。

## 23. 现场问题修复（2026-10-27）

真机安装后出现两个现象：服务 `RUNNING` 但 `\\.\pipe\` 里没有 `NetAnchor.Campus.v1`；托盘设置报"连接服务失败：系统找不到指定的文件"；多次启动托盘出现重复图标。

### 根因：打包内有两个 pywintypes DLL

`_internal` 里同时存在：

- `pywin32_system32\pywintypes314.dll`（PyInstaller 的 pywin32 hook 收集）
- `pywintypes314.dll`（被二进制依赖分析从基础 Python 目录额外收集）

Windows 加载 `win32\win32file.pyd` 时解析到根目录那份，而 Python 侧的 `pywintypes` 模块用的是 `pywin32_system32` 那份——**同一个 DLL 的两份加载，产生两个互不相等的 `pywintypes.error` 类**。

后果：客户端连上就断时 `ReadFile` 抛 `ERROR_BROKEN_PIPE(109)`，`except pywintypes.error` 因为类身份不同而抓不住，异常穿出到线程顶层，IPC 服务端线程终止、管道消失，此后所有连接都报"找不到指定的文件"。

### 修复

1. 打包 spec 里去掉根目录那份 `pywintypes314.dll`，只保留 `pywin32_system32` 下的。
2. IPC 服务端按错误码而不是异常类判断：`109`（管道已结束）/`233`（未连接）视为正常断开。
3. 单个连接出异常不再终止服务端循环，记 warning 后继续服务。
4. 托盘单实例（命名互斥体）；`--version` 便于确认包版本；客户端错误按服务状态分类。

### 验证

- 单元/集成测试：`129 passed, 2 skipped`（含"客户端连上就断，服务端仍可继续响应"用例）。
- 打包产物：`_internal` 只剩一份 `pywin32_system32\pywintypes314.dll`。
- 冻结服务实测：模拟客户端连上即断后，日志无异常、管道仍存在、后续 `version` 请求正常返回。
