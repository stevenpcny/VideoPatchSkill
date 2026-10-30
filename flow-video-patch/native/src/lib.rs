//! Optional per-pixel kernels for flow_patch.py, loaded with ctypes.
//!
//! Each kernel repeats the matching NumPy expression with the same operation order and the same
//! f32/f64 constants (NumPy casts a Python float to the array's float32), so results are
//! bit-identical; flow_patch.py checks this against NumPy before using the library. Rust never
//! fuses multiply-add on its own, which keeps every step an IEEE single operation like NumPy's.
//! Arrays arrive as base pointers plus row strides in elements; pixels within a row are packed.
#![allow(clippy::missing_safety_doc, clippy::too_many_arguments)]

use std::slice;

unsafe fn row<'a, T>(base: *const T, stride: isize, y: usize, len: usize) -> &'a [T] {
    slice::from_raw_parts(base.offset(stride * y as isize), len)
}

unsafe fn row_mut<'a, T>(base: *mut T, stride: isize, y: usize, len: usize) -> &'a mut [T] {
    slice::from_raw_parts_mut(base.offset(stride * y as isize), len)
}

#[inline]
fn to_u8(x: f32) -> u8 {
    // np.clip(np.rint(x), 0, 255).astype(np.uint8)
    x.round_ties_even().clamp(0.0, 255.0) as u8
}

/// flow_patch._yuv_to_bgr
#[no_mangle]
pub unsafe extern "C" fn fk_yuv_to_bgr(
    y: *const u8, ys: isize, u: *const u8, us: isize, v: *const u8, vs: isize,
    out: *mut u8, os: isize, w: usize, h: usize, kr: f64, kb: f64, tv: i32,
) {
    let (cy, cc) = ((255.0f64 / 219.0) as f32, (255.0f64 / 224.0) as f32);
    let (kv, ku) = ((2.0 * (1.0 - kr)) as f32, (2.0 * (1.0 - kb)) as f32);
    let (krf, kbf, kg) = (kr as f32, kb as f32, (1.0 - kr - kb) as f32);
    for j in 0..h {
        let (yr, ur, vr) = (row(y, ys, j, w), row(u, us, j, w), row(v, vs, j, w));
        let o = row_mut(out, os, j, 3 * w);
        for i in 0..w {
            let (mut yy, mut uu, mut vv) = (yr[i] as f32, ur[i] as f32, vr[i] as f32);
            if tv != 0 {
                yy = (yy - 16.0) * cy;
                uu = (uu - 128.0) * cc;
                vv = (vv - 128.0) * cc;
            } else {
                uu -= 128.0;
                vv -= 128.0;
            }
            let r = yy + kv * vv;
            let b = yy + ku * uu;
            let g = (yy - krf * r - kbf * b) / kg;
            o[3 * i] = to_u8(b);
            o[3 * i + 1] = to_u8(g);
            o[3 * i + 2] = to_u8(r);
        }
    }
}

/// flow_patch._bgr_to_yuv420 (w and h even)
#[no_mangle]
pub unsafe extern "C" fn fk_bgr_to_yuv420(
    bgr: *const u8, bs: isize, yo: *mut u8, yos: isize, uo: *mut u8, uos: isize,
    vo: *mut u8, vos: isize, w: usize, h: usize, kr: f64, kb: f64, tv: i32,
) {
    let (krf, kbf, kg) = (kr as f32, kb as f32, (1.0 - kr - kb) as f32);
    let (du, dv) = ((2.0 * (1.0 - kb)) as f32, (2.0 * (1.0 - kr)) as f32);
    let (sy, sc) = ((219.0f64 / 255.0) as f32, (224.0f64 / 255.0) as f32);
    let mut uv = vec![0f32; 4 * w]; // u and v of the two rows of a chroma row
    for j2 in 0..h / 2 {
        for d in 0..2 {
            let j = 2 * j2 + d;
            let src = row(bgr, bs, j, 3 * w);
            let yr = row_mut(yo, yos, j, w);
            for i in 0..w {
                let (b, g, r) = (src[3 * i] as f32, src[3 * i + 1] as f32, src[3 * i + 2] as f32);
                let mut y = krf * r + kg * g + kbf * b;
                let mut u = (b - y) / du;
                let mut v = (r - y) / dv;
                if tv != 0 {
                    y = 16.0 + y * sy;
                    u *= sc;
                    v *= sc;
                }
                yr[i] = to_u8(y);
                uv[d * w + i] = u;
                uv[2 * w + d * w + i] = v;
            }
        }
        let (ur, vr) = (row_mut(uo, uos, j2, w / 2), row_mut(vo, vos, j2, w / 2));
        for (plane, dst) in [(0usize, ur), (2 * w, vr)] {
            for i in 0..w / 2 {
                let (a, b) = (uv[plane + 2 * i], uv[plane + w + 2 * i]);
                let (c, e) = (uv[plane + 2 * i + 1], uv[plane + w + 2 * i + 1]);
                // u[0::2,0::2] + u[1::2,0::2] + u[0::2,1::2] + u[1::2,1::2], then /4 + 128
                dst[i] = to_u8((a + b + c + e) / 4.0 + 128.0);
            }
        }
    }
}

