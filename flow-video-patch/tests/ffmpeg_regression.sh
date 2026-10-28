#!/bin/sh
# Synthetic fixtures only; no Python, network, model or HEVC encoder required.
set -eu
task_ffmpeg=${FFMPEG_BIN:-ffmpeg}
task_ffprobe=${FFPROBE_BIN:-ffprobe}
command -v "$task_ffmpeg" >/dev/null
command -v "$task_ffprobe" >/dev/null
task_tmp=$(mktemp -d "${TMPDIR:-/tmp}/flow-patch-ffmpeg.XXXXXX")
trap 'test -n "$task_tmp" && rm -rf "$task_tmp"' EXIT
ff() { "$task_ffmpeg" -n -hide_banner -v error -nostdin -filter_complex_threads 1 "$@"; }
pass() { echo "PASS $1"; }

# 1: Explicit gray alpha has a full core and zero exterior.
ff -f lavfi -i "nullsrc=s=32x32:r=24,format=gray,geq=lum='255*clip((1-pow((X-16)/10,2)-pow((Y-16)/10,2))*4,0,1)',split[a][b];[a]crop=1:1:16:16[ac];[b]crop=1:1:0:0[bc];[ac][bc]hstack" \
  -frames:v 1 -pix_fmt gray -f rawvideo "$task_tmp/alpha.raw"
