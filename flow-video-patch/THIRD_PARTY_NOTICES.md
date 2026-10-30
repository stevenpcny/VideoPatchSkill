# 第三方工具与版权许可说明

适用范围：本包仅包含技能说明、处理脚本、通用参数和合成媒体测试代码。Python、FFmpeg、ffprobe、NumPy、OpenCV及其依赖由各电脑单独安装；本包不分发其源码、运行时、wheel、模型、字体或媒体。没有附第三方库的完整许可证文本，不表示替这些库更改许可证；准确条款以实际安装版本自带的许可文件为准。

## 实际外部依赖

| 组件 | 许可证与主要权利人 | 本技能用途及商业使用 |
|---|---|---|
| CPython与标准库 | 主体Python Software Foundation License Version 2；历史和组件条款另保留。PSF及相关贡献者 | 进程调度、JSON、校验和文件操作；允许商业使用 |
| NumPy | 主体BSD-3-Clause；实际发行包可含0BSD、MIT、Zlib、CC0及其他组件条款。NumPy Developers及相关作者 | 像素运算、插值、遮罩与固定颜色拟合；允许商业使用 |
| OpenCV核心 | 4.5.0及以上核心Apache-2.0；本包兼容范围从4.10起。OpenCV及相关贡献者 | 缩放、仿射、ECC配准及图像金字塔；允许商业使用 |
| opencv-python或headless包装代码 | MIT；wheel另有第三方许可。包装项目作者与贡献者，具体版权名单见安装包 | 提供cv2；只选一种发行包安装，不将整个wheel统称为MIT |
| FFmpeg与ffprobe | 基础许可为LGPL-2.1-or-later，可选GPL组件和构建选项会改变实际构建许可。FFmpeg开发者及贡献者 | 解码、编码、封装与验证；GPL/LGPL均允许商业运行，须核对实际构建 |
| x264／libx264 | 开源GPL-2.0-or-later；另有商业授权路线。x264开发者及相关权利人 | 通过FFmpeg完成H.264编码；仅内部商业运行不自动要求另购x264商业许可 |

完整版权名单、许可证、免责声明和第三方通知以所装版本为准，不以本表替代。

## 内部商业制作与软件再分发

- 这些主体软件许可允许内部商业视频制作，未在本技能依赖清单中引入仅研究或仅非商业模型权重。
- GPL不会仅因工具处理普通自有媒体而要求成片或内部技能自动开源。工具运行、输出作品与工具二进制再分发分别判断。
- 保留合法安装包随附的LICENSE、NOTICE等文件，不删除或改写版权通知。不使用第三方名称或商标暗示官方背书。
- 各电脑单独安装依赖，不把某台电脑的虚拟环境、site-packages或可执行文件复制进技能包。若以后附带库、运行时或第三方源码，须按实际内容重新核对通知、对应源码等分发义务。
- opencv-python的wheel可能带FFmpeg、图像库，非headless Linux wheel还可能带Qt；不同平台和版本的构建不同，不能只凭cv2版本号判断全部许可。
- H.264／AAC专利、原视频音乐肖像等素材权利及Flow服务条款独立适用；本说明不作无侵权或全部专利清查保证。

## 实际使用的技术

Alpha和多频段金字塔融合、ECC平移／欧氏／仿射配准、仿射变换、关键帧插值、固定BGR增益偏移拟合、裁切、遮罩与音轨载荷检查，均通过上述库接口和本包脚本执行；没有额外依赖本地人脸识别、换脸或生成模型。一般数学方法和具体实现代码的版权应分别理解，不声称所有算法不存在专利。

## 目标电脑复核

在本机记录`python --version`、`python -m pip freeze`、`ffmpeg -version`、`ffmpeg -L`、`ffprobe -version`，并保留实际安装包许可文件。`doctor`用于能力检查，不代替完整许可记录。记录仅供本机留存；向他人分享前清除用户名、机器路径及其他识别信息。

## 官方依据

- [Python许可](https://docs.python.org/3/license.html)
- [NumPy许可](https://numpy.org/doc/stable/license.html)
- [OpenCV许可](https://opencv.org/license/)
- [opencv-python许可说明](https://github.com/opencv/opencv-python/blob/master/README.md#licensing)
- [FFmpeg法律与许可](https://ffmpeg.org/legal.html)
- [x264授权说明](https://x264.org/licensing/)

这些是外部依赖说明，不是对第三方软件的重新授权，也不替代实际版本的原始条款。

## 白名单与覆盖范围

运行必须遵守 [工具白名单与审批](references/tool-policy.md)和 [TOOL_POLICY.json](TOOL_POLICY.json)。本表没有为未列出工具、任意新版本／构建、额外模型权重或在线服务提供许可结论。新增组件先停止并核查，核查通过且取得用户明确同意后才使用，并单独保留实际版本的依据。用户同意不能把待核查或不符合目标用途的条款变成核查通过。实际安装包中的间接依赖也须核对，不把主体许可证当作整个构建的统一许可证。
