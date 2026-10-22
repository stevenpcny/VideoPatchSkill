# Flow 视频局部修补技能

供 Codex 等能读取文件、执行 Python 的助手使用的本地视频修补技能，当前版本 **v1.4**。

v1.4 新增换头后身体露肤匹配经验，以及补丁与原视频的融合性、匹配性必检项。

功能包括制作局部蒙块素材、将生成后的视频局部贴回原片、显式帧映射、多人补丁合成、嘴部保护、固定调色和自动验证。视频生成由使用者在自己的服务账号内完成。

## 使用入口

- [技能目录与操作说明](flow-video-patch/SKILL.md)
- [安装与依赖](flow-video-patch/references/install.md)
- [安全使用与发布条件](flow-video-patch/SECURITY.md)
- [第三方版权与许可证](flow-video-patch/THIRD_PARTY_NOTICES.md)
- [内部使用与转交约定](flow-video-patch/INTERNAL_USE.md)

下载发行包后，保持 `flow-video-patch` 文件夹结构，按接收电脑的技能配置安装；也可让助手直接读取该文件夹内的 `SKILL.md`。

## 分发文件

[下载 v1.4 ZIP](dist/flow-video-patch-v1.4.zip) · [ZIP SHA-256](dist/flow-video-patch-v1.4.zip.sha256)

包内 `SHA256SUMS.json` 记录各文件摘要。第三方工具由每台电脑独立安装；不随包附带运行环境、媒体、模型或真实项目配置。

## 工具白名单

默认只在 [白名单及用途](flow-video-patch/TOOL_POLICY.json)内运行。清单外先停止，按 [审批流程](flow-video-patch/references/tool-policy.md)核查具体版本／构建、许可证、商业用途及安全条件，核查通过且用户明确同意后才能用；不自动安装、换工具或用临时脚本绕行。实际调用另附本地清单，新增工具不自动归入旧版权报告。

这是技能执行规则，没有在接收电脑部署强制沙箱。需要强制禁止任意额外工具时，须配置并验证宿主及操作系统权限控制。公开仓库中的 v1.2、v1.3 历史 ZIP 保留用于追溯，新使用者选 v1.4。

## 验证与安全状态

在技能目录、安装依赖后运行：

```bash
python tests/smoke_test.py
python tests/security_test.py
```

本次 macOS 验证为 8 项功能测试和 10 项安全回归测试通过；Windows/Linux 需在接收机器复验。技能限制媒体输入协议、独占创建输出，并包含权限、尺寸预算和超时防护。

**外部运行环境仍须安全更新。** 本次检查的 FFmpeg 8.1 尚未更新；8.1 分支采用至少 8.1.2 或供应商确认已回补相应安全修复的构建。x264 的公告适用性仍待确认。测试通过不能视为全部依赖安全通过，详细边界见安全说明。[FFmpeg 官方安全公告](https://ffmpeg.org/security.html)

本仓库公开可见；技能文件仍保留原有内部使用约定，没有另行授予 MIT、Apache 等公共开源许可。第三方软件继续适用各自许可证。
