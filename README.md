# Flow 视频局部修补技能

供 Codex 等能读取文件、执行 Python 的助手使用的本地视频修补技能，当前版本 **v2.4**。

v2.0 合并此前两条并行发版线：仓库线（v1.4 身体露肤、v1.5 局部补丁经验、v1.5.1 色彩修复）与另一份 v1.3 起分出的发行线（v1.4 视觉验收流程与记录、v1.5 高级合成诊断与 FFmpeg 合成回归、v1.6 只读核对脚本 `qc_tools.py`、v1.7 实片经验二、v1.8 色彩修复）。两线 v1.4、v1.5 编号指不同内容，包内文档按线别标注。v2.0 不新增处理能力或工具授权。

v2.4 处理提速与局域网工作机：`flow_patch.py` 逐帧色彩转换多线程、解码预读与编码写出后台线程、`merge` 只转换补丁区域（补丁外亮度在编码前与原片解码值一致），新增 `FLOW_ENCODE_THREADS`／`FLOW_PROCESS_THREADS` 环境变量；合成 4K 测试在 24 核 Windows 机上由 128 s 降至约 55 s。新增[局域网工作机执行](flow-video-patch/references/remote-worker.md)（含后续可改进方向）。不新增工具、依赖或编码器授权。

v2.1–v2.3 只增文档、不改处理脚本和测试：v2.1 加入多人交界与局部借用经验、可编辑达芬奇工程交付指南；v2.2 加入返回片贴合度预检与接缝选位、整人贴回路线、像素级复刻提示词；v2.3 补充整人贴回遮罩经验（衣服遮罩按行填满、底部接缝避开手肘、并集边界离开原片轮廓）与"高清竖条贴回静态底图"（固定形变校正、矩形贴回）。

v1.5.1 修正 `flow_patch.py` 色彩管线：旧版经 FFmpeg 隐式转换解码会整体压暗约 1 级，并以无标签 BT.601 编码，播放器按 709 解读时整帧偏色，多轮合成会累积。现按源标签（709/601、tv/pc）在脚本内精确换算并写入色彩标签，不支持的矩阵直接拒绝；新增 `tests/color_pipeline_test.py`。

v1.5 整理局部修补经验：眼周与镜框、手部保护、生成遮罩与融合遮罩分离、核心完全覆盖、每份返回独立对齐、多人按人选优、接点比较、减少重编码及音轨尾包检查；新增默认待审的视觉验收记录。仅更新文档与记录模板，未改变处理脚本或工具白名单。

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

[下载 v2.4 ZIP](dist/flow-video-patch-v2.4.zip) · [ZIP SHA-256](dist/flow-video-patch-v2.4.zip.sha256)

包内 `SHA256SUMS.json` 记录各文件摘要。第三方工具由每台电脑独立安装；不随包附带运行环境、媒体、模型或真实项目配置。

## 工具白名单

默认只在 [白名单及用途](flow-video-patch/TOOL_POLICY.json)内运行。清单外先停止，按 [审批流程](flow-video-patch/references/tool-policy.md)核查具体版本／构建、许可证、商业用途及安全条件，核查通过且用户明确同意后才能用；不自动安装、换工具或用临时脚本绕行。实际调用另附本地清单，新增工具不自动归入旧版权报告。

这是技能执行规则，没有在接收电脑部署强制沙箱。需要强制禁止任意额外工具时，须配置并验证宿主及操作系统权限控制。公开仓库中的 v1.2–v1.5 及 v2.0 历史 ZIP 保留用于追溯，新使用者选 v2.4。

## 验证与安全状态

在技能目录、安装依赖后运行：

```bash
python tests/smoke_test.py
python tests/security_test.py
python tests/qc_tools_test.py
python tests/color_pipeline_test.py
sh tests/ffmpeg_regression.sh
```

v2.4 的实测结果与验证范围记录在 [VALIDATION.json](flow-video-patch/VALIDATION.json)；历史 Windows 测试有兼容性失败，未因文档更新而解决，接收机器仍需复验。技能限制媒体输入协议、独占创建输出，并包含权限、尺寸预算和超时防护。

**外部运行环境仍须安全更新。** 历史审计中 FFmpeg 8.1 的更新事项继续保留；8.1 分支采用至少 8.1.2 或供应商确认已回补相应安全修复的构建。x264 的公告适用性仍待确认。测试通过不能视为全部依赖安全通过，详细边界见安全说明。[FFmpeg 官方安全公告](https://ffmpeg.org/security.html)

本仓库公开可见；技能文件仍保留原有内部使用约定，没有另行授予 MIT、Apache 等公共开源许可。第三方软件继续适用各自许可证。
