# Flow 视频局部修补技能包

版本：2.0。供Codex及其他能读取文件、执行Python命令的AI助手在内部协作中使用。

本包包含技能入口、普通命令行脚本、中文提示词、假设示例、通用参数模板和合成媒体测试代码；不包含真实项目案例、人物资料、媒体、模型、桌面软件或第三方运行时。

## 使用

保持文件夹结构，把本目录放到接收电脑配置的Codex技能目录，或让其他助手先读[AGENT_ENTRY.md](AGENT_ENTRY.md)。具体安装位置按接收环境设置；本包不会自动安装到用户技能目录。

先读[安装说明](references/install.md)，自行安装Python、FFmpeg／ffprobe、NumPy与一种OpenCV Python发行包，然后从本目录运行：

```bash
python scripts/flow_patch.py doctor
python tests/smoke_test.py
python tests/security_test.py
python tests/qc_tools_test.py
python tests/color_pipeline_test.py
sh tests/ffmpeg_regression.sh
```

入口为[SKILL.md](SKILL.md)。本地处理不需要网络；安装依赖或使用外部生成服务需相应网络和账号权限。Flow生成由使用者自行完成。

## 版权与内部转交

本包使用范围见[内部使用说明](INTERNAL_USE.md)。实际调用的第三方工具、许可证类型、商业使用及安装包通知见[第三方版权与许可说明](THIRD_PARTY_NOTICES.md)。第三方工具由各电脑单独安装，不随技能分发。

## 隐私与验证

案例为重新编写的假设示例，模板媒体名和坐标用于演示。不要把实际项目参数、媒体、报告、账号或缓存追加进共享包；运行产生的JSON可能包含真实媒体路径，应另行检查后再分享。

验证结果见[验证说明](references/validation.md)与VALIDATION.json。SHA256SUMS.json核对包内文件，ZIP旁置.sha256核对整个压缩包。仅转交此发行ZIP，不重新压缩已经装入依赖或生成媒体的工作目录。

v2.0 合流：本包此前分成两条线各自发版——仓库线 v1.4（身体露肤）、v1.5（局部补丁经验）、v1.5.1（色彩修复），本地线 v1.4（视觉验收）、v1.5（高级合成）、v1.6（qc_tools）、v1.7（实片经验二）、v1.8（色彩修复，与 v1.5.1 相同）。两线的 v1.4、v1.5 编号指不同内容，下文按线别标注。v2.0 合并两线全部文档、脚本与测试，不新增处理能力或工具授权；SKILL.md 原本地线原则 10–18 重编号为 12–20，视觉验收模板以本地线结构为准并补入仓库线字段与检查项。

本地线 v1.8（仓库线同内容为 v1.5.1）修正 flow_patch.py 色彩管线：旧版经 FFmpeg 隐式转换解码会整体压暗约 1 级，并以无标签 BT.601 编码，播放器按 709 解读时整帧偏色，多轮合成会累积。现按源标签（709/601、tv/pc）在脚本内精确换算，用码流元数据写入色彩标签（不再触发隐式转换），不支持的矩阵直接拒绝。新增 `python tests/color_pipeline_test.py`（8 项）。不新增工具或授权。

本地线 v1.6 新增[实片经验](references/field-lessons.md)与只读核对脚本 `scripts/qc_tools.py`（残留遮罩色、整体帧偏移与残余位移、模板粗跟踪、成片尺度清晰度、时间切片地标图），复验 `python tests/qc_tools_test.py`。两个主处理脚本不变；qc_tools 不写媒体、不联网、不调用清单外组件，结果仅作判断证据。

本地线 v1.5 新增[高级合成诊断](references/advanced-compositing.md)和`sh tests/ffmpeg_regression.sh`六组合成回归：时序压缩、实际alpha、头周暗圈、残差溢出、播放器颜色和同场景清晰度匹配。两个主处理脚本及原有测试不变，不新增自动追踪、10bit处理或自动修复能力，不扩大编码器授权。实片仍须独立视觉验收。

本地线 v1.4 加强实际成片的视觉验收：[接缝/重影/肤色检查](references/verification.md)及[视觉验收记录模板](assets/visual-review-template.json)。要求全修补帧巡检、原像素与放大边缘/遮罩检查、接点逐帧及正常速度播放；技术通过不自动代表视觉通过。补充工作裁切与贴回遮罩区别、保留背景纹理、脸/脖子/手肤色协调及独立补丁贴回验证。此次只更新指南和模板，不新增媒体运行能力，不宣称自动检测能保证任何镜头绝对无缝。

安全修订版 v1.2：运行和转交前阅读 [安全使用与发布条件](SECURITY.md)。新增复验命令：`python tests/security_test.py`。FFmpeg 8.1 分支须更新至 8.1.2 或采用供应商已回补相关安全补丁的构建；doctor 仅检查能力。

## 仓库线 v1.5 局部补丁经验与验收

新增 [局部补丁经验](references/patch-lessons.md)与 [视觉验收记录](assets/visual-review-template.json)。重点包括修补范围、眼镜与双手保护、覆盖核心和过渡区、每份返回独立映射、多人选优、分段接续、颜色与编码、音轨尾包及独立补丁贴回说明。视觉状态默认待审，技术通过和实际播放验收分别记录。本版仅更新说明和模板，处理脚本、测试代码、依赖及工具白名单保持不变。

## 仓库线 v1.4 融合与匹配

新增 [身体露肤匹配](references/skin-match.md)：换头后脖子、手等身体露肤的处理经验，要点是以接缝处的实际改动量为调色目标、已生成的像素不再调色、排除衣服和头部。[检查说明](references/verification.md) 新增必做的"融合性与匹配性检查"：补丁必须和原视频比对接缝颜色、肤色一致、光照质感、运动、时间稳定和遮挡层次。本版仅更新文档，处理脚本和测试与 v1.3 字节一致。

## 白名单执行规则

v1.3 新增 [工具白名单与审批](references/tool-policy.md)、[机器可读清单](TOOL_POLICY.json)及[本地核查记录模板](assets/tool-review-template.json)。白名单内也须核实实际版本、构建、许可证与安全条件；清单外停止，核查通过并经用户明确同意后才能使用。不能自动换工具、安装依赖或用临时脚本绕行。交付附实际工具使用清单。

本版只约束智能体执行流程，未在接收电脑部署强制沙箱。原有 FFmpeg 更新要求及 x264 待确认项继续有效；本版没有宣称解决这些依赖风险。
