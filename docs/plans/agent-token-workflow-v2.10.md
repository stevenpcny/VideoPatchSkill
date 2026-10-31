# 智能体 token 消耗与流程精简 PLAN（v2.10 候选）

状态：**供上游评审的提案。本文没有修改技能包、处理脚本、默认参数、工具白名单或发行包；下文的节省数字除标明"实测"的以外都是估算，必须按第 8 节的协议实测印证。**

核查日期：2026-10-31。

**修订 1（同日）**：纳入[业主复验反馈](https://github.com/stevenpcny/VideoPatchSkill/pull/21#issuecomment-6545163483)——E4、C1、T4／C2、E2 补充阻断条件与负例；新增可直接运行的 fixture 脚本 `tools/token_fixture.py`，证据文件改为记录提交、环境、命令、stdout／stderr 分列与产物哈希；首批实施改为 T1／T2 加带阻断条件的 C1／E4。逐条处理见第 11 节。

| 对象 | 提交 | 用途 |
|---|---|---|
| **main（评估对象与合并基线）** | `8915d8620842ac84fad5723f2d543ff3cf661351`（[#19](https://github.com/stevenpcny/VideoPatchSkill/pull/19)、[#20](https://github.com/stevenpcny/VideoPatchSkill/pull/20) 已合并） | 修订 2 起的基线；本 PR 已 rebase 到此 |
| 首次测量 | `8b7525d07e669f52d9f31840e15bd99a4048c0c6`（#20 合并前的 head） | 第一版与修订 1 的测量对象 |
| [PR #19](https://github.com/stevenpcny/VideoPatchSkill/pull/19) | 性能与质量 PLAN（已合并） | 本文不重复其计算性能项；本文把 #19 的 P6"工作流维护：减少手工重复工作"展开成可执行方案 |

**修订 2（#20 合并后复测）**：main 相对 `8b7525d` 只改了 CHANGELOG、`flow_patch.py`（撤回零复制短路 53bd1c6）与 smoke 测试，技能阅读路径不变。在 main 上重跑：文档估算三场景仍为 37,783／26,498／54,240；fixture 的全部媒体产物（源片、上传片、检查片、成片）SHA-256 与 `8b7525d` 相同，precheck、seam 的 stdout 逐字节相同，JSON 报告与 dry-run 输出只差在其中绝对输出目录路径的长度。下文数字因此不变，两次完整记录都在证据文件中（`fixture_record`、`fixture_record_main`）。

#19／#20 优化的是**机器时间**（解码次数、转换面积、读取器）。本文优化的是另一种成本：**智能体读文档、读输出、写临时代码、手填 JSON、重建状态所花的 token，以及人和智能体之间的来回轮次**。两者互不替代。

---

## 1. 结论摘要

1. 智能体按现行阅读规则完成"首台机器、单人、单返回片"的任务，**光读文档约 3.8 万 token**；同机后续任务约 2.6 万；多人交界加达芬奇工程约 5.4 万（第 2.1 节，静态估算）。`SKILL.md` 本身约 6.7 千，且每轮都在上下文里。
2. 文档之外最大的 token 去向（按估算从大到小；命令输出与记录体量已由业主在 Windows 复现）：
   - **临时辅助代码**：时间映射轨迹法、全修补帧联系图、放大边缘图、遮罩叠色、补丁外差值、眼部状态表、接缝 LAB 取样，目前都由智能体每个任务现写。这部分是**输出 token**，单价通常是输入的数倍，还会带来调试轮次。
   - **冗长命令输出**：`qc_tools.py seam` 每帧一行，约 165 B/帧（96 帧 15.9 KB，实测）；`fit_patch.py` 写出的 `proposed.json` 达 46.9 KB（实测，96 帧逐帧仿射、缩进 2），智能体一打开就是约 1.5 万 token。
   - **手工转录与手填 JSON**：prepare 报告 → merge 配置要手抄 crop、work_size、fps、source_size、target、generated_start；视觉验收记录 25 项中只有 2 项自动预填（实测），其余 23 项手写。
   - **状态重建**：多轮修补中上下文压缩或换会话后，智能体要重读 SKILL.md、配置和报告才能知道"做到哪一步、哪些检查已作废"。
3. 提案共 16 项，分四类：文档路由（D）、只读工具输出（T）、新增证据与记录工具（E）、配置能力（C），外加 4 条操作约定（P）。**全部不放宽白名单、不放宽"状态如实报告"、不把自动证据升级为视觉通过、不改变已有配置的合成输出**。少数涉及阅读规则或审批边界的项单独列为待上游决定（第 10 节）。
4. 粗估合计：同类任务的非图片 token 有望下降约 60–70%，其中输出 token 下降比例更高；图片审看量由验收规则决定，本提案不减少应看的帧，只减少重复和无效的看图。**这是目标估算，须按第 8 节实测；合成复验不能证明真实会话收益或实片视觉质量。**
5. 首批实施（修订 1）：T1／T2（冗长输出）与带阻断条件的 C1／E4（待配置状态、验收门槛写进文件）。其依赖"#20 合并后的 main"已满足（修订 2）。

---

## 2. 现状测量

### 2.1 文档阅读路径（静态估算）

可复验：`python tools/token_budget.py [--json out.json]`（本 PR 新增，只读、仅标准库）。原始数字：[agent-token-baseline.json](evidence/agent-token-baseline.json)。

估算法：每个 CJK 字符（含全角标点）记 1 token，其余字符每 3.2 个记 1 token。只用于同一仓库改动前后对比，不是计费数字；绝对值要以宿主用量日志为准。

| 文件 | 估算 token | 现行阅读规则 |
|---|---:|---|
| SKILL.md | 6,738 | 每次 |
| AGENT_ENTRY.md | 587 | 跨助手入口，每次 |
| references/verification.md | 4,774 | 每个任务第一次合成前 |
| references/parameters.md | 4,402 | 制作参数或调用脚本时 |
| references/remote-worker.md | 4,154 | 有本地工作机档案时 |
| references/hd-relay-lessons.md | 4,101 | 9 类触发条件之一 |
| references/return-fit-assessment.md | 3,787 | 收到任何返回片 |
| references/overlap-and-local-donors.md | 3,230 | 多人交界／局部借用 |
| references/validation.md | 3,082 | （维护文档） |
| references/patch-lessons.md | 2,817 | 保护、遮罩、候选、分段、清晰度 |
| references/resolve-project-handoff.md | 2,819 | 要求达芬奇工程 |
| references/workflow.md | 2,724 | 首次准备或长片分段 |
| references/advanced-compositing.md | 2,604 | 变速、透旧脸、暗圈等 |
| references/tool-policy.md + TOOL_POLICY.json | 2,181 + 1,692 | 首次使用／版本变化 |
| SECURITY.md | 1,871 | "发布或首次运行前" |
| references/field-lessons.md | 1,910 | **收到 Flow 返回片**（与 return-fit 同一触发） |
| assets/visual-review-template.json | 2,126 | 按模板记录 |
| VALIDATION.json / CHANGELOG.md | 13,741 / 7,076 | 无规则要求，但在包内，检索时可能被读到 |

| 场景 | 文件数 | 估算 token |
|---|---:|---:|
| 首台机器，单人单返回片 | 13 | **37,783** |
| 同机后续任务，同版本 | 8 | **26,498** |
| 多人交界 + 露肤 + 达芬奇工程 | 20 | **54,240** |

观察：

- **整文件阅读**：路由只指到文件，不指到节。例如"高清竖条贴回静态底图"只需 hd-relay-lessons 第 13 节，却要读完 4.1 千 token。
- **重叠触发**："收到 Flow 返回片"同时触发 field-lessons 和 return-fit-assessment；"接缝"同时指向 cases、patch-lessons、return-fit。
- **SKILL.md 原则与参考文档重复**：原则 16–17 复述 field-lessons，18–20、28 复述 hd-relay-lessons，24–27 复述 return-fit-assessment，21–23 复述 overlap-and-local-donors。SKILL.md 每轮常驻，重复部分每轮都付费（有缓存时按缓存读计费，压缩后重读时全价）。
- **常驻部分偏大**：SKILL.md 的"入口与范围"有 15 条触发、"实施原则"28 条（其中第 12 条已空置）。智能体每个任务都要在这 6.7 千 token 中自己判断哪些适用。

### 2.2 命令输出与产物（合成片实测）

可复验：`python tools/token_fixture.py --out 新空目录 --package 被测的flow-video-patch目录 --json record.json`。脚本生成合成素材、写出完整的 prepare／merge 配置（含 matte、源片编码参数）、依次运行全部命令，逐条记录 argv、退出码、耗时、stdout 与 stderr 分列的字节数和 SHA-256、全部产物的字节数和 SHA-256，以及被测提交、包清单摘要、Python／NumPy／OpenCV／FFmpeg 版本、平台和全部 `FLOW_*` 环境变量。完整记录在[证据文件](evidence/agent-token-baseline.json)的 `fixture_record`。

本机环境：Linux 容器，Python 3.13.18，NumPy 2.5.3，opencv-python-headless 5.0.0，FFmpeg 6.1.1，NumPy 内核，被测提交 `8b7525d`，并在 main `8915d86` 复测（媒体产物与 QC 输出相同）。素材：testsrc2 1280×720、24 fps、240 帧加正弦 AAC（libx264 medium CRF 18，GOP 48）；prepare 核心 `[48,144)`、参照 24 帧、裁切 360×640，把上传片当作自身返回片（恒等），matte 椭圆 `[60,40,240,300]`、feather 20。业主在 Windows（FFmpeg 9.0.2，matte 矩形 `[80,100,180,250]`）的复验列在最后一栏；配置和平台不同，不要求字节数一致。

| 命令 | stdout 字节（fixture） | 约合 token | 业主复验 | 备注 |
|---|---:|---:|---:|---|
| `doctor` | 753 | 235 | | |
| `inspect` | 450 | 140 | | 每个文件一次 |
| `prepare --dry-run` | 1,293 | 404 | | 回显完整配置 |
| `merge --dry-run` | 3,435 | 1,073 | | 回显配置 + 源信息 + 计划 |
| `qc_tools.py precheck` | 3,703 | 1,157 | 3,701 | 8 个样本逐条列出 |
| `qc_tools.py seam` | **15,872** | **4,960** | 15,876 | 每帧一行，随 target 长度线性增长：240 帧约 1.2 万 token，1000 帧约 5 万 |
| `fit_patch.py` | 40 | 13 | | 只打印路径，智能体要知道改了什么就得打开文件 |
| `merge --frames --preview` / 完整 `merge` | 88 / 141 | ~35 | | 已很简洁 |

全部命令 stderr 为 0 字节、退出码 0。

| 产物 | 字节（fixture） | 约合 token | 业主复验 | 备注 |
|---|---:|---:|---:|---|
| `proposed.json`（fit 输出） | **46,815** | **14,630** | 49,426 | 逐帧关键帧仿射，`indent=2`；业主用标准库紧凑序列化后 21,504 字节（−56.5%），解析对象不变 |
| `*.report.json` | 3.5–4.2 K | 1,100–1,300 | | 含 `tools`、`command_argv` |
| `*.visual-review.json` | 7,685 | 2,400 | 7,960 | 25 项检查，预填 pass 2 项、not_run 23 项（两边一致） |

### 2.3 智能体仍在手工完成的环节（源码与文档核对）

| ID | 环节 | 依据 | token 形态 |
|---|---|---|---|
| M1 | prepare → merge 手抄 crop 轨迹、work_size、fps、source_size，target 取 core，generated_start 取 `core_in_upload[0]` | workflow.md 第 4 节："复制 merge-template，从对应 prepare 复制 crop 轨迹和工作尺寸" | 读两份 JSON + 写一份；抄错要重跑 |
| M2 | 时间映射：头部模板跟踪 + `上传帧 = k·返回帧 + 偏移` 搜索 + ±1 帧确认 | return-fit 第 1 节第 3 项；precheck 的 `manual_checks_remaining` 仍列"time mapping against the upload (qc_tools offset)"；`offset` 需手选 `--region` | 每个任务现写 |
| M3 | 全修补帧联系图、100%／200% 边缘图、黑白遮罩与叠色、接点窗口逐帧图 | verification.md 第 2–4 节 | 每个任务现写，且多轮重做 |
| M4 | 补丁外区域与原片差值（原则 9"应接近 0"） | SKILL.md 原则 9；verification.md 融合性检查第 7 项 | 现写 |
| M5 | 接缝颜色 LAB 取样、肤色比较、过渡带亮度逐帧曲线（闪变） | verification.md 融合性检查第 1、5 项；[#18](https://github.com/stevenpcny/VideoPatchSkill/issues/18) 报告的分段切换背景闪变也属此类 | 现写 |
| M6 | 左右眼逐帧状态表的取图 | SKILL.md 原则 18；return-fit 第 1 节第 7 项 | 现写取图，判断必须人工 |
| M7 | 视觉验收记录 23 项手填，状态、区间、证据路径都手写 JSON | assets/visual-review-template.json | 输出 token；容易把旧哈希的 pass 留在新文件上 |
| M8 | 逐帧形状遮罩：包装脚本替换主脚本 `matte()` 后调用 `main()` | return-fit 第 5 节；属"清单外辅助代码"，每次先取得用户批准 | 现写 + 一次审批往返 |
| M9 | 首次使用核对工具版本是否与已有本机核查记录一致 | tool-policy.md"运行前必须做"第 2、4 条 | 读 3.9 千 token 文档后人工比对 |
| M10 | 上下文压缩或换会话后，重新弄清任务进度和哪些检查已作废 | SKILL.md 完成标准："修改遮罩、时序、调色或编码后，复核受影响项目" | 重读 SKILL.md、配置、报告 |

---

## 3. token 去向分析

| 类别 | 计价性质 | 主要来源 | 本提案对应项 |
|---|---|---|---|
| A 常驻与整文件阅读 | 输入；常驻部分每轮都计（缓存读或全价） | 2.1 节 | D1–D5 |
| B 冗长输出 | 输入；进入上下文后每轮都计 | 2.2 节 seam、proposed.json、dry-run 回显 | T1–T2 |
| C 临时辅助代码 | **输出**；另有调试轮次 | M2–M6、M8 | E1–E3、C2 |
| D 手工转录与手填记录 | 输出 + 返工 | M1、M7 | C1、E4 |
| E 状态重建 | 输入；压缩后全价 | M9、M10 | T3、D4 |
| F 图片审看 | 输入（图片） | 验收规则要求的联系图、放大图 | P2（只减重复和无效看图，不减应看帧） |
| G 人机往返 | 轮次（每轮都重发上下文） | 逐项确认、逐项审批 | P1、P2、C2 |

---

## 4. 不能削弱的质量合同

所有提案都须满足以下条件，验收时逐条核对：

1. **白名单不变**：新工具只用 Python 标准库、NumPy、一种 OpenCV 发行包、FFmpeg／ffprobe，属于包内脚本；不引入新依赖、模型或在线服务。清单外停止规则原文保留。
2. **状态如实**：素材准备、生成、时间对齐、空间配准、合成、自动校验、视觉审看仍是不同状态。新工具生成的证据一律保持 `not_run`，不自动写 `pass`（已有的解码、音轨两项自动预填除外）。
3. **视觉验收绑定最终文件**：`visual_pass` 仍要求全部适用项实际完成；记录绑定最终文件 SHA-256，哈希变化后旧 pass 作废。
4. **检查用导出永远不可交付**：`--frames`／`--preview` 语义不变。
5. **已有配置的合成输出逐字节不变**：D、T、E 类改动不碰渲染路径；C 类只新增可选字段或新命令，旧配置在同一工具构建下的成片 SHA-256 必须不变（沿用 #19 的 A 类判据）。
6. **不减少应看的帧**：自动排序、自动挑选只决定"先看哪里"，不替代"全部修补帧巡检"和"正常速度播放"。
7. **规则不丢**：文档重构前后建立规则保全表（第 5.1 节 D1），CI 检查每条规则 ID 仍有落点。
8. **隐私不变**：新工具输出不写入用户姓名、原文件名、账号；绝对路径只在任务目录内的报告中出现，与现行规则一致。

---

## 5. 提案明细

每项写明：问题、方案、预期节省（估算）、质量影响分类、验收标准。分类沿用 #19：**A 类** = 结果保持（媒体输出逐字节不变）；**D 类** = 仅文档；**N 类** = 新增能力，不影响已有配置。

### 5.1 文档路由（D）

#### D1　SKILL.md 重构为"硬规则 + 阶段路由表"，目标 ≤ 2,500 token

- **问题**：SKILL.md 6.7 千 token 每轮常驻；28 条原则中大半是专题经验，与参考文档重复（2.1 节）。
- **方案**：
  1. SKILL.md 只保留：frontmatter；**硬规则**（白名单与清单外停止、状态如实、对齐先于调色、`[start,end)` 与坐标约定、接缝不放在脸上、检查用导出不可交付、视觉验收判定、隐私）；**阶段路由表**（下例）；完成标准一段。
  2. 原则 1–28 原文迁入 `references/principles.md`，按主题分节（眼部、整头、遮罩三分法、颜色、清晰度、分段、多层重叠、整人贴回），**保留原编号**，旧链接不失效。
  3. 每条硬规则和原则编号加稳定 ID（如 `R-WL`、`P-11`），新增 `tests/doc_rules_test.py`：从 `docs/plans/rule-map.json`（规则 ID → 文件#锚点 → 关键短语）检查每个 ID 都能找到、关键短语仍在。
- **路由表示例**（每格写"文件#节"和估算 token，让智能体按需取节）：

  | 阶段 | 必读 | 按症状加读 |
  |---|---|---|
  | 0 新机器／版本变化 | `doctor --policy`（T3）的结论；有变化才读 tool-policy 全文 | 依赖缺失 → install |
  | 1 定目标、做素材 | workflow#1–2、parameters#prepare | 眼部 → principles#眼部；跟踪裁切 → hd-relay#跟踪 |
  | 2 写提示词 | prompts 中**一个**分支 | |
  | 3 收到返回片 | `precheck` 输出；return-fit#1 摘要 | 缺帧／残色 → field-lessons#认段；变速 → advanced-compositing#变速 |
  | 4 调参 | parameters#merge 表 | 接缝 → return-fit#2；露肤 → skin-match；多人 → overlap |
  | 5 验收交付 | verification 检查卡（D3） | 某项不过 → verification 对应节全文 |
  | 专题 | — | 达芬奇工程、局域网工作机、编译内核 |

- **预期节省**：常驻部分 6.7 千 → ≤ 2.5 千 token；单任务首读约省 4 千，长会话中每轮常驻成本同比下降。
- **分类**：D。
- **验收**：规则保全表 100% 覆盖；`doc_rules_test.py` 进 CI；两名评审抽查 10 个典型问题能在路由表中两跳内找到原文。

#### D2　参考文档节级锚点与"本文导读"

- **问题**：整文件阅读（2.1 节）。
- **方案**：每份 references 文件开头加 5–10 行"导读"：各节一句话 + 估算 token + 何时读；各节标题加稳定锚点。路由表、SKILL.md、AGENT_ENTRY.md 只链接到节。
- **预期节省**：典型任务的参考文档读取量估计减少 40–60%（由整文件变为 1–3 节）。
- **分类**：D。
- **验收**：`doc_rules_test.py` 同时检查所有 `文件#锚点` 链接可解析；`tools/token_budget.py` 改为按节计算，前后数字写入证据文件。

#### D3　verification.md 前置"检查卡"

- **问题**：每个任务第一次合成前读全文 4.8 千 token；实际执行时智能体要的是"25 个检查项各自的通过标准"。
- **方案**：verification.md 开头加检查卡：模板中每个 check id 一行（什么算通过、最少覆盖范围、对应全文节）。全文仍是权威，任何一项不通过或有歧义时读对应节。CI 检查卡中的 id 集合与 `assets/visual-review-template.json` 完全一致。
- **阅读规则调整（待上游决定，Q1）**：首台机器／包版本变化时读全文；同机同版本的后续任务读检查卡即可。
- **预期节省**：后续任务约省 3.8 千 token／任务。
- **分类**：D（阅读规则调整需上游批准）。
- **验收**：id 一致性测试；检查卡不出现全文没有的标准。

#### D4　AGENT_ENTRY.md 与 SKILL.md 去重

- **问题**：AGENT_ENTRY 的 5 条硬规则与 SKILL.md 重复，两份都读（587 token）；维护时两处要同步。
- **方案**：AGENT_ENTRY.md 缩为指针 + 一句"无终端时只给方案"，硬规则以 SKILL.md 为准（Q5：确认 Codex 等宿主不依赖其完整文字）。
- **分类**：D。

#### D5　标明维护文档

- **问题**：VALIDATION.json（13.7 千 token）、CHANGELOG.md（7.1 千）、references/validation.md（3.1 千）在包内，智能体检索包时可能整份读入。
- **方案**：SKILL.md 路由表末行写明"维护记录：任务中不读"；validation.md 开头一句说明用途。不移出发行包，保留追溯。
- **分类**：D。

### 5.2 只读工具输出（T）

#### T1　所有命令支持 `--brief`：≤ 1 KB 摘要，详情写文件

- **问题**：seam 每帧一行（实测 165 B/帧）；dry-run 回显完整配置；precheck 逐样本列出。
- **方案**：
  - `--brief` 只打印结论与汇总（seam：三项汇总 + 最差 5 帧；precheck：provisional_call、summary、手工项；dry-run：每个补丁一行计划摘要）。**所有 NOTICE、错误和拒绝原因照常完整打印，不受 `--brief` 影响**。
  - `--json-out PATH` 把完整结果写文件（独占创建，与现有输出规则一致），需要时再读。
  - 默认行为不变，避免破坏已有调用；文档中给智能体的命令统一写 `--brief`（Q4：是否改为默认）。
- **预期节省**：每轮迭代约 5–7 千 token（以 96 帧补丁计；长补丁更多）。
- **分类**：A（不碰媒体，测试比对 `--json-out` 内容与现有 stdout 逐字节相同）。
- **验收**：`--brief` 输出 ≤ 1 KB（不含 NOTICE）；`--json-out` 与旧 stdout 逐字节相同；qc_tools_test、smoke 新增用例。

#### T2　`fit_patch.py` 打印变更摘要，输出紧凑化

- **问题**：智能体为了知道 fit 改了什么要打开 46.9 KB 的 `proposed.json`（实测）。
- **方案**：stdout 打印一屏摘要：配准模型、相关度最小值、平移／缩放／旋转范围、相对初始值的最大变化、调色增益与偏移、留出验证误差前后对比、是否采用新调色。关键帧仿射每帧一行写出（`separators` 紧凑、外层仍缩进），字段与语义不变。业主复验：同一 `proposed.json` 用标准库紧凑序列化后由 49,426 字节降到 21,504 字节（−56.5%），解析对象不变；这只验证了序列化潜力，T2 实现后仍须对紧凑配置另跑成片确认逐字节相同。
- **分类**：A（同一输入下，解析后的配置对象与现在完全相等；merge 成片不变）。
- **验收**：新旧 `proposed.json` 经 `json.load` 后相等；摘要字段有单测。

#### T3　`doctor --policy [--record PATH]`：自动比对工具版本与已有本机核查记录

- **问题**：M9。判断"版本是否变化"本身就要读 3.9 千 token 文档并人工比对。
- **方案**：`doctor --policy` 读取 TOOL_POLICY.json 的组件与版本范围，与本机实际版本、OpenCV 发行包名、FFmpeg 构建配置行比对；给 `--record` 时再与本机已有核查记录（tool-review-template 格式，存放在任务目录或 `~/.claude/flow-patch-local/`，不进仓库）逐项比对，输出"与记录一致／以下项变化，需要按 tool-policy 重新核查"。**它不判定许可证或安全是否合格，只判断"是否与上次核查一致"**——与 tool-policy.md 第 4 条"已有本机核查记录仅在版本、构建、来源、用途及授权范围一致时复用"相符。
- **预期节省**：同机后续任务不必为了确认"无变化"而重读白名单文档；首台机器不变。
- **分类**：A（只读）。
- **验收**：版本相同／不同／缺记录三种情形的测试；输出不含路径以外的个人信息。

#### T4　`flow_patch.py status --dir 任务目录`：一屏任务状态

- **问题**：M10。上下文压缩、换会话或交接时，状态要从多份 JSON 中重建。
- **方案**：扫描目录中的 `*.report.json`、`*.visual-review.json`、配置与返回片，按补丁列出：已到阶段（prepared／returned／prechecked／check export／final）、最新完整导出、视觉记录各状态计数，以及**作废检测**（依据下面的依赖指纹）。不建状态文件，全部由已有报告推导。
- **依赖指纹（修订 1）**：merge／prepare 报告新增 `inputs_fingerprint`，status 只在**全部**分量与当前一致时判 `fresh`：
  1. 源片、每个返回片的 SHA-256（报告已有）；
  2. **配置语义哈希**：解析路径、补齐默认值后按规范 JSON（键排序、无空白、数值规范化）计算，不含 `name` 等不影响输出的字段；只改空白或键顺序仍为 fresh，改任何影响输出的值即 stale——即使碰巧成片字节没变也判 stale，因为成片对应的是旧配置；
  3. 外部资源清单：C2 遮罩序列每个文件的相对路径、字节数、SHA-256，及清单整体哈希；遮罩视频的 SHA-256；
  4. 影响输出的环境与构建：编码器、`FLOW_CRF`、`FLOW_X264_PRESET`、`FLOW_SEGMENTS`、`FLOW_VIDEO_ENCODER`、内核类别、处理脚本 SHA-256、FFmpeg 版本行；
  5. 导出范围（完整／`--frames`／`--preview`）。
  
  旧报告缺 `inputs_fingerprint` 时判 `unknown`，不判 fresh；任何分量无法读取（文件缺失、无权限）判 `unknown` 并说明原因。视觉记录绑定的 output_sha256 与当前成片不一致 → 其中 pass 全部作废。
- **预期节省**：每次恢复约 1–1.5 万 token → ≤ 1 千。
- **分类**：A（只读；报告新增字段不影响媒体）。
- **验收**：负例至少包括——只替换一张遮罩、删除一张遮罩、新增一张遮罩、只改配置值但成片未变、只改配置空白与键顺序（应 fresh）、替换返回片、改 `FLOW_CRF`、旧版报告无指纹（应 unknown）、成片被替换而视觉记录未更新。

### 5.3 证据与记录工具（E）

#### E1　`qc_tools.py review-pack`：视觉验收证据包

- **问题**：M3–M6，每个任务现写取图和统计代码，多轮迭代重写。
- **方案**：对**最终成片**（读 report 获得配置、帧映射、遮罩）一次顺序读取生成：
  1. 全部修补帧联系图：带源帧号；按补丁区域在原片中的实际像素尺寸自动分页，保证补丁区域显示不小于原像素（可设下限），满足 verification 第 2 节"看不清时拆页"；
  2. 100% 与 200% 边缘图：沿遮罩过渡带，覆盖首尾帧、最大位移帧、seam 最差帧；
  3. 黑白遮罩与原／新轮廓叠色联系图，遮罩面积逐帧曲线（标出跳变帧）；
  4. 每个补丁起止及分段接点前后 0.5 秒（按 fps 换算）逐帧条带；
  5. 补丁外区域与原片的逐帧差值统计（同一解码与颜色空间），对应原则 9；
  6. 过渡带内外亮度、色度逐帧曲线（CSV + 图），用于闪变和分段色跳检查（#18 类问题的取证，不声称修复）；
  7. 左右眼区域逐帧条带（只取图，眨眼判断仍人工）；
  8. `index.json`：每个文件对应的 check id、帧区间与"建议先看"排序（按 seam、遮罩跳变、差值异常排序）。
  
  同时把证据路径及其 SHA-256 写进 `*.visual-review.json` 对应检查的 `evidence`（供 E4 校验证据未被替换），**状态保持 not_run**，记录 `evidence_generated_by`、工具版本与所针对的成片 SHA-256。
- **预期节省**：每任务估计 0.8–2 万输出 token 的临时代码与调试（按 M3–M6 等价代码 300–600 行估算，含 1–2 次修正），多轮迭代时节省成倍。
- **分类**：A（只读媒体，只写新证据文件）。
- **验收**：合成片上每类证据存在、帧号覆盖与修补区间一致（自动断言"全部修补帧都出现在联系图中"）；visual-review 中除 evidence 外字段不变；隐私检查（证据文件名不含源文件名，用 patch 名与帧号）。

#### E2　`qc_tools.py timemap`：时间映射证据

- **问题**：M2；precheck 把时间映射列为手工项。
- **方案（修订 1：全局拟合只作第一步，主体改为逐帧局部映射加分段）**：
  1. **特征**：上传片与返回片中做头部模板跟踪（首帧模板 + 上一帧模板的归一化匹配，return-fit 第 1 节第 3 项），另取排除头部的身体区域高频特征。头部框默认取 matte.region 的外接框，可用 `--head` 指定。
  2. **逐帧局部匹配**：对每个上传帧 i，在返回片 `[i+o−W, i+o+W]` 内找身体特征差最小的帧 j(i)，同时记下最优与次优的差距（margin）、窗口内运动能量。
  3. **分段**：对 `d(i)=j(i)−i` 做变点检测，把 target 切成若干恒定偏移段 `[a,b)→d`。由此区分：恒定偏移（一段）、中间缺帧／重复帧（相邻段偏移跳 ±1 或 ±n，**在任意位置**而不是只在首中尾）、连续缺帧（跳 n>1）、整体变速（偏移随 i 线性漂移，再用全局 `k·返回帧 + 偏移` 拟合核对，残差须小）、片尾续出（末段之后返回片仍有帧）。
  4. **歧义判定**：窗口内运动能量低于门槛（静止画面）→ 该区间 `undetermined`；最优与次优差距不足、或出现周期性多重极小（重复动作）→ `ambiguous` 并列出候选偏移；任一段 undetermined／ambiguous 时整体结论为"无法判定"，不给单一映射。
  5. **输出**：分段表、每段证据（margin、运动能量、例帧号）、建议的显式 `frame_map`（仅作证据文件，不写入配置）、与配置中 generated_start／frame_map 的逐帧一致性。precheck 加 `--timemap` 时并入摘要。
- **边界**：只给证据，**不写配置、不自动变速**；静止重复背景仍需动作核对的提醒保留。
- **分类**：A（只读）。
- **验收**：合成用例由脚本按固定种子生成，至少包括——恒定偏移；整体变速（25→24 重采样）；片尾多帧；**中间随机位置**缺 1 帧（多个种子，覆盖靠近首尾与中段）；连续缺 2–3 帧；中间重复 1 帧；缺帧与偏移同时存在；静止段（应 undetermined）；周期性重复动作（应 ambiguous）；足帧但整体偏移若干帧（与 C1 的未核验状态配套）。断言分段位置误差 0 帧，或明确给出无法判定；任何用例都不得把缺帧误报为轻微变速。

#### E3　接缝颜色取样并入 `seam`

- **问题**：M5 的 LAB 取样每次现写，且 verification 要求记录颜色转换、取样区域与 ΔE 算法。
- **方案**：`seam --colour` 在过渡带内外按固定宽度取样，输出 CIELAB（明确从解码 BGR 按 sRGB/709 转换的公式）均值与 ΔE2000，以及取样区域描述；首、中、尾、最大动作帧。只作定位证据，不设通过阈值。
- **分类**：A。

#### E4　`qc_tools.py review-set`：用命令更新视觉记录

- **问题**：M7。手写 JSON 费输出 token，且容易把旧文件的 pass 留在新文件上、或过早写 visual_pass。
- **方案**：
  ```
  python scripts/qc_tools.py review-set --record patched.visual-review.json \
      --check all_repair_frames_contact_sheets --status pass \
      --intervals 48:144 --evidence review/sheets-p0-*.png --reviewer user --note "..."
  ```
  - **逐项写入的校验**：check id 必须是模板中的已知 id，未知 id 拒绝；状态枚举、区间格式；`pass`／`fail` 必须带至少一个**存在的**证据文件（记录其 SHA-256），`not_applicable` 必须带理由；写入前重新计算成片 SHA-256，与记录不符即拒绝（提示记录已作废）。
  - **整体状态由规则计算，不允许手动设置**，优先级 `fail` > `not_run` > `pass`：
    - 任何适用项 fail → `visual_fail`（即使同时有 not_run）；
    - 否则任何适用项 not_run、或模板中有检查项在记录里缺失 → `pending_visual_review`；
    - 只有同时满足以下**全部门槛**才可能是 `visual_pass`：
      1. 全部适用项 pass，not_applicable 项都有理由；
      2. `technical_validation.status` 为 pass；
      3. 记录指向的技术报告存在，`scope.deliverable=true`（复用检查用导出已有的 `deliverable=false`，预览片、局部导出的记录永远不能 pass）；
      4. 报告 `output_sha256`、记录 `output_sha256`、当前成片 SHA-256 三者一致；
      5. 每个 pass 项引用的证据文件仍存在且 SHA-256 与写入时一致；
      6. 覆盖型检查（全修补帧联系图、遮罩、接点窗口、正常速度播放等，清单写在模板中）的审看区间并集覆盖报告中全部修补区间，接点类覆盖每个接点前后约 0.5 秒。
  - **用户审看**：`--reviewer user` 不能自证。必须同时给 `--confirmation`，写明用户确认的出处（例如会话中用户原话的引述与时间，或用户签名的审看单文件路径），文件型确认记录其 SHA-256；没有确认依据的 user 项按 not_run 计入整体状态。智能体不得替用户写确认原话（Q3）。
- **预期节省**：约 2.4 千输出 token／次整份重写 → 每项约 50 token。
- **分类**：A（不碰媒体），并**加强**了质量合同第 3 条。
- **验收**：整体状态真值表测试，以及负例——同哈希但 `technical_validation` 未运行；对预览片（`deliverable=false`）人工构造的记录；pass 项证据缺失或被替换；fail 与 not_run 并存（应 visual_fail）；未知 check id；模板项在记录中被删除；not_applicable 无理由；`--reviewer user` 无确认依据；报告与记录的输出哈希不一致。

### 5.4 配置能力（C）

#### C1　`flow_patch.py init-merge`：由 prepare 报告生成 merge 配置

- **问题**：M1 手抄六类字段。
- **方案**：
  ```
  python scripts/flow_patch.py init-merge --prepare-report upload.report.json \
      --generated flow-result.mp4 --name person-a-seg1 --output merge.json [--append]
  ```
  从报告中的配置与 `core_in_upload` 填 source、source_size、fps、crop 轨迹、work_size、target=core、generated_start=`core_in_upload[0]`；探测返回片：尺寸等比则接受，帧数不足以覆盖 target 时**拒绝并列出选项**（显式 frame_map 重复末帧／null 保留原片／重刷），不自动变速、不补帧；`--append` 往已有 merge.json 加补丁（多人、多段）。
- **待配置状态写进配置本身（修订 1）**：终端警告会在压缩或换会话后丢失，所以阻断条件存放在配置文件里：
  - **matte**：不自动推断（原则 11），也**不写入模板中可直接渲染的默认遮罩**。未给 `--matte rect|ellipse x y w h --feather N` 时写 `"matte": {"pending": "set the blend matte; see SKILL.md principle 11"}`。含 pending matte 的补丁在任何 merge（完整、`--frames`、`--preview`）、`fit_patch`、`seam`、`precheck` 中都拒绝并说明原因；`--dry-run` 照常列出计划，并把该补丁标为 `matte pending`。
  - **时间映射**：尺寸、fps、帧数合格不能证明 generated_start 正确（返回片可能足帧但整体偏移）。init-merge 写 `"timing": {"status": "unverified", "basis": "init-merge from core_in_upload"}`。**完整 merge 遇到 unverified 即拒绝**；检查用导出允许运行（用来目视核对时间），报告 `scope.timing=unverified`。改为 `verified` 必须给出依据：`"basis"` 指向 E2 timemap 证据文件（记录 SHA-256，且其映射与配置逐帧一致），或写明人工核对的帧号与方法；改 generated_start／frame_map 后状态自动回到 unverified（依据 T4 的配置语义哈希判断）。
  - 两个字段都只出现在 init-merge 生成的配置中；手写的旧配置没有这些字段，行为不变。
- **预期节省**：每补丁约 1.5–3 千 token，并消除抄错导致的返工。
- **分类**：N（新命令与新字段，已有配置不受影响）。
- **验收**：生成的配置与人工按 workflow 第 4 节填写的结果在测试中逐字段相等（matte、timing 除外）；返回片少帧、宽高比不符时拒绝；负例——matte pending 时各命令均拒绝；timing unverified 时完整 merge 拒绝、检查用导出可运行且报告标注；**足帧但整体偏移若干帧**的返回片：init-merge 照常生成但保持 unverified，timemap 报告与配置不一致，改为 verified 被拒绝；改 generated_start 后自动回到 unverified。

#### C2　`matte.sequence`：逐帧灰度遮罩作为正式输入

- **问题**：M8。return-fit 第 5 节的包装脚本靠替换主脚本函数实现，每次都是"清单外辅助代码"审批往返，还要写包装代码。
- **方案**：merge 的 `matte` 增加可选 `sequence`，与 `region`／`shape`／`feather` 互斥（同时出现即拒绝），`protect` 照常生效。为避免不同实现给出不同 alpha，规格写死如下（修订 1）：
  - **坐标与尺寸**：遮罩为工作坐标，尺寸必须等于 `work_size`，否则拒绝；不缩放、不裁切。
  - **帧寻址**：一律按**绝对源帧号**。目录形式 `{"dir": "masks/a", "pattern": "{frame:06d}.png"}`，文件名中的数字即源帧号（不是 target 内序号）。视频形式 `{"video": "masks/a.mkv", "start_frame": 48}`，视频第 0 帧对应源帧 `start_frame`；视频 fps 必须与源片 fps 精确相等（有理数比较），帧数必须覆盖所需区间，时间戳须在帧网格上、零起点（复用现有严格探测）。
  - **格式**：PNG 只接受单通道 8 位或 16 位灰度（不接受带 alpha 或 RGB 的 PNG，避免"取哪个通道"的分歧）；视频只接受无损编码（FFV1、PNG、rawvideo）的 `gray`／`gray16le`，其他编码或像素格式拒绝。
  - **归一化与合成**：8 位 `v/255`、16 位 `v/65535`，转 float32；**不再做 smoothstep 或羽化**（序列视为已烘焙的最终 alpha）；之后对 `protect` 矩形置 0，与现有 `_matte()` 的保护区处理相同；随后进入现有 blend 路径。multiband 与 alpha 的现有限制不变。
  - **区间**：完整导出在开始前校验 target 内每一帧的遮罩都存在且合规；`--frames`／`--preview`／分段只读取并校验与本次导出区间相交的帧，报告注明校验范围（与 #20 中 `scope.per_frame_checks` 的做法一致）；完整导出仍校验全部。
  - **记录**：报告记录遮罩清单（相对路径、字节数、SHA-256）及清单整体哈希，纳入 T4 依赖指纹。
- **边界**：生成遮罩的任务脚本仍是 tool-policy 允许的"仅用 OpenCV／NumPy 的辅助代码"，保留代码记录；本项只消除"改写主处理链"这一层（Q2：上游确认此后合成步骤不再需要单独的清单外审批）。
- **分类**：N（新字段；无此字段的配置成片逐字节不变）。
- **验收**：与 return-fit 第 5 节包装方式在同一遮罩下的成片逐字节相同；8 位与 16 位同值遮罩（如 255 与 65535、128 与 32896）给出预期 alpha；目录与视频两种形式同内容时成片相同；负例——缺一帧、多余尺寸、RGB／带 alpha PNG、有损视频、fps 不等、start_frame 使区间越界、与 region 同时出现；`--frames` 只校验区间内帧而完整导出报出区间外缺帧。

### 5.5 操作约定（P）

#### P1　标准流程卡与集中确认点

在 SKILL.md 路由表下给出一张 6 步流程卡（doctor → inspect／定目标 → prepare → 用户生成 → precheck(+timemap) → `merge --frames --preview` 迭代 → 完整导出 + review-pack → 验收记录），每步标明"需要用户的输入"。把需要用户决定的事集中在三个点：①开工时一次确认目标、范围、验收标准与嘴部来源；②预检后一次给"可贴／费劲／建议重刷"及数据表；③交付时一次给待审项清单。减少零散追问带来的轮次。

#### P2　看图预算与审看分工

- 智能体看图优先用 review-pack 的裁剪图，不反复整帧读入 4K 原图；同一张图在同一成片哈希下不重复看。
- **全部修补帧巡检和正常速度播放这类要求不减**。智能体自己没法正常速度播放时，不再停在"待审"反复说明，而是把 review-pack 和播放检查清单交给用户，用户确认后经 `review-set --reviewer user` 记录（Q3）。

#### P3　委派的边界（宿主支持子任务时）

只把机械运行（执行命令、收集 `--brief` 摘要）委派给上下文较小的子任务，返回摘要。白名单判断、用户批准、视觉结论不得委派；SKILL.md 中"不用另一个智能体绕过"的规则保持原文。

#### P4　缓存友好

SKILL.md 与 AGENT_ENTRY.md 不写版本说明、日期等每版都变的文字（版本信息留在 README、CHANGELOG），让常驻前缀在版本之间尽量不变，提高宿主提示缓存命中率。

---

## 6. 预估收益（目标估算，待第 8 节实测；合成复验不能证明真实会话收益或实片视觉质量）

以"首台机器、单人、单返回片、96 帧补丁、两轮调参"为例。图片 token 不计入（由验收规则决定）。

| 类别 | 现状估算 | 提案后估算 | 依据 |
|---|---:|---:|---|
| 文档阅读 | 37.8 千 | 约 15 千（首台机器仍读白名单全文）；后续任务约 8 千 | D1–D3，2.1 节表 |
| 命令输出（两轮） | 约 15–40 千（取决于是否打开 proposed.json、seam 帧数） | 约 3 千 | T1–T2，2.2 节实测字节 |
| 临时辅助代码（**输出**） | 约 1–2.5 万 | 约 1 千（命令行） | E1–E3、C2；按等价代码行数估算 |
| 配置转录与记录手填（**输出**） | 约 5–8 千 | 约 1.5 千 | C1、E4 |
| 状态重建（每次压缩／续会话） | 约 1–1.5 万 | ≤ 3.5 千（status + SKILL 核心） | T4、D1 |
| 合计（不含图片） | **约 9–12 万** | **约 2.5–3.5 万** | 下降约 60–70% |

注意：上表"现状"的命令输出和临时代码两行是按 2.2 节字节数与等价代码量推算的区间，不是真实会话的测量；输出 token 的单价高于输入，按费用算的下降比例可能大于按 token 数算的比例。

---

## 7. 实施批次

| 批次 | 内容 | 依赖 | 影响面 | 退出条件 |
|---|---|---|---|---|
| W0 | 本 PR：`tools/token_budget.py`、`tools/token_fixture.py`、基线证据、本计划；真实会话测量协议（第 8 节） | 无 | 仅新增文档与只读工具 | 上游确认测量协议与第 10 节问题 |
| **W1（首批，修订 1）** | **T1、T2，带阻断条件的 C1（matte pending、timing unverified）与 E4（整体状态门槛、用户确认依据）** | W0；main `8915d86`（#20 已合并，依赖已满足） | 只读输出 + 新命令／新字段 + 记录工具 | T1／T2 输出等价性；C1、E4 第 5 节全部负例；已有配置成片逐字节不变 |
| W2 | D1–D5、P1–P4、`tests/doc_rules_test.py`、`docs/plans/rule-map.json` | W0；可与 W1 并行 | 仅文档 + 测试 | 规则保全 100%；token_budget 前后对比；三平台 CI 绿 |
| W3 | T3、T4（含依赖指纹） | W1 | 只读命令 + 报告新增字段 | T4 全部负例；旧报告判 unknown；成片哈希不变 |
| W4 | E1、E2（分段映射）、E3 | W1（E4 记录格式）、W3（指纹） | 只读媒体 + 新证据文件 | 第 5.3 节各项验收；E2 歧义用例 |
| W5 | C2 | W3（指纹覆盖遮罩清单） | 新可选字段 | C2 规格与负例；与包装方式成片相同 |
| W6 | 实测与发布 | W1–W5 中通过验收的部分 | — | 第 8 节 A/B 结果公开；只纳入过门槛的项 |

各批次独立 PR、可单独回退。按业主复验建议，首批为 T1／T2 与带阻断条件的 C1／E4：T1／T2 已复现的冗长输出收益最直接；C1、E4 的阻断条件把"待配置"与"不可通过"写进文件，能在压缩或换会话后仍然生效。文档路由（W2）可并行。E1 第 6 项视 #18 闪变调查需要可提前。

与 #19／#20 的协调：实施批次改动 `qc_tools.py`、`flow_patch.py` 的命令行层，基于已合并 #20 的 main 开发，复用其单次解码与共享探测（E1 的顺序读取与 precheck 的单遍读取同一模式）。注意 #20 合并时撤回了零复制短路（53bd1c6，见 `docs/plans/pr20-review.md`），T1 的 `--brief` 不得改变 `--copy-unpatched` 的回退判断与编码参数。

---

## 8. 印证协议

### 8.1 静态（任何人可复跑）

- 文档：`python tools/token_budget.py --json before.json`（本 PR 基线）与 W2 后的 `after.json` 对比；W2 合并时改为按节计算。
- 命令输出：`python tools/token_fixture.py --out 新空目录 --package 被测目录 --json record.json`。每次测量都保存完整记录（提交、环境、argv、stdout／stderr 分列字节数与哈希、产物哈希），平台差异和实现变化才能区分开；W1 合并后在 fixture 中加 `--brief` 变体，与 [agent-token-baseline.json](evidence/agent-token-baseline.json) 中的 `fixture_record` 对照。
- 跨平台比较只比同一记录内的相对变化，不比绝对字节数（FFmpeg／x264 构建、平台和路径长度都会影响字节数，见业主复验）。

### 8.2 真实会话 A/B（需要上游或使用者执行）

1. **固定任务脚本**：3 个合成任务（单人单返回、单人两段接点、两人相邻补丁），素材由脚本生成，指令文本固定；另可选 1 个使用者本机实片任务，结果只公开汇总数字。
2. **对照**：A = v2.9（或 #20 合并后的 main），B = 本计划实施后的版本；同一宿主、同一模型、新会话；每组至少 3 次，交替顺序。
3. **记录**：宿主用量日志中的输入 token、缓存读 token、输出 token、图片数；工具调用次数；人机往返轮次；临时脚本数与行数；错误／返工次数（抄错配置、过期 pass 未作废等）。
4. **质量门槛（任何一项不过即该批次不发布）**：
   - 同一配置下成片 SHA-256 与 A 相同（C 类新功能另以其自身测试为准）；
   - 视觉记录中适用检查项数量不少于 A；没有任何 `pass` 缺少证据或绑定了旧哈希；
   - 交付文字中状态区分与 A 一致（不出现"无接缝"之类无证据的说法）。
5. **成功标准（预先约定，不是已达成结果）**：非图片总 token 中位数下降 ≥ 40%，输出 token 中位数下降 ≥ 50%，人机往返轮次不增加；若未达到但消除了确定的手工错误来源（M1、M7），单独说明价值，不声称达到 token 目标。

---

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| 文档压缩丢失细节，智能体误用规则 | 规则 ID + 保全表 + CI；原文迁移不删；全文仍是权威；A/B 中统计规则违反次数 |
| `--brief` 藏掉警告 | NOTICE、错误、拒绝原因不受 `--brief` 影响，测试断言 |
| 证据包被误当作验收通过 | 状态保持 not_run；`evidence_generated_by` 字段；review-set 由规则计算整体状态 |
| status 作废检测漏判 | 只依据 SHA-256，不依据修改时间；测试覆盖替换文件、改配置、重导出 |
| init-merge 掩盖时间映射问题 | 返回片不足即拒绝；不写 frame_map；timemap 证据独立 |
| matte.sequence 引入错位遮罩 | 尺寸、帧数、灰度校验；报告记录遮罩哈希；与包装方式逐字节比对 |
| 与 #20 冲突 | #20 已合并，各批次基于新 main 开发；本 PR 已 rebase |

每批独立 PR，回退即 revert 对应 PR，不影响其他批次与已有配置。

---

## 10. 请上游确认的问题

- **Q1**　同机同版本的后续任务只读 verification 检查卡、首台机器或版本变化时读全文，是否可接受？
- **Q2**　C2 把逐帧遮罩作为正式输入后，合成步骤是否不再需要 return-fit 第 5 节的"清单外辅助代码"审批（遮罩生成脚本仍按辅助代码记录）？
- **Q3**　视觉记录增加 `reviewer`（agent／user），用户完成的正常速度播放等检查经 `review-set --reviewer user --confirmation …` 记为 pass（必须有确认出处，见 E4），是否符合验收规则本意？确认出处接受哪些形式（会话原话引述加时间、签名审看单文件）？
- **Q4**　`--brief` 保持可选，还是改为默认、旧输出改用 `--full`？（改默认会影响已有脚本调用。）
- **Q5**　AGENT_ENTRY.md 缩为指针，是否影响 Codex 等宿主的使用方式？
- **Q6**　第 8.2 节真实会话 A/B 由谁在哪个宿主执行？是否可沿用此前 Windows 实测（issue #8、#10）的方式由使用者跑实片任务并只回报汇总数字？
- **Q7**　E1 第 6 项（过渡带逐帧亮度曲线）是否应提前到 #18 的调查中使用？

---

## 附：本 PR 文件

- `docs/plans/agent-token-workflow-v2.10.md`：本计划。
- `docs/plans/evidence/agent-token-baseline.json`：2.1、2.2 节的原始数字与环境。
- `tools/token_budget.py`：文档阅读路径估算，只读、仅标准库，不进入技能发行包（`tools/` 在包目录之外）。
- `tools/token_fixture.py`：命令输出基线 fixture（修订 1），生成合成素材与完整配置，运行全部命令并记录提交、环境、命令、stdout／stderr 分列与产物哈希；仅标准库、FFmpeg 与包内脚本，不进入发行包。

---

## 11. 反馈处理记录

### 修订 1：[业主复验](https://github.com/stevenpcny/VideoPatchSkill/pull/21#issuecomment-6545163483)

复验结论：支持分批实施；在 Windows（Python 3.13.16、NumPy 2.5.4、opencv-python 5.0.0.93、FFmpeg 9.0.2）上复现了输出与配置过长、验收记录大量手填的基线；文档估算三个场景数字完全一致。

| 反馈 | 处理 | 位置 |
|---|---|---|
| E4 不能只查成片哈希和各项状态 | 整体 visual_pass 增加六项门槛（技术校验、报告 deliverable、三方哈希一致、证据存在且未被替换、审看区间覆盖、not_applicable 有理由）；fail 优先于 not_run；未知或缺失检查项拒绝／按待审计；`--reviewer user` 必须带确认出处；补充负例 | 5.3 E4 |
| C1 默认 matte 必须不可正式导出；时间映射要标待核验 | 不写可渲染的默认遮罩，写 `matte.pending`，所有渲染与 QC 命令拒绝；`timing.status=unverified`，完整 merge 拒绝、检查用导出可运行；改为 verified 需依据，改映射后自动回到 unverified；加入"足帧但整体偏移"用例 | 5.4 C1 |
| T4／C2 定义完整依赖指纹；C2 规格写死 | 指纹五个分量（源与返回哈希、配置语义哈希、外部资源清单、影响输出的环境与构建、导出范围），旧报告判 unknown；C2 规定坐标、绝对源帧寻址、视频 start_frame 与 fps、PNG／视频格式、8／16 位归一化、不再羽化、protect 处理、区间校验；补充负例 | 5.2 T4、5.4 C2 |
| E2 全局 k+offset 表达不了中间缺帧 | 改为逐帧局部匹配 + 变点分段，全局拟合只用于核对整体变速；静止、重复动作、margin 不足时输出无法判定；用例覆盖任意位置缺帧、连续缺帧、重复帧 | 5.3 E2 |
| 首批先做 T1／T2 与带阻断条件的 C1／E4 | 批次表调整，首批 W1 即此四项 | 第 7 节 |
| 60–70% 属于目标估算 | 第 6 节标题与结论摘要明确为目标估算，以第 8 节 A/B 为准 | 第 1、6 节 |
| 补充可运行的完整 fixture 脚本与具体配置，记录提交、环境、命令、stdout／stderr 分列及产物哈希 | 新增 `tools/token_fixture.py`；证据文件改为 `fixture_record`，并保留首次临时测量与业主复验数字以便追溯 | 2.2 节、证据文件 |

### 修订 2：#19、#20 合并后

- 分支 rebase 到 main `8915d86`，无冲突。
- 在 main 上用 `tools/token_fixture.py` 与 `tools/token_budget.py` 重跑，结果见本文开头的基线说明；数字不变，证据文件新增 `fixture_record_main`。
- 首批 W1 的依赖（#20 合并）已满足；W1 须保持 #20 合并时撤回零复制短路后的 `--copy-unpatched` 行为。

### 修订 3：首批 W1 已实施（同一 PR）

范围：T1、T2、C1（含 `mark-timing`）、E4（含 `review-status`），按第 5 节修订 1 的阻断条件实现。改动文件：`scripts/flow_patch.py`、`scripts/qc_tools.py`、`scripts/fit_patch.py`、`assets/visual-review-template.json`（新增 `coverage_requirements`）、`references/parameters.md`／`workflow.md`／`verification.md`、`SKILL.md`、`AGENT_ENTRY.md`、`CHANGELOG.md`、两份 README、CI、`SHA256SUMS.json`；新增 `tests/workflow_test.py`（11 项）。没有发版、没有改版本号或 ZIP。

自验证（Linux，Python 3.13.18，NumPy 2.5.3，OpenCV 5.0.0，FFmpeg 6.1.1，NumPy 内核）：

- **媒体不变**：同一参数下 `init-merge` 生成的配置与手写配置成片 SHA-256 相同；紧凑写出的 fit 建议与完全缩进写法成片相同；无 `timing` 字段的旧配置报告不出现 `scope.timing`。
- **输出体量**（同一 fixture，main + W1）：

  | 项目 | 之前 | `--brief`／W1 后 |
  |---|---:|---:|
  | `seam` stdout | 15,872 B | 545 B |
  | `precheck` stdout | 3,703 B | 1,279 B（高于 T1 的 1 KB 目标：保留了 summary、seam 汇总与人工项清单） |
  | `merge --dry-run` stdout | 3,437 B | 305 B |
  | `fit_patch` 建议文件 | 46,817 B | 23,394 B（另打印 591 B 摘要） |

  `--json-out` 文件与不带 `--brief` 的 stdout 逐字节相同（测试断言）。
- **阻断与负例**：workflow_test 覆盖 E4 的预览片人工构造记录、技术校验未运行、证据缺失与被改、区间不全（修补区间与接点窗口）、fail 与 not_run 并存、未知与缺失检查项、无理由 not_applicable、无确认出处的用户项、报告哈希不一致；C1 的 matte pending（6 个命令均拒绝且不留输出）、timing unverified（完整导出拒绝、检查用导出可运行并标注）、足帧但整体偏移 2 帧的返回片、改 generated_start 与替换返回片后回到 unverified、返回片不足、重名与非法遮罩。
- **测试能否发现问题**：分别去掉"报告须为可交付完整导出"、"fail 优先于 not_run"、"完整导出须核验时间"三处判断，对应测试均失败；恢复后通过。
- **全部测试**：smoke 28（跳过 1）、security 16、qc_tools 9、color_pipeline 9、remote_run 4、workflow 11、ffmpeg_regression 6 全部通过；构建原生内核后 workflow 与 smoke test_07／14／17／21 复跑通过。Windows／macOS 由 CI 复跑。

与计划的差异：

- `precheck --brief` 为 1,279 B，未达 1 KB；是否进一步裁剪（如去掉 `manual_checks_remaining` 文字）请上游决定。
- C1 中"timemap 证据与配置逐帧一致才可标 verified"依赖 E2，尚未实现；当前 `mark-timing` 要求非空的文字依据，可选附证据文件并记录其 SHA-256。E2 实施后再加一致性校验。
- `review-set` 对用户确认只要求写明出处（引用原话与时间，或签名文件），无法核实其真伪，记录中会标出 `reviewer: user` 与出处供人工核对（Q3 仍待上游确认）。

