# PR #23 follow-up: local T0/T1 evidence

Target: f5f13c87253e0c587ce7b17d9cb91ea2450bb158. These files are experimental evidence, not changes to the installed skill. Synthetic arrays/media only.

## Reproduce

Use Python/NumPy/OpenCV/FFmpeg as recorded in t1-*.json. From a workspace parent, check out the target in a directory named `VideoPatchSkill-pr23-review`. Copy the scripts into a sibling `pr23-followup` directory. Run each command serially from that parent:

```text
python pr23-followup/t1.py --kernel numpy
python pr23-followup/t1.py --kernel native
python pr23-followup/t0.py --kernel numpy
python pr23-followup/t0.py --kernel native
python pr23-followup/key-cost.py
```

For native, build the target's native/Cargo.toml with `cargo build --release --locked` first. No downloaded binaries. Scripts set process threads=4 and OpenCV threads=4; encode threads=4 is recorded but no encoder runs in T0/T1. All iterations execute synchronously; these are blend-only timings, not task delivery time. No benchmark runs concurrently with another benchmark. Machine was on Power saver; thermal state, frequency and unrelated background load were not controlled or measured. Public evidence does not contain host paths or credentials.

## Results

Ten measured repetitions per path/size/frame-count, after each path's initial build/warmup. Timing includes bytes-key generation and lookup in B. C uses a prepared immutable-spec tuple; separate key-cost.json measures generating/hashing that tuple. Its creation is not included in T1 C's reported hot time. D excludes mask-pyramid construction and lookup and is an idealized diagnostic, not deliverable performance.

For 30-frame runs, changes versus A (negative is faster):

| Kernel | Size | B bytes key | C prepared semantic key | D prebuilt |
|---|---|---:|---:|---:|
| NumPy | 640x640 | +2.69% | -2.67% | -2.08% |
| NumPy | 960x540 | +2.87% | -1.99% | -2.36% |
| NumPy | 1920x1080 | +3.29% | -0.91% | -1.35% |
| native | 640x640 | +4.12% | -2.47% | -3.40% |
| native | 960x540 | +4.95% | -1.24% | -1.37% |
| native | 1920x1080 | +5.45% | -0.97% | -0.39% |

T0 instrumentation median on/off overhead: NumPy about 0.23%, native about 0.71%. Each kernel has five A/A pairs and ten balanced off/on pairs, with raw observations, process CPU and wall time retained. A/A variation is comparable to small candidate improvements; do not claim a robust speedup.

Separate diagnostic profile at 640x640 found mask pyrDown about 0.60% (NumPy) / 0.73% (native) of blend wall time. This is a single instrumented profile, not a universal Amdahl bound. Residual combines allocations, band scheduling, element-wise operations and finish; it does not identify their individual cost. Process CPU is parent process CPU and must not be summed with wall time. Images still perform six pyrDown calls and mask three calls per baseline frame; cache hits replace only the three mask calls. All tested fusion outputs are byte-identical.

## Quality coverage and boundaries

Tested three sizes, levels 2/3, fade 1/.5/0/1 plus .13/.29/.87, changed protect/size/levels and cache eviction. The four fade values are correctness cases; timed 3- and 30-frame loops use a static fade=1 mask. Same-address mutation is explicitly checked for B's content key. C assumes the passed array was generated from the immutable semantic specification; arbitrary mutation under an unchanged semantic key is not prevented. A production API needs ownership/read-only enforcement or validation. These results do not certify that adversarial case for C.

Multiband+protect is an internal stress test only: production merge_plan rejects it. Legal multiband uses no protect. Alpha+protect checks preservation outside support separately; it has no pyramid cache and no cache speed claim. Cache budget tracks pyramid array values, not key bytes, Python overhead or total RSS. B's bytes keys can consume additional substantial memory. Cold construction is in cold_seconds. Per-layer gm equality is implied by fresh same-operator builds, but this experiment explicitly asserts final blend equality, not an independent per-layer numerical oracle.

## Prior E01 evidence

Included original scripts, complete per-run output hashes/times, media hashes, portable configuration and audio follow-up. Existing E01 experiment used auto processing/OpenCV threading, unlike fixed-4 T0/T1; do not combine them as a single controlled experiment. Old argv/stderr and exact contemporaneous background conditions were not recorded and cannot be recovered. The original configuration used `color_gain`/`color_offset`, which the implementation does not consume (`gain_bgr`/`offset_bgr` are the actual fields). Thus nonidentity color matching was not tested there. The portable config replaces host paths with basenames; its hash differs from the original host-specific JSON.

18 retained files have source-identical audio packet payload and timing fingerprints. Repeated outputs within each E01 path have identical SHA256. A/B final images differ, as expected from the extra lossy generation; the supplementary PSNR is descriptive only.

## Pending requested protocol

T0/T1 here are blend-level experiments. Per-component key/lookup/copy/element-wise phase timers, original-auto vs fixed-thread single-factor runs, thermal/frequency/load monitoring and parent-to-report task timing are not all implemented. Native and NumPy results remain separate. T2's new matched-final-encoding matrix (ultrafast/fast intermediate, bigger patches, no-patch diagnostic, long short-patch, three-round tasks), pre-encode independent reference and final visual checks remain unexecuted. No HDR or remote-host acceptance is claimed. The reviewer requested T0/profile before deciding further work; the observed small fraction argues against prioritizing this cache solely for speed.
