# 参数与命令

## 统一约定

- JSON UTF-8；`schema_version=1`。`source`和`generated`相对JSON文件所在目录解析，也接受绝对路径。路径带空格时命令行加双引号。
- 从0开始数帧，所有时间区间`[start,end)`含start、不含end。例如`[0,24)`为24帧。
- `fps`支持24、25或`"24000/1001"`等精确字符串。配置必须与源片一致，脚本不自动转帧率。
- `source_size=[宽,高]`；`crop=[x,y,w,h]`在原视频像素中；矩形边界为左上起、右下不含。
- `output_size`/`work_size`为局部画布尺寸。蒙块、贴回matte、protect、fit_exclude在局部画布坐标中。
- 可变几何支持常量向量或`[{"frame":0,"value":[...]}, ...]`，frame始终为**绝对源帧号**，不是局部片段号。关键帧之间线性插值，前后沿用端值；矩形最终四舍五入成整数。突变/遮挡需补关键帧。
- 裁切宽高比须与输出/工作尺寸在0.5%内一致，不加隐式黑边或拉伸。上传蒙块可小于贴回范围，两者职责不同。
- 编码固定libx264、8位yuv420p、CRF17；实际预处理为BGR8。高位深/HDR不走此路线。最终尺寸须是偶数。

## prepare

复制`assets/prepare-template.json`；模板为假设的1280×720原片、局部360×640裁切，缩到180×320，实际素材请全部重测。

| 字段 | 含义 |
|---|---|
| `prepare.core` | 原片中需修补的区间 |
| `context_frames` | 核心两侧最多保留的参照帧数，边界不足时缩短 |
| `crop` | 原片中的人物裁切，固定框或关键帧 |
| `output_size` | 上传输出尺寸，必须为正偶数 |
| `mask_regions` | 一个或多个蒙块矩形，每个都支持关键帧 |
| `color_rgb` | RGB整数0–255，默认黑色 |

报告中`output.frames`为真实上传长度；`output.first/last`是截取原片的区间，`output.core_in_upload`是修补核心在上传视频中的区间。两端有多少实际参照以报告为准。

## merge

复制`assets/merge-template.json`，每个patch表示一个人物的一段补丁。全部patch从同一源片一次合成；多人可复制patch，但同帧crop不能相交；分段patch的target允许首尾相邻。

| 字段 | 含义 |
|---|---|
| `patches[].target` | 需要写入原片的源帧区间 |
| `generated` | Flow下载结果路径 |
| `generated_start` | target首帧对应的生成帧；其后逐帧同序 |
| `frame_map` | 可选，长度等于target长度；每项为生成原始帧索引或null；提供时优先于generated_start |
| `crop` | 对应源片的人物裁切，沿用准备时几何 |
| `work_size` | 局部工作画布，通常等于上传尺寸；生成结果允许等比缩放 |
| `source_to_generated_affine` | 6个数`[a,b,tx,c,d,ty]`，将源局部坐标映射到已缩放生成画布；默认单位变换，支持关键帧 |
| `matte.region` | 贴回区域，在工作坐标中，支持关键帧 |
| `matte.shape` | rect或ellipse |
| `matte.feather` | 向区域内部羽化像素数，0为硬边 |
| `matte.protect` | 强制保留原片的矩形列表，支持关键帧；仅允许alpha融合 |
| `blend` | alpha或multiband |
| `levels` | 多频段金字塔层数1–6，工作宽高须至少`2**levels*2`；默认5 |
| `fade_frames` | 时间淡变，默认0；开启后首尾权重变为0，整脸替换要审查叠影 |
| `gain_bgr` / `offset_bgr` | 固定BGR增益/偏移，默认[1,1,1]/[0,0,0]；**不是RGB顺序** |
| `fit_exclude` | 只用于fit：排除未可信的手/乐器/动态背景区域，局部矩形列表 |

`frame_map=[0,1,2,2]`明确重复生成第2帧；`[0,1,null,2]`在第三个目标帧使用原画面。跳过原画面可能闪回旧脸，重复可能停顿，都需审看。生成索引要求不递减；脚本不支持反向播放。生成多余尾帧不被映射就不会使用，需在案例记录说明。

仿射采用`cv2.WARP_INVERSE_MAP`，即对每个源局部像素去生成画布采样。`tx=+2`表示从生成图右侧2像素取样，视觉上把生成内容向左移2像素。超出生成有效边界的贴回支持区会被拒绝；不要用反射边界伪造头发或背景。

保护原嘴时，`protect`必须覆盖所有运动；更优先让`region`本身止于上半脸并有安全距离。保护区为原画面精确保留仅指**编码前**，成片重编码仍存在编码差异。多频段会改变低频颜色，不承诺mask=1区域的每个像素都来自Flow。

## fit_patch

```bash
python scripts/fit_patch.py --config "merge.json" --patch 0 --output "proposed.json"
```

仅对所选patch提出配准与固定调色，其他patch保持不变。使用ECC平移模型，以贴回范围外、排除fit_exclude后的像素估计；默认相关性门槛0.8、相对初始平移最多24工作像素。可用`--min-correlation`和`--max-shift`调整，但不能靠放宽门槛掩盖错误时序。fit输出中媒体路径变为绝对路径，分享前需清理。

全部映射帧配准通过才输出建议，颜色要有至少3个采样时刻、足够非暗部/高光参照，最后一组采样留出验证；不改善则保留旧颜色参数。动态光线无法靠一组固定参数完全解决。平滑的是空间变换，不是视频帧序。需要缩放、旋转、复杂跟踪时可人工提供affine或在按工具白名单扩展流程核查并获批后使用外部工具计算，不把平移拟合当完整仿射配准。

## 命令与报告

```bash
python scripts/flow_patch.py doctor
python scripts/flow_patch.py inspect --source "source.mp4"
python scripts/flow_patch.py prepare --config "prepare.json" --output "upload.mp4" --dry-run
python scripts/flow_patch.py prepare --config "prepare.json" --output "upload.mp4"
python scripts/flow_patch.py merge --config "merge.json" --output "patched.mp4" --dry-run
python scripts/flow_patch.py merge --config "merge.json" --output "patched.mp4"
```

dry-run仍读取并检查媒体/参数，不生成视频。正式输出拒绝覆盖已存在的视频或同名report；使用临时文件编码并验证后再发布新输出。报告记录配置、实际命令、文件SHA256、尺寸/fps/帧数、完整解码与合成音轨载荷哈希；`visual_review`默认pending，不能当视觉验收通过。临时命令路径属于执行记录，后续复现应使用报告里的配置重新运行。
