unsafe fn fk_chroma_avg_avx2(src: *const u8, ss: isize, dst: *mut u8, ds: isize, w: usize, h: usize) {
    #[cfg(target_arch="x86_64")]
    use std::arch::x86_64::*;
    #[cfg(target_arch="x86")]
    use std::arch::x86::*;
    let ones8 = _mm256_set1_epi8(1);
    let one = _mm256_set1_epi16(1);
    let two = _mm256_set1_epi16(2);
    let three = _mm256_set1_epi16(3);
    for j in 0..h/2 {
        let a = src.offset(ss*(2*j) as isize);
        let b = src.offset(ss*(2*j+1) as isize);
        let out = dst.offset(ds*j as isize);
        let mut x=0;
        while x+32<=w {
            let va=_mm256_loadu_si256(a.add(x) as *const __m256i);
            let vb=_mm256_loadu_si256(b.add(x) as *const __m256i);
            // Adjacent byte pairs sum to <=510; the two-row sum is <=1020.
            let s=_mm256_add_epi16(_mm256_maddubs_epi16(va,ones8),_mm256_maddubs_epi16(vb,ones8));
            let q=_mm256_srli_epi16::<2>(s);
            let r=_mm256_and_si256(s,three);
            let odd=_mm256_cmpeq_epi16(_mm256_and_si256(q,one),one);
            let inc=_mm256_and_si256(one,_mm256_or_si256(_mm256_cmpgt_epi16(r,two),_mm256_and_si256(_mm256_cmpeq_epi16(r,two),odd)));
            let rounded=_mm256_add_epi16(q,inc);
            let packed=_mm_packus_epi16(_mm256_castsi256_si128(rounded),_mm256_extracti128_si256::<1>(rounded));
            _mm_storeu_si128(out.add(x/2) as *mut __m128i,packed);
            x+=32;
        }
        while x<w {
            let s=*a.add(x) as u16+*a.add(x+1) as u16+*b.add(x) as u16+*b.add(x+1) as u16;
            let (q,r)=(s>>2,s&3);
            *out.add(x/2)=(q+(r>2 || (r==2 && q&1==1)) as u16) as u8;
            x+=2;
        }
    }
}
