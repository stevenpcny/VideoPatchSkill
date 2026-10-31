// Development-only extension appended to native/src/lib.rs for measurement.
// Same integer ties-to-even operation; combines Y copy and U/V averaging.
#[no_mangle]
pub unsafe extern "C" fn fk_passthrough420(
    y: *const u8, ys: isize, u: *const u8, us: isize, v: *const u8, vs: isize,
    yo: *mut u8, yos: isize, uo: *mut u8, uos: isize, vo: *mut u8, vos: isize,
    w: usize, h: usize,
) {
    for j in 0..h / 2 {
        row_mut(yo, yos, 2*j, w).copy_from_slice(row(y, ys, 2*j, w));
        row_mut(yo, yos, 2*j+1, w).copy_from_slice(row(y, ys, 2*j+1, w));
        let (ua, ub) = (row(u,us,2*j,w), row(u,us,2*j+1,w));
        let (va, vb) = (row(v,vs,2*j,w), row(v,vs,2*j+1,w));
        let (ud, vd) = (row_mut(uo,uos,j,w/2), row_mut(vo,vos,j,w/2));
        for i in 0..w/2 {
            let su = ua[2*i] as u16 + ub[2*i] as u16 + ua[2*i+1] as u16 + ub[2*i+1] as u16;
            let sv = va[2*i] as u16 + vb[2*i] as u16 + va[2*i+1] as u16 + vb[2*i+1] as u16;
            let (qu,ru,qv,rv) = (su>>2,su&3,sv>>2,sv&3);
            ud[i] = (qu + (ru>2 || (ru==2 && qu&1==1)) as u16) as u8;
            vd[i] = (qv + (rv>2 || (rv==2 && qv&1==1)) as u16) as u8;
        }
    }
}
