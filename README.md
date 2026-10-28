# Flow 视频局部修补技能

供 Codex 等能读取文件、执行 Python 的助手使用的本地视频修补技能，当前版本 **v1.5.1**。

v1.5.1 修正 `flow_patch.py` 色彩管线：旧版经 FFmpeg 隐式转换解码会整体压暗约 1 级，并以无标签 BT.601 编码，播放器按 709 解读时整帧偏色，多轮合成会累积。现按源标签（709/601、tv/pc）在脚本内精确换算并写入色彩标签，不支持的矩阵直接拒绝；新增 `tests/color_pipeline_test.py`。发行 ZIP 仍为 v1.5，未重新打包。

v1.5 整理局部修补经验：眼周与镜框、手部保护、生成遮罩与融合遮罩分离、核心完全覆盖、每份返回独立对齐、多人按人选优、接点比较、减少重编码及音轨尾包检查；新增默认待审的视觉验收记录。仅更新文档与记录模板，未改变处理脚本或工具白名单。

经验入口：[局部补丁经验](flow-video-patch/references/patch-lessons.md)。

功能包括制作局部蒙块素材、将生成后的视频局部贴回原片、显式帧映射、多人补丁合成、嘴部保护、固定调色和自动验证。视频生成由使用者在自己的服务账号内完成。

## 使用入口

- [技能目录与操作说明](flow-video-patch/SKILL.md)
- [安装与依赖](flow-video-patch/references/install.md)
- [安全使用与发布条件](flow-video-patch/SECURITY.md)
- [第三方版权与许可证](flow-video-patch/THIRD_PARTY_NOTICES.md)
- [内部使用与转交约定](flow-video-patch/INTERNAL_USE.md)

下载发行包后，保持 `flow-video-patch` 文件夹结构，按接收电脑的技能配置安装；也可让助手直接读取该文件夹内的 `SKILL.md`。

## 分发文件

[下载 v1.5 ZIP](dist/flow-video-patch-v1.5.zip) · [ZIP SHA-256](dist/flow-video-patch-v1.5.zip.sha256)

包内 `SHA256SUMS.json` 记录各文件摘要。第三方工具由每台电脑独立安装；不随包附带运行环境、媒体、模型或真实项目配置。

## 工具白名单

默认只在 [白名单及用途](flow-video-patch/TOOL_POLICY.json)内运行。清单外先停止，按 [审批流程](flow-video-patch/references/tool-policy.md)核查具体版本／构建、许可证、商业用途及安全条件，核查通过且用户明确同意后才能用；不自动安装、换工具或用临时脚本绕行。实际调用另附本地清单，新增工具不自动归入旧版权报告。

这是技能执行规则，没有在接收电脑部署强制沙箱。需要强制禁止任意额外工具时，须配置并验证宿主及操作系统权限控制。公开仓库中的 v1.2–v1.4 历史 ZIP 保留用于追溯，新使用者选 v1.5。

## 验证与安全状态

在技能目录、安装依赖后运行：

```bash
python tests/smoke_test.py
python tests/security_test.py
```

v1.5 的实测结果与验证范围记录在 [VALIDATION.json](flow-video-patch/VALIDATION.json)；历史 Windows 测试有兼容性失败，未因文档更新而解决，接收机器仍需复验。技能限制媒体输入协议、独占创建输出，并包含权限、尺寸预算和超时防护。

**外部运行环境仍须安全更新。** 历史审计中 FFmpeg 8.1 的更新事项继续保留；8.1 分支采用至少 8.1.2 或供应商确认已回补相应安全修复的构建。x264 的公告适用性仍待确认。测试通过不能视为全部依赖安全通过，详细边界见安全说明。[FFmpeg 官方安全公告](https://ffmpeg.org/security.html)

本仓库公开可见；技能文件仍保留原有内部使用约定，没有另行授予 MIT、Apache 等公共开源许可。第三方软件继续适用各自许可证。
