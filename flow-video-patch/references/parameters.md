# 参数与命令

## 统一约定

- JSON UTF-8；`schema_version=1`。`source`和`generated`相对JSON文件所在目录解析，也接受绝对路径。路径带空格时命令行加双引号。
- 从0开始数帧，所有时间区间`[start,end)`含start、不含end。例如`[0,24)`为24帧。
- `fps`支持24、25或`"24000/1001"`等精确字符串。配置必须与源片一致，脚本不自动转帧率。
- `source_size=[宽,高]`；`crop=[x,y,w,h]`在原视频像素中；矩形边界为左上起、右下不含。
- `output_size`/`work_size`为局部画布尺寸。蒙块、贴回matte、protect、fit_exclude在局部画布坐标中。
- 可变几何支持常量向量或`[{"frame":0,"value":[...]}, ...]`，frame始终为**绝对源帧号**，不是局部片段号。关键帧之间线性插值，前后沿用端值；矩形最终四舍五入成整数。突变/遮挡需补关键帧。
- 裁切宽高比须与输出/工作尺寸在0.5%内一致，不加隐式黑边或拉伸。上传蒙块可小于贴回范围，两者职责不同。
- 编码默认libx264（`FLOW_VIDEO_ENCODER` 可选硬件 H.264 编码器，交付须 `FLOW_HW_DELIVERABLE=1`，见[局域网工作机执行](remote-worker.md)）、8位yuv420p，默认CRF17、preset fast，可用环境变量 `FLOW_CRF`（0–51）与 `FLOW_X264_PRESET`（ultrafast…veryslow）调整，实际值写入报告命令；同一任务的候选比较保持同一设置。实际预处理为BGR8。高位深/HDR不走此路线。最终尺寸须是偶数。

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
| `fade_in_frames`／`fade_out_frames` | 分别设首／尾淡变长度，覆盖 `fade_frames` 对应一端；两者之和不超过target帧数 |
| `gain_bgr` / `offset_bgr` | 固定BGR增益/偏移，默认[1,1,1]/[0,0,0]；**不是RGB顺序** |
| `fit_exclude` | 只用于fit：排除未可信的手/乐器/动态背景区域，局部矩形列表 |

`frame_map=[0,1,2,2]`明确重复生成第2帧；`[0,1,null,2]`在第三个目标帧使用原画面。跳过原画面可能闪回旧脸，重复可能停顿，都需审看。生成索引要求不递减；脚本不支持反向播放。生成多余尾帧不被映射就不会使用，需在案例记录说明。

仿射采用`cv2.WARP_INVERSE_MAP`，即对每个源局部像素去生成画布采样。`tx=+2`表示从生成图右侧2像素取样，视觉上把生成内容向左移2像素。超出生成有效边界的贴回支持区会被拒绝；不要用反射边界伪造头发或背景。

保护原嘴时，`protect`必须覆盖所有运动；更优先让`region`本身止于上半脸并有安全距离。保护区为原画面精确保留仅指**编码前**，成片重编码仍存在编码差异。多频段会改变低频颜色，不承诺mask=1区域的每个像素都来自Flow。

## fit_patch

```bash
python scripts/fit_patch.py --config "merge.json" --patch 0 --output "proposed.json"
```

仅对所选patch提出配准与固定调色，其他patch保持不变。默认使用ECC平移模型，以贴回范围外、排除fit_exclude后的像素估计；默认相关性门槛0.8、相对初始平移最多24工作像素。`--motion euclidean`（加旋转）或 `--motion affine`（加缩放、错切）用于返回片整体缩放／转动的情况，2×2 线性部分相对初始值的变化不得超过 `--max-linear-change`（默认0.08），超出即不写建议。可用`--min-correlation`和`--max-shift`调整，但不能靠放宽门槛掩盖错误时序。fit输出中媒体路径变为绝对路径，分享前需清理。

