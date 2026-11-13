# Inno Setup 语言文件

`ChineseSimplified.isl` 为 Inno Setup 官方简体中文翻译，**随仓库分发**，不依赖编译器自带翻译：

- 来源：<https://github.com/jrsoftware/issrc/blob/main/Files/Languages/ChineseSimplified.isl>
- 版本标记：Inno Setup 6.5.0+
- 维护者：Zhenghan Yang (Kira)
- 取得日期：2026-11-14，SHA256 `e0b0b350e2245f3c5e65586dfe43d574f6e7f06f2261149aba284954b3fc9a8d`
- 许可与翻译说明见文件头注释；本目录只做原样存放，不修改内容

## 为什么要放进来

Inno Setup 6.7.3 的安装目录 `Languages\` 里**没有**简体中文（只带 29 种其它语言），官方页面虽把简体中文列为"official"并声明随安装包分发，实际安装后并不存在。而简体中文是本项目安装包的验收项（必须存在且默认），所以：

- 构建时用 `build\installer.iss` 里的 `MessagesFile: "languages\ChineseSimplified.isl"`
- `build\make_installer.ps1` 校验该文件存在，缺失即中止构建

## 更新方式

Inno Setup 大版本升级后，如果新增了必需的消息项，用上面的链接重新下载覆盖即可；覆盖后同步更新本文件里的 SHA256。