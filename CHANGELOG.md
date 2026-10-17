# 0.1.0

- 全新实现：Windows 服务常驻，注销、切换用户不中断
- 断网检测与 Dr.COM 门户自动登录（探测、取码、识别、校验、提交）
- 验证码识别跑在独立子进程，按需拉起，空闲时不驻留
- 配置与凭据由服务统一管理：ProgramData + 受限 ACL + Machine DPAPI
- 密码密文绑定门户地址，改地址必须重输密码
- 托盘客户端：状态显示、启停监控、立即重连、设置、打开日志
- 多门户候选探测，失败按阶梯退避，不空转不刷屏
- 安装、升级、卸载：`NetAnchor.Cli.exe install | upgrade | uninstall | status`
