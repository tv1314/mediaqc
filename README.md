# 媒体画质管家 · fnOS

扫描媒体库里视频的真实画质，按分辨率、码率、编码、抽帧画面体检综合分级，支持预览、保留、删除与移动。

用来回答一个问题：**这一堆片子，哪些是真高清，哪些是假 4K。**

<p align="center">
  <a href="https://github.com/tv1314/mediaqc/releases/latest">
    <img alt="下载最新版本" src="https://img.shields.io/badge/%E4%B8%8B%E8%BD%BD%E6%9C%80%E6%96%B0%E7%89%88%E6%9C%AC-fnOS%20%E5%AE%89%E8%A3%85%E5%8C%85-4c8dff?style=for-the-badge&logo=github&logoColor=white">
  </a>
</p>

<p align="center">
  <a href="https://github.com/tv1314/mediaqc/releases"><img alt="release" src="https://img.shields.io/github/v/release/tv1314/mediaqc?style=flat-square&label=release&color=4c8dff"></a>
  <img alt="downloads" src="https://img.shields.io/github/downloads/tv1314/mediaqc/total?style=flat-square&color=3ecf8e">
  <img alt="platform" src="https://img.shields.io/badge/platform-fnOS%201.2%2B-4c8dff?style=flat-square">
  <img alt="license" src="https://img.shields.io/github/license/tv1314/mediaqc?style=flat-square&color=ffb020">
</p>

## 它解决什么

文件名和容器信息经常骗人：

- 标着 `4K` 但码率只有 3 Mbps —— 拉伸出来的
- 标着 `1080p` 实际是低码率二次压制
- 好片子混在大量低质文件里，靠肉眼分辨成本太高

本应用读取真实媒体信息，再抽帧做画面体检（块效应、模糊、噪点），给出综合评分和分级，让你批量处理。

## 功能

| 能力 | 说明 |
|---|---|
| 库扫描 | 递归扫描指定目录，读取分辨率、码率、编码、时长、音轨 |
| 画面体检 | 抽帧分析块效应 / 清晰度 / 噪点，识别低码率拉伸与二次压制 |
| 综合评分 | 分辨率、码率、编码、画面四项加权，输出分级 |
| 预览 | 窗口内直接播放片段，不用切到别的播放器 |
| 批量处理 | 按分级批量保留 / 删除 / 移动到指定目录 |
| 进度可视 | 扫描与体检进度、当前文件、预计剩余时间 |

## 安装

1. 到 [Releases](https://github.com/tv1314/mediaqc/releases/latest) 下载 `mediaqc.fpk`
2. 应用中心 → 手动安装 → 选择该文件
3. 桌面出现「媒体画质管家」图标

要求：fnOS 1.2.0+，x86_64。

## 从源码构建

在 NAS 上执行（需要 `fnpack`）：

```bash
cd mediaqc
rm -rf app/server/__pycache__
rm -f mediaqc.fpk
fnpack build
```

产物：`mediaqc.fpk`

## 目录结构

```
mediaqc/
├── manifest              应用元数据
├── cmd/                  生命周期脚本（含 main 守护）
├── config/               privilege
├── app/
│   ├── server/
│   │   └── server.py     网关 HTTP 服务 + 扫描 / 体检 / 评分
│   ├── www/              前端（原生 JS）
│   └── ui/               桌面入口与图标
└── ICON.PNG  ICON_256.PNG
```

运行时数据（数据库、缩略图、日志）保存在 `/vol1/@appdata/mediaqc/`，不在本仓库内。

## 技术要点

- 后端监听 **Unix socket**，由飞牛统一网关转发并注入登录身份，不占 TCP 端口
- 纯 Python 标准库 + SQLite，无第三方依赖
- 未登录访问 `/api/*` 返回 401

## 许可

MIT
