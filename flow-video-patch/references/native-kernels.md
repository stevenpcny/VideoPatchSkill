# 可选编译内核（Rust）

`native/` 是 `flow_patch.py` 逐像素运算的 Rust 版本：YUV↔BGR 色彩转换、补丁外色度均值、alpha／多频段逐元素融合与缩放贴回。只用 Rust 标准库，不下载任何 crate；`flow_patch.py` 用 Python 标准库 `ctypes` 调用，运算顺序与常数照搬 NumPy 版，结果逐位相同。

## 收益

合成 4K 测试（4 核 Linux，60 帧带 1200px 多频段补丁，快速编码）：Python 部分 CPU 时间约 28 s → 18 s，总耗时约 18.4 s → 13.7 s。输出与 NumPy 路径、与 v2.4 逐字节相同。编码器本身不受影响；多核机器上 NumPy 路径已按行多线程，相对收益可能更小，须实测。

## 构建（每台电脑自行完成）

需要 Rust 工具链（rustc／cargo，MIT OR Apache-2.0），只在构建时使用。安装由使用者自行完成或另行明确授权；不分发、不下载预编译库。

```bash
cd native
cargo build --release
```

Windows 使用 GNU 工具链（`x86_64-pc-windows-gnu`）且目录含中文等非 ASCII 字符时，默认的 MinGW `ld` 可能把输出路径变成 `???` 并报 `cannot open output file ... Invalid argument`。同一官方工具链自带的 `rust-lld` 已实测可用（PowerShell，当前进程内设置，不改系统 PATH）：

```powershell
$rustBin = Join-Path (rustc --print sysroot) 'lib/rustlib/x86_64-pc-windows-gnu/bin'
$env:PATH = "$rustBin;$env:PATH"
$env:RUSTFLAGS = '-C linker=rust-lld.exe'
cargo build --release --locked --offline --manifest-path native/Cargo.toml
```

MSVC 工具链未在中文路径下验证；只装 Rust 而未装 Visual Studio Build Tools／Windows SDK 时，MSVC 目标会在链接阶段缺少 kernel32.lib 等系统库而失败，此时改用上面的 GNU 工具链。

产物位于 `native/target/release/`（Linux `libflow_kernels.so`、macOS `libflow_kernels.dylib`、Windows `flow_kernels.dll`），不入库、不打包。

## 使用

`FLOW_KERNELS`：`auto`（默认，已构建就用）、`native`（必须用，未构建则报错）、`numpy`（不用）。每个进程首次使用前，以随机数据比对全部内核与 NumPy 表达式，任一位不同即报错停止，不会悄悄换用。`doctor` 显示当前状态。改动 `native/src/lib.rs` 或换编译器后重新构建，并跑 `python tests/smoke_test.py`（test_17 比对两种内核的整片输出）。