/// Reader.passthrough420 chroma: integer rint(sum/4), ties to even (w and h even)
#[no_mangle]
pub unsafe extern "C" fn fk_chroma_avg(src: *const u8, ss: isize, dst: *mut u8, ds: isize, w: usize, h: usize) {
    for j2 in 0..h / 2 {
        let (a, b) = (row(src, ss, 2 * j2, w), row(src, ss, 2 * j2 + 1, w));
        let o = row_mut(dst, ds, j2, w / 2);
        for i in 0..w / 2 {
            let s = a[2 * i] as u16 + b[2 * i] as u16 + a[2 * i + 1] as u16 + b[2 * i + 1] as u16;
            let (q, r) = (s >> 2, s & 3);
            o[i] = (q + (r > 2 || (r == 2 && q & 1 == 1)) as u16) as u8;
        }
    }
}

/// blend alpha: original*(1-m) + patch*m, float32
#[no_mangle]
pub unsafe extern "C" fn fk_alpha(
    o: *const u8, os: isize, p: *const u8, ps: isize, m: *const f32, ms: isize,
    out: *mut f32, outs: isize, w: usize, h: usize,
) {
    for j in 0..h {
        let (or, pr, mr) = (row(o, os, j, 3 * w), row(p, ps, j, 3 * w), row(m, ms, j, w));
        let dst = row_mut(out, outs, j, 3 * w);
        for i in 0..w {
            let (mm, inv) = (mr[i], 1.0 - mr[i]);
            for c in 0..3 {
                let k = 3 * i + c;
                dst[k] = or[k] as f32 * inv + pr[k] as f32 * mm;
            }
        }
    }
}

/// One multiband level: up(out) + (ga-up(ga))*(1-m) + (gb-up(gb))*m, float32
#[no_mangle]
pub unsafe extern "C" fn fk_level(
    ga: *const f32, gas: isize, ua: *const f32, uas: isize, gb: *const f32, gbs: isize,
    ub: *const f32, ubs: isize, uo: *const f32, uos: isize, gm: *const f32, gms: isize,
    out: *mut f32, outs: isize, w: usize, h: usize,
) {
    for j in 0..h {
        let (gar, uar) = (row(ga, gas, j, 3 * w), row(ua, uas, j, 3 * w));
        let (gbr, ubr) = (row(gb, gbs, j, 3 * w), row(ub, ubs, j, 3 * w));
        let (uor, mr) = (row(uo, uos, j, 3 * w), row(gm, gms, j, w));
        let dst = row_mut(out, outs, j, 3 * w);
        for i in 0..w {
            let (mm, inv) = (mr[i], 1.0 - mr[i]);
            for c in 0..3 {
                let k = 3 * i + c;
                dst[k] = uor[k] + (gar[k] - uar[k]) * inv + (gbr[k] - ubr[k]) * mm;
            }
        }
    }
}

/// blend finish: clip(out,0,255).round() as uint8, original where m == 0
#[no_mangle]
pub unsafe extern "C" fn fk_finish(
    v: *const f32, vs: isize, m: *const f32, ms: isize, o: *const u8, os: isize,
    res: *mut u8, rs: isize, w: usize, h: usize,
) {
    for j in 0..h {
        let (vr, mr, or) = (row(v, vs, j, 3 * w), row(m, ms, j, w), row(o, os, j, 3 * w));
        let dst = row_mut(res, rs, j, 3 * w);
        for i in 0..w {
            for c in 0..3 {
                let k = 3 * i + c;
                dst[k] = if mr[i] == 0.0 { or[k] } else { vr[k].clamp(0.0, 255.0).round_ties_even() as u8 };
            }
        }
    }
}

/// Scaled paste-back, step 1: merged - original in float64
#[no_mangle]
pub unsafe extern "C" fn fk_diff64(
    a: *const u8, as_: isize, b: *const u8, bs: isize, out: *mut f64, outs: isize, w: usize, h: usize,
) {
    for j in 0..h {
        let (ar, br) = (row(a, as_, j, 3 * w), row(b, bs, j, 3 * w));
        let dst = row_mut(out, outs, j, 3 * w);
        for k in 0..3 * w {
            dst[k] = ar[k] as f64 - br[k] as f64;
        }
    }
}

/// Scaled paste-back, step 2: clip(roi + delta*support, 0, 255).round() as uint8
#[no_mangle]
pub unsafe extern "C" fn fk_apply64(
    roi: *const u8, rs: isize, d: *const f64, ds: isize, sup: *const u8, ss: isize,
    out: *mut u8, os: isize, w: usize, h: usize,
) {
    for j in 0..h {
        let (rr, dr, sr) = (row(roi, rs, j, 3 * w), row(d, ds, j, 3 * w), row(sup, ss, j, w));
        let dst = row_mut(out, os, j, 3 * w);
        for i in 0..w {
            for c in 0..3 {
                let k = 3 * i + c;
                let delta = if sr[i] != 0 { dr[k] } else { 0.0 };
                dst[k] = (rr[k] as f64 + delta).clamp(0.0, 255.0).round_ties_even() as u8;
            }
        }
    }
}

/// Build/ABI check for the loader.
#[no_mangle]
pub extern "C" fn fk_abi_version() -> u32 {
    1
}
