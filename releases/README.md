# 安装包

这里放的是**编译好的 Windows 安装包**，给不想装 Python、不想配环境的人用。

| 文件 | 大小 | 说明 |
|---|---|---|
| `OceanTravelAgent-0.1.0-setup.exe` | 36 MB | 双击安装，带桌面图标、开始菜单、卸载项 |

## 怎么装

1. 下载 `OceanTravelAgent-0.1.0-setup.exe`
2. 双击运行
3. 如果弹出蓝色窗口「Windows 已保护你的电脑」：点 **更多信息** → **仍要运行**
   （软件没有买代码签名证书，不是病毒）
4. 装完桌面会出现「Ocean 旅行规划」，双击就能用

**不需要装 Python，不需要装任何运行库，不需要填 API key。** 程序内置了一份
低额度 key，装完直接能用。

## 装在哪

```
%LOCALAPPDATA%\Programs\OceanTravelAgent\
├── OceanTravelAgent.exe     主程序
├── _internal\               依赖（别删）
├── config.json              换自己的 key 就改这里
├── 使用说明.txt             一页纸说明
└── data\、logs\             运行后生成
```

装在用户目录而不是 Program Files，因为程序要往 exe 旁边写配置和会话记录；
Program Files 不可写，而且不报错，只会表现为「设置不生效」。

卸载只删 `logs\`，`data\` 和 `config.json` 一律保留。

## 系统要求

- Windows 10 / 11，**64 位**
- 需要联网（模型走 `api.deepseek.com`，国内直连）
- 32 位系统装不上；Windows 7 跑不起来

## 内置额度用完了怎么办

打开安装目录下的 `config.json`，把 `MODEL_API_KEY` 换成你自己的 key
（任何 OpenAI 兼容接口都行），关掉窗口重新打开。你填的值优先于内置 key，
换 key 不需要等新版本。

## 关于这个目录

`dist/` 是构建输出，每次打包都会重来，所以不进版本库；`releases/` 是**明确
要发布出去的那一份**，进版本库，方便直接从仓库下载。

重新生成：

```bash
python scripts/build_desktop.py --installer
cp dist/OceanTravelAgent-*-setup.exe releases/
```

> 注意：二进制文件进 git 会让仓库随版本线性变大（每版 +36 MB）。版本多了
> 之后建议改用 GitHub Releases 挂附件，仓库里只留 `dist/`（已忽略）。