全部映射帧配准通过才输出建议，颜色要有至少3个采样时刻、足够非暗部/高光参照，最后一组采样留出验证；不改善则保留旧颜色参数。动态光线无法靠一组固定参数完全解决。平滑的是空间变换，不是视频帧序。逐帧形变超出上限、局部变形或复杂跟踪时，人工提供affine，或按工具白名单扩展流程核查并获批后使用外部工具计算。

## 命令与报告

```bash
python scripts/flow_patch.py doctor
python scripts/flow_patch.py inspect --source "source.mp4"
python scripts/flow_patch.py prepare --config "prepare.json" --output "upload.mp4" --dry-run
python scripts/flow_patch.py prepare --config "prepare.json" --output "upload.mp4"
python scripts/flow_patch.py merge --config "merge.json" --output "patched.mp4" --dry-run
python scripts/flow_patch.py merge --config "merge.json" --output "patched.mp4"
python scripts/flow_patch.py merge --config "merge.json" --output "check-40-80.mp4" --frames 40:80 --preview
python scripts/flow_patch.py init-merge --prepare-report "upload.report.json" --generated "flow-result.mp4" --output "merge.json" [--matte ellipse X Y W H --feather N] [--name NAME] [--append]
python scripts/flow_patch.py mark-timing --config "merge.json" --patch 0 --basis "怎样核对的" [--evidence FILE]
python scripts/qc_tools.py review-set --record "patched.visual-review.json" --check ID --status pass|fail|not_applicable|not_run [--intervals A:B ...] [--evidence FILE ...] [--reviewer agent|user --confirmation 出处] [--note 文字]
python scripts/qc_tools.py review-status --record "patched.visual-review.json"
```

精简输出：`qc_tools.py` 的所有命令和 `merge`／`prepare --dry-run` 接受 `--brief`（只打印汇总，逐帧列表以 `{"omitted_items": N}` 代替；NOTICE 与错误照常完整打印）和 `--json-out PATH`（把完整结果写入新文件，内容与不带 `--brief` 时的 stdout 相同；文件已存在即拒绝）。智能体默认用 `--brief`，需要逐帧数据时再读 `--json-out` 文件。`fit_patch.py` 写出的建议配置中逐帧仿射与配准记录每帧一行（解析后与完全缩进的写法是同一对象），终端另打印一行变更摘要（配准帧数、最低相关度、相对初始值的最大平移与线性变化、平移范围、调色结论），通常不必打开建议文件。

由 prepare 报告生成 merge 配置（`init-merge`）：从 prepare 报告抄入 source、source_size、fps、crop 轨迹、work_size，target 取 core、generated_start 取 `core_in_upload[0]`，路径写成相对新配置的位置；`--append` 把补丁加到已有配置（源片、尺寸、fps 须相同，名称不能重复）。返回片 fps 不同、宽高比不符或帧数不够覆盖 target 时拒绝并列出选项（显式 frame_map 重复末帧或 null 保留原片、缩短 target、重刷），不自动变速或补帧。两项待定状态写在配置里，换会话也不会丢：

- 不给 `--matte` 时写 `"matte": {"pending": …}`：任何 merge（含 `--frames`／`--preview`）、`fit_patch.py`、`qc_tools.py seam`／`precheck` 都拒绝该补丁；`--dry-run` 照常列出并标 `matte pending`。裁切画布、生成蒙块、融合遮罩是三件事（SKILL.md 原则 11），遮罩不从蒙块推断。
- 写 `"timing": {"status": "unverified", …}`：尺寸、fps、帧数合格不能证明 generated_start 正确（返回片可能足帧但整体偏移）。完整 merge 拒绝；检查用导出与 QC 照常运行，报告 `scope.timing` 标注。核对后用 `mark-timing --basis` 记录依据（可附 `--evidence` 文件，记录其 SHA-256）；该记录绑定返回片 SHA-256 与 target、generated_start、frame_map，任何一项变化即回到 unverified。没有 `timing` 字段的手写配置行为不变。