task_bytes=$(od -An -v -tu1 "$task_tmp/alpha.raw" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')
test "$task_bytes" = '255 0'
pass alpha_core_and_exterior

# 2: Opaque 10bit overlay reproduces the replacement, while alpha235 does not.
for task_alpha in 255 235; do
  ff -f lavfi -i 'color=red:s=16x16:r=24' \
    -f lavfi -i 'color=blue:s=16x16:r=24' \
    -f lavfi -i "nullsrc=s=16x16:r=24,format=gray,geq=lum=$task_alpha" \
    -filter_complex '[0:v]format=yuv420p10le[base];[1:v]format=yuv420p10le,split[p][ref];[p][2:v]alphamerge,format=yuva420p10le[pa];[base][pa]overlay=format=yuv420p10:alpha=straight[out]' \
    -map '[out]' -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt yuv420p10le -f framemd5 "$task_tmp/overlay$task_alpha.md5" \
    -map '[ref]' -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt yuv420p10le -f framemd5 "$task_tmp/reference$task_alpha.md5"
done
cmp "$task_tmp/overlay255.md5" "$task_tmp/reference255.md5"
if cmp -s "$task_tmp/overlay235.md5" "$task_tmp/reference235.md5"; then
  echo 'FAIL undercoverage negative control' >&2; exit 1
fi
pass opaque_overlay_and_undercoverage_negative_control

# 3: Signed RGB16 arithmetic saturates instead of wrapping into colored specks.
ff -f lavfi -i 'nullsrc=s=16x16:r=24,format=gbrp16le,geq=r=60000:g=10000:b=30000' \
  -f lavfi -i 'nullsrc=s=16x16:r=24,format=gbrp16le,geq=r=30000:g=10000:b=10000' \
  -f lavfi -i 'nullsrc=s=16x16:r=24,format=gbrp16le,geq=r=65535:g=0:b=4464' \
  -filter_complex "[0:v][1:v]blend=all_expr='clip((A-32768)*2+B,0,65535)'[actual]" \
  -map '[actual]' -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt gbrp16le -f rawvideo "$task_tmp/clipped.raw" \
  -map 2:v -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt gbrp16le -f rawvideo "$task_tmp/expected.raw"
cmp "$task_tmp/clipped.raw" "$task_tmp/expected.raw"
pass rgb16_saturation

# 4: A local operation changes its center but not pixels outside its physical box.
ff -f lavfi -i 'testsrc2=s=64x64:r=24:d=0.5' \
  -f lavfi -i "nullsrc=s=32x32:r=24:d=0.5,format=gray,geq=lum='255*clip((1-pow((X-16)/10,2)-pow((Y-16)/10,2))*4,0,1)'" \
  -filter_complex '[0:v]format=yuv420p10le,split=3[base][patch][control];[patch]crop=32:32:16:16,gblur=sigma=1.5:planes=1[p];[p][1:v]alphamerge,format=yuva420p10le[a];[base][a]overlay=x=16:y=16:format=yuv420p10:alpha=straight,split[m0][m1];[control]split[c0][c1];[m0]drawbox=x=16:y=16:w=32:h=32:color=black:t=fill, setpts=N/(24*TB)[outside];[c0]drawbox=x=16:y=16:w=32:h=32:color=black:t=fill, setpts=N/(24*TB)[original_outside];[m1]crop=16:16:24:24[inside];[c1]crop=16:16:24:24[original_inside]' \
  -map '[outside]' -frames:v 12 -an -c:v rawvideo -threads 1 -pix_fmt yuv420p10le -f framemd5 "$task_tmp/outside.md5" \
  -map '[original_outside]' -frames:v 12 -an -c:v rawvideo -threads 1 -pix_fmt yuv420p10le -f framemd5 "$task_tmp/original_outside.md5" \
  -map '[inside]' -frames:v 12 -an -c:v rawvideo -threads 1 -pix_fmt yuv420p10le -f framemd5 "$task_tmp/inside.md5" \
  -map '[original_inside]' -frames:v 12 -an -c:v rawvideo -threads 1 -pix_fmt yuv420p10le -f framemd5 "$task_tmp/original_inside.md5"
cmp "$task_tmp/outside.md5" "$task_tmp/original_outside.md5"
for task_md5 in outside original_outside inside original_inside; do
  task_count=$(awk '!/^#/ && NF {n++} END {print n+0}' "$task_tmp/$task_md5.md5")
  test "$task_count" = 12
done
if cmp -s "$task_tmp/inside.md5" "$task_tmp/original_inside.md5"; then
  echo 'FAIL local operation had no effect' >&2; exit 1
fi
pass local_effect_and_protected_exterior

# 5: A known, explicitly selected time transform gives the requested frame count.
# This tests resampling, not automatic diagnosis of real returned footage.
ff -f lavfi -i 'testsrc2=s=64x64:r=24:d=1' \
  -vf 'setpts=PTS*5/4,fps=24:start_time=0:round=near,trim=end_frame=30' \
  -an -c:v libx264 -threads 1 -preset ultrafast -crf 18 -pix_fmt yuv420p "$task_tmp/retimed.mp4"
task_frames=$("$task_ffprobe" -v error -protocol_whitelist file -count_frames -select_streams v:0 \
  -show_entries stream=nb_read_frames -of default=noprint_wrappers=1:nokey=1 "$task_tmp/retimed.mp4")
test "$task_frames" = 30
pass explicit_retime_frame_count

# 6: The protected head uses no old spatial-lighting branch, unlike the body.
ff -f lavfi -i 'nullsrc=s=32x32:r=24,format=gbrp16le,geq=r=48000:g=36000:b=28000' \
  -f lavfi -i 'nullsrc=s=32x32:r=24,format=gbrp16le,geq=r=8000:g=7000:b=6000' \
  -filter_complex "[0:v]split[clean][cr];[1:v]split[old][orr];[clean][old]blend=all_expr='clip(A*clip((24-Y)/8,0,1)+B*(1-clip((24-Y)/8,0,1)),0,65535)',split[mh][mb];[mh]crop=32:16:0:0[head];[cr]crop=32:16:0:0[head_ref];[mb]crop=32:8:0:24[body];[orr]crop=32:8:0:24[body_ref]" \
  -map '[head]' -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt gbrp16le -f rawvideo "$task_tmp/head.raw" \
  -map '[head_ref]' -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt gbrp16le -f rawvideo "$task_tmp/head_ref.raw" \
  -map '[body]' -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt gbrp16le -f rawvideo "$task_tmp/body.raw" \
  -map '[body_ref]' -frames:v 1 -an -c:v rawvideo -threads 1 -pix_fmt gbrp16le -f rawvideo "$task_tmp/body_ref.raw"
cmp "$task_tmp/head.raw" "$task_tmp/head_ref.raw"
cmp "$task_tmp/body.raw" "$task_tmp/body_ref.raw"
pass protected_head_has_no_old_lighting

echo 'RESULT 6 passed; synthetic FFmpeg invariants only, not visual acceptance.'
