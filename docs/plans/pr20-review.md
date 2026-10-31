# 对 PR #20 的独立审阅

日期：2026-10-31。审阅对象：[PR #20](https://github.com/stevenpcny/VideoPatchSkill/pull/20)，固定 head `8b7525d07e669f52d9f31840e15bd99a4048c0c6`；对照为 main v2.9 `401f8dadef2382d98e8a653dde2aa4a069f1310c`。

结论：**支持主要优化方向；当前提交尚不宜按“全部为结果保持型改动”整体合并。E7 零复制短路改变了编码行为，需要修正或拆分。** 未发现其他足以阻断合并的新增问题，不代表已经证明所有场景无缺陷。

## 1. 已复现的合并阻断：E7 的比较对象错误

位置：`flow-video-patch/scripts/flow_patch.py:1349–1351` 新增的 `all(render...)` 分支，以及随后普通完整导出路径；测试 `test_27_copy_unpatched_with_nothing_to_copy_renders_directly`。

触发条件：H.264 源、48 帧、24fps、GOP12、源 `bframes=0`、所有 GOP 都有补丁、`FLOW_SEGMENTS=1`、`--copy-unpatched`。

- 旧路径从源视频重排延迟推导 `bframes`，渲染时显式 `-bf 0`。
- 新路径提前抛出 `CopyFallback`，进入普通完整渲染；未传入旧路径的 `bframes`，libx264 使用默认 B 帧设置。
- 本机独立复验：旧输出 `has_b_frames=0`，新输出 `has_b_frames=2`；两者都为 48 帧、24fps，但 **48/48 帧的解码 SHA-256 不同**，文件 SHA-256 也不同。
- `FLOW_SEGMENTS>1` 还可能把旧路径中的单个连续渲染块改为普通分段编码；这个第二分支仅静态确认，本次没有对其另做实测。

因此，“新输出等于不带 `--copy-unpatched` 的普通导出”不等于“新输出等于旧版本相同配置的导出”。当前 test_27 比较了前者，不能证明 A 类不变性。源码可能保持预编码像素，但编码策略变化按 PLAN 属于 B 类。本次没有观察到或声称画质必然变差；问题是未经声明、未经 B 类验收的行为变化。

复现脚本：[tools/review_zero_copy.py](../../tools/review_zero_copy.py)；实际结果：[pr20-zero-copy.json](evidence/pr20-zero-copy.json)。脚本只用临时合成素材和既有工具，不修改两个检出目录，输出独占创建。

```text
python tools/review_zero_copy.py --baseline BASELINE_SKILL_DIR --candidate CANDIDATE_SKILL_DIR --output NEW_RESULT.json
```

目录应分别对应上面两个固定提交的 `flow-video-patch`。本次 Windows、Python 3.13.16、NumPy 2.5.4、OpenCV 5.0.0.93、Gyan FFmpeg 9.0.2；两端固定 NumPy、处理/编码线程4、segments1、libx264 CRF17/fast。源生成、配置与比对方法均保存在脚本内。

建议任选一个明确方案：

1. **本次只合并结果保持优化**：先撤出 E7，保留其他六项。E7 独立 PR 再决定编码语义。
2. **保留 E7 但满足 A 类**：直接渲染时保留旧 `bframes`、旧连续块/分段安排、封装与音频行为；加入相对旧路径的预编码字节和实际解码对照，覆盖 B0/B1/B3、segments1/2、有/无音轨。还须记录容器字节是否一致，不能只比较新路径与普通导出。
3. **承认是 B 类**：改正文档/标题的“全部结果保持”声明，增加明确 opt-in，按计划补足编码结果和视觉证据后另行接受。仅改一句描述不足以让新的默认行为满足既定门槛。

## 2. 已确认或支持的部分

| 项目 | 本次独立核验 | 结论 |
|---|---|---|
| 共享 probe + precheck 共享读取 | 同一观测脚本：完整 probe 6→2，Reader 4→2；8 样本保持，结论 fits | 结构性冗余确实消除；单次耗时不作速度承诺 |
| QC 测试 | 本机 9/9 通过，17.664 秒 | 包含缓存作用域、替换文件、严格请求和两类采样 |
| smoke 测试 | 本机 28 项：27 pass、1 skip，112.553 秒 | skip 为新检出未构建 native；不能声称本机双内核全过 |
| 三平台 CI | #20 六组均 success；#19 原提交六组也 success | CI 证据成立，但未覆盖上述旧编码路径差异 |
| PR #17 整合 | 已重命名为 test_26；dry-run 带 frames 仍全段检查 | 接受该语义，避免重复 test_22 与验证范围歧义 |
| 不活跃 Reader | 实现与新增 test_25，本机 smoke 通过 | 支持 |
| 色度安全 ROI 分组 | 静态检查组框再次相交会合并、保持补丁顺序；test_28 本机通过 | 支持；没有把该项当作金字塔裁剪 |
| offset 同遍取双 ROI | 实现与新增 QC 测试通过 | 支持；尚未实现有界内存流式 offset |

precheck 本次结果：[pr20-precheck.json](evidence/pr20-precheck.json)。它的 `reference_baseline_commit` 表示对照基线，`matches_reference_manifest=false` 明确表示被测的是已修改候选包。原观测脚本硬编码 `baseline_commit` 容易在复用时误标候选；此次同步修正为 schema v2，包版本读取实际 TOOL_POLICY，历史基线证据保留不改。

## 3. 对 PLAN 执行方式的调整

- 可以接受不等完整 P0 大框架就先消除已证实的重复 probe/Reader；本次已有直接计数和回归证据。仍须保留精确版本、输入、参数与原始数据，不把框架建设本身作为小型确定性改进的阻碍。
- PR #20 的性能数值使用 Linux 单机交替各3次，与原计划5次配对及发布矩阵不同。可作为开发期观测，暂不升级为跨平台发布承诺；建议后续补可公开的基准脚本和原始运行值。
- “P1/P2/P4/P5 已做”应细化到子项：P1 的共享上下文采用有限作用域实现；P5 只完成双 ROI 同遍读取，未完成 bounded offset/framehash 流式解析；P0/P3/P6/P7 仍未完成。
- 不需要把未发布候选立即命名为新 v2.9，保留未发布 CHANGELOG 是合理做法；合并不等于发行，也不等于更新用户已安装技能。
- 这次只做合成素材审计，没有实片、播放或人物视觉验收。质量状态不能因 CI 全绿自动变为 visual_pass。

## 4. 合并建议

计划文档 PR #19 在补入本次记录后可以合并；它只携带文档、观测/复现工具和脱敏证据，不改变运行中的媒体处理脚本。实现 PR #20 应先处理第1节例外，再核对最终 head 与 CI，避免将当前发现带入主线。PR #17 已被 #20 整合，但 #20 未合并前不应把“待替代”误写成“已进入 main”。
