# Flow 视频局部修补技能包

版本：1.2。供Codex及其他能读取文件、执行Python命令的AI助手在内部协作中使用。

本包包含技能入口、普通命令行脚本、中文提示词、假设示例、通用参数模板和合成媒体测试代码；不包含真实项目案例、人物资料、媒体、模型、桌面软件或第三方运行时。

## 使用

保持文件夹结构，把本目录放到接收电脑配置的Codex技能目录，或让其他助手先读[AGENT_ENTRY.md](AGENT_ENTRY.md)。具体安装位置按接收环境设置；本包不会自动安装到用户技能目录。

先读[安装说明](references/install.md)，自行安装Python、FFmpeg／ffprobe、NumPy与一种OpenCV Python发行包，然后从本目录运行：

```bash
python scripts/flow_patch.py doctor
python tests/smoke_test.py
```

入口为[SKILL.md](SKILL.md)。本地处理不需要网络；安装依赖或使用外部生成服务需相应网络和账号权限。Flow生成由使用者自行完成。

## 版权与内部转交

本包使用范围见[内部使用说明](INTERNAL_USE.md)。实际调用的第三方工具、许可证类型、商业使用及安装包通知见[第三方版权与许可说明](THIRD_PARTY_NOTICES.md)。第三方工具由各电脑单独安装，不随技能分发。

## 隐私与验证

案例为重新编写的假设示例，模板媒体名和坐标用于演示。不要把实际项目参数、媒体、报告、账号或缓存追加进共享包；运行产生的JSON可能包含真实媒体路径，应另行检查后再分享。

验证结果见[验证说明](references/validation.md)与VALIDATION.json。SHA256SUMS.json核对包内文件，ZIP旁置.sha256核对整个压缩包。仅转交此发行ZIP，不重新压缩已经装入依赖或生成媒体的工作目录。

安全修订版 v1.2：运行和转交前阅读 [安全使用与发布条件](SECURITY.md)。新增复验命令：`python tests/security_test.py`。FFmpeg 8.1 分支须更新至 8.1.2 或采用供应商已回补相关安全补丁的构建；doctor 仅检查能力。
