# Flow 视频局部修补技能

供 Codex 等能读取文件、执行 Python 的助手使用的本地视频修补技能，当前版本 **v2.6**。

v2.6：新增可选 Rust 编译内核（Python 部分 CPU 约少 35%，输出逐字节相同）、可选硬件编码（NVENC／QSV／AMF，交付前须比较画质）与远程执行脚本 `remote_run.py`。

v2.5：完整导出与 v2.4 逐字节相同、速度约快一倍；新增检查用导出 `merge --frames START:END`／`--preview`（长片局部检查由整片合成约 2 分钟降至数秒）、可选分段多进程合成、交付编码参数、旋转／缩放配准、接缝量化检查（`qc_tools.py seam`）与首尾分设的淡变；修正旧版 FFmpeg 的全范围源片对比度与毫秒时间基 MKV 误判；GitHub Actions 在 Linux／macOS／Windows 运行全部合成测试。各版本完整记录见[版本记录](flow-video-patch/CHANGELOG.md)。

经验入口：[局部补丁经验](flow-video-patch/references/patch-lessons.md)、[身体露肤匹配](flow-video-patch/references/skin-match.md)、[高级合成诊断](flow-video-patch/references/advanced-compositing.md)、[实片经验](flow-video-patch/references/field-lessons.md)、[实片经验二](flow-video-patch/references/hd-relay-lessons.md)。

功能包括制作局部蒙块素材、将生成后的视频局部贴回原片、显式帧映射、多人补丁合成、嘴部保护、固定调色和自动验证。视频生成由使用者在自己的服务账号内完成。

## 使用入口

- [技能目录与操作说明](flow-video-patch/SKILL.md)
- [安装与依赖](flow-video-patch/references/install.md)
- [安全使用与发布条件](flow-video-patch/SECURITY.md)
- [第三方版权与许可证](flow-video-patch/THIRD_PARTY_NOTICES.md)
- [内部使用与转交约定](flow-video-patch/INTERNAL_USE.md)

下载发行包后，保持 `flow-video-patch` 文件夹结构，按接收电脑的技能配置安装；也可让助手直接读取该文件夹内的 `SKILL.md`。

## 分发文件

[下载 v2.6 ZIP](dist/flow-video-patch-v2.6.zip) · [ZIP SHA-256](dist/flow-video-patch-v2.6.zip.sha256)

包内 `SHA256SUMS.json` 记录各文件摘要。第三方工具由每台电脑独立安装；不随包附带运行环境、媒体、模型或真实项目配置。

## 工具白名单

默认只在 [白名单及用途](flow-video-patch/TOOL_POLICY.json)内运行。清单外先停止，按 [审批流程](flow-video-patch/references/tool-policy.md)核查具体版本／构建、许可证、商业用途及安全条件，核查通过且用户明确同意后才能用；不自动安装、换工具或用临时脚本绕行。实际调用另附本地清单，新增工具不自动归入旧版权报告。

这是技能执行规则，没有在接收电脑部署强制沙箱。需要强制禁止任意额外工具时，须配置并验证宿主及操作系统权限控制。公开仓库中的 v1.2–v1.5、v2.0、v2.3–v2.5 历史 ZIP 保留用于追溯，新使用者选 v2.6。

## 验证与安全状态

在技能目录、安装依赖后运行：

```bash
python tests/smoke_test.py
python tests/security_test.py
python tests/qc_tools_test.py
python tests/color_pipeline_test.py
python tests/remote_run_test.py
sh tests/ffmpeg_regression.sh
```

v2.6 的实测结果与验证范围记录在 [VALIDATION.json](flow-video-patch/VALIDATION.json)；每次推送由 GitHub Actions 在 Linux／macOS／Windows 复跑全部合成测试，接收机器仍需复验。技能限制媒体输入协议、独占创建输出，并包含权限、尺寸预算和超时防护。

**外部运行环境仍须安全更新。** 历史审计中 FFmpeg 8.1 的更新事项继续保留；8.1 分支采用至少 8.1.2 或供应商确认已回补相应安全修复的构建。x264 的公告适用性仍待确认。测试通过不能视为全部依赖安全通过，详细边界见安全说明。[FFmpeg 官方安全公告](https://ffmpeg.org/security.html)

本仓库公开可见；技能文件仍保留原有内部使用约定，没有另行授予 MIT、Apache 等公共开源许可。第三方软件继续适用各自许可证。