视觉验收记录（`review-set`／`review-status`）：只能修改模板里已有的检查项；写入前核对成片 SHA-256 与记录一致，不一致即拒绝（记录属于另一个文件）。`pass`／`fail` 至少附一个存在的证据文件（记录相对路径与 SHA-256），`not_applicable` 须 `--note` 写理由；`--reviewer user` 须 `--confirmation` 写明用户确认的出处（引用原话与时间，或用户签名的审看文件），智能体不得代写。整体状态由规则计算、不能手设：任何 fail → `visual_fail`；否则任何 not_run、缺项、无理由的 not_applicable、无确认出处的用户项 → `pending_visual_review`；只有同时满足以下条件才是 `visual_pass`——技术校验为 pass，技术报告是可交付的完整导出（检查用导出永远不能通过），报告、记录与当前成片三方 SHA-256 一致，每个 pass 项的证据文件仍在且未改，`coverage_requirements` 所列检查的审看区间覆盖报告中全部修补区间（接点类覆盖每个补丁首尾前后 0.5 秒）。未满足的条件列在 `blocking`。该命令只管理记录，不代替实际审看。

接缝量化（只读）：`python scripts/qc_tools.py seam --config merge.json --patch 0 [--step 2]` 按合成时相同的对齐、调色、遮罩与淡变，计算过渡带（`w=4a(1−a)>0.2`）内新旧高频差、低频差的加权均值，以及 `w>0.5` 且高频差超过 `--threshold` 的像素数；输出逐帧数值、汇总与最差帧。用于同一镜头候选方案的比较，不代表视觉验收。

只重编码有补丁的片段（仅完整 `merge`）：`merge --copy-unpatched`。

- 适用：源片为 8 位 4:2:0 H.264、时间戳在帧网格上，且容器时间基能精确表示每帧时间。Matroska 的毫秒时间基在 23.976、24、60 fps 下做不到（复制后的时间戳会被取整，拼接校验必然失败），这类源片会在开始前就给出原因并整片渲染；需要此功能时先用下面的 `regrid` 转为 MP4／MOV（普通 `-c copy` 转封装保留毫秒取整，帧率检查不通过）。所有 GOP 都含补丁帧时复制 0 帧、不省时间，会给出 NOTICE。不满足时终端与报告给出 NOTICE，自动整片渲染。
- 做法：按源片关键帧把片子切成 GOP 块；不含补丁帧的块用 FFmpeg 原样复制码流，含补丁帧的块（相邻合并）照常渲染、编码，其 B 帧重排延迟与源片一致；拼接时参数集写入码流、整条流统一写色彩标签，原音轨复制一次。`FLOW_SEGMENTS` 大于 1 时，多个渲染块用多个进程并行。
- 校验：拼接后整片探测（帧数、恒定帧率、零起点），并逐帧比对复制部分与源片的 128 位 murmur3 解码哈希，须全部相同；任一不符或 FFmpeg 切块／拼接失败即自动整片渲染并给出 NOTICE。拼接后的这次严格解码同时完成计时检查、解码错误检查与逐帧哈希，并直接作为成片校验结果复用；源片哈希取自探测时的同一次解码。之后仍做音轨载荷校验。报告 `copy_unpatched` 记录关键帧数、复制与渲染帧数及各块范围。
- 收益：长片配短补丁时只编码补丁所在 GOP；补丁外画面与原片逐比特相同（整片渲染会多一次有损编码）。1080p 2 分钟合成片、补丁 130 帧：整片渲染 98.4 s，本方式 35.6 s；渲染量随源片关键帧间隔增减。
- 已抽查：Windows 上 VLC 3.0.24 与 DaVinci Resolve 21.0.4.5 打开含参数集切换的合成样片（24 fps、240 帧），播放到末帧并随机定位到重编码段与其后的复制段，画面正常（issue #10）。这只是该合成样片的导入、播放与定位检查。
- 注意：输出码流前后来自两个编码器（原片的与 libx264），参数集在码流中切换。FFmpeg 解码已逐帧验证；部分播放器、剪辑软件或硬件解码器对 MP4 中途切换参数集支持不一，交付前在实际使用的软件中打开、拖动并播放接缝前后各段确认。复制部分保留源片码率，文件大小接近源片。

