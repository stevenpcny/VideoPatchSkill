# Flow 视频局部修补技能

供 Codex 等能读取文件、执行 Python 的助手使用的本地视频修补技能，当前版本 **v1.2**。

功能包括制作局部蒙块素材、将生成后的视频局部贴回原片、显式帧映射、多人补丁合成、嘴部保护、固定调色和自动验证。视频生成由使用者在自己的服务账号内完成。

## 使用入口

- [技能目录与操作说明](flow-video-patch/SKILL.md)
- [安装与依赖](flow-video-patch/references/install.md)
- [安全使用与发布条件](flow-video-patch/SECURITY.md)
- [第三方版权与许可证](flow-video-patch/THIRD_PARTY_NOTICES.md)
- [内部使用与转交约定](flow-video-patch/INTERNAL_USE.md)

下载发行包后，保持 `flow-video-patch` 文件夹结构，按接收电脑的技能配置安装；也可让助手直接读取该文件夹内的 `SKILL.md`。

## 分发文件

[下载 v1.2 ZIP](dist/flow-video-patch-v1.2.zip) · [ZIP SHA-256](dist/flow-video-patch-v1.2.zip.sha256)

包内 `SHA256SUMS.json` 记录各文件摘要。第三方工具由每台电脑独立安装；不随包附带运行环境、媒体、模型或真实项目配置。

## 验证与安全状态

在技能目录、安装依赖后运行：

```bash
python tests/smoke_test.py
python tests/security_test.py
```

本次 macOS 验证为 8 项功能测试和 10 项安全回归测试通过；Windows/Linux 需在接收机器复验。技能限制媒体输入协议、独占创建输出，并包含权限、尺寸预算和超时防护。

**外部运行环境仍须安全更新。** 本次检查的 FFmpeg 8.1 尚未更新；8.1 分支采用至少 8.1.2 或供应商确认已回补相应安全修复的构建。x264 的公告适用性仍待确认。测试通过不能视为全部依赖安全通过，详细边界见安全说明。[FFmpeg 官方安全公告](https://ffmpeg.org/security.html)

本仓库公开可见；技能文件仍保留原有内部使用约定，没有另行授予 MIT、Apache 等公共开源许可。第三方软件继续适用各自许可证。
