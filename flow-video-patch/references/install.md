# 安装与迁移

## 依赖

- Python 3.10或以上；优先使用有NumPy/OpenCV预编译轮子的常见稳定版本，如3.11/3.12。
- FFmpeg与ffprobe，FFmpeg需含`libx264`、AAC编码器。
- NumPy与opencv-python。无图形桌面的机器可用同版本范围的`opencv-python-headless`替代，二者不要同时装。
- 运行不需要GPU、模型权重或桌面客户端。高分辨率输出仍会消耗较多CPU、内存和磁盘；脚本按帧流式处理，不把整段高分辨率视频读进内存。

官方下载与说明：[Python](https://www.python.org/downloads/)、[FFmpeg下载页](https://ffmpeg.org/download.html)、[OpenCV pip安装说明](https://docs.opencv.org/5.0/py_tutorials/py_setup/py_pip_install/py_pip_install.html)、[opencv-python](https://pypi.org/project/opencv-python/)、[NumPy](https://numpy.org/install/)。FFmpeg官网提供源码并链接第三方预编译包；Windows/macOS可按官网列出的构建来源选择。不要从不明网盘获取可执行文件。

## Python隔离环境

macOS/Linux，在技能文件夹中执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts\flow_patch.py doctor
```

Windows可始终使用`.venv\Scripts\python.exe`代替文档中的`python`，不必改执行策略。

FFmpeg安装后，把`ffmpeg`和`ffprobe`加入PATH；或分别设置`FLOW_FFMPEG`和`FLOW_FFPROBE`为可执行文件绝对路径。脚本不调用shell，中文和空格路径通过参数传入。

```bash
python scripts/flow_patch.py doctor
python tests/smoke_test.py
```

doctor只检查当前环境，不下载安装。复验成功后可用`python -m pip freeze`记录接收机器实际版本。包内`requirements.txt`为兼容范围，不是覆盖所有平台的锁文件。本次在macOS本地实际执行；Windows/Linux沿用可移植代码，但未在这两类系统实跑。

## 输入规范化

先`inspect`。脚本拒绝HDR、高位深、可变帧率、非方形像素或带旋转的源片，避免隐式改变画面。需要时单独生成SDR/CFR工作副本，明确色调映射、目标fps及对原片影响后再配置；不要仅删除HDR标记或强制8位冒充正确转换。帧率转换改变帧编号，必须重新建立映射。

## 版权与内部转交

Python、FFmpeg／ffprobe、NumPy与OpenCV均由接收电脑单独安装。本技能实际依赖的主体软件许可证允许内部商业视频制作；具体构建和包内第三方条款以安装文件为准。详细说明见[第三方版权与许可](../THIRD_PARTY_NOTICES.md)；技能文件的内部使用范围见[内部使用说明](../INTERNAL_USE.md)。

不把运行环境或wheel重新打包进技能。保留依赖原始许可和通知，安装或升级后记录实际版本与`ffmpeg -L`。仅内部使用不要求为本技能公开选择开源许可证。软件许可不替代素材、生成服务或编码专利权限。

分享时仅发送已检查的发行ZIP，不附实际配置、运行报告、.venv、缓存或媒体。

安全修订版 v1.2：运行和转交前阅读 [安全使用与发布条件](../SECURITY.md)。新增复验命令：`python tests/security_test.py`。FFmpeg 8.1 分支须更新至 8.1.2 或采用供应商已回补相关安全补丁的构建；doctor 仅检查能力。