时间戳规整到帧网格（Matroska 毫秒时间基等）：`python scripts/flow_patch.py regrid --source "source.mkv" --output "source-grid.mp4"`。

- 原因：MKV 以毫秒记时间戳，23.976 fps 的帧时间被取整；普通 `ffmpeg -i x.mkv -c copy x.mp4` 照搬这些取整值与毫秒包时长，MP4 的平均帧率变成如 240000/10009，恒定帧率检查拒收（issue #13）。检查不放宽。
- 做法：码流复制（`-copyts` 加 `setts` 码流过滤器），每个视频包的 PTS／DTS 移到最近的 n/fps，包时长设为一帧，时间基设为 1/fps；音轨原样复制。不重编码、不增删帧、不改帧序。`setts` 的含义随 FFmpeg 版本变化（新版本在输入时间基计算表达式、自行取整到目标时间基；6.x／7.0 把结果直接当目标时间基），脚本依次尝试两种写法，只保留通过全部校验的一种，报告 `setts` 记录实际所用写法；已在 FFmpeg 6.1、7.0 与 9.x 验证。
- 前提：源片其余探测条件均满足，且每帧时间离 n/fps 不到四分之一帧（即只是取整误差）；否则拒绝，不做静默重定时。
- 校验：输出严格探测（帧率、帧数、零起点、在帧网格上），逐帧 murmur3 解码哈希与源片全部相同，音轨载荷 SHA-256 相同。报告 `<输出名>.report.json` 记录最大时间戳移动（毫秒，通常不到 1 ms）、是否可用 `--copy-unpatched`、实际命令与两端 SHA-256。
- 影响：画面时间最多移动不到四分之一帧（实际为取整误差），音频时间戳保持不变，音画相对偏移变化不超过源片取整误差；字幕、数据、附件流不带入（终端 NOTICE），多条音轨全部复制。之后的配置以新文件为 `source`，帧号不变。

检查用导出（仅 `merge`）：`--frames START:END` 只导出原片帧 `[START,END)`，配置中的帧号不用改，各帧与完整导出的同号帧来自同一计算；逐帧裁切比例、遮罩与仿射空洞只核对导出区间内的帧（与区间不相交的补丁仍核对其第一帧，遮罩、融合参数格式错误照样拒绝），报告 `scope.per_frame_checks` 注明；完整导出与 `--dry-run`（带不带 `--frames` 都一样）核对全部补丁帧，区间外的问题在完整导出时报出；该段音频重编码为 AAC。`--preview` 改用 libx264 `ultrafast` 编码，画面计算不变、编码更快、文件更大。两者可单用或合用，报告 `scope.deliverable=false`、`visual_review` 标为不可交付，终端输出注明 "check only, not deliverable"；最终交付与视觉验收必须用不带这两个参数的完整导出。检查用导出在输出目录的 `.flow-probe-cache/` 缓存原片与返回片的探测结果（按路径、大小、修改时间与 inode 识别，文件变动即重新探测），正式导出始终完整探测、不读写缓存；该目录可随时删除。`prepare` 不接受这两个参数，上传 Flow 的素材始终完整编码。

dry-run仍读取并检查媒体/参数，不生成视频；与检查用导出共用探测缓存，一般直接用 `--frames … --preview` 代替。完整 `merge` 另生成预填的 `<输出名>.visual-review.json`（自动字段：文件名、哈希、fps、帧数、技术校验、声明的帧映射、解码与音轨两项检查），报告含 `tools`（实际使用的组件、版本与白名单类别）。正式输出拒绝覆盖已存在的视频或同名report；使用临时文件编码并验证后再发布新输出。报告记录配置、实际命令、文件SHA256、尺寸/fps/帧数、完整解码与合成音轨载荷哈希；`visual_review`默认pending，不能当视觉验收通过。临时命令路径属于执行记录，后续复现应使用报告里的配置重新运行。
