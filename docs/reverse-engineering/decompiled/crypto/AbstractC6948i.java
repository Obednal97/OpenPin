package p083ed;

import com.google.crypto.tink.subtle.Bytes;
import io.flutter.embedding.android.KeyboardMap;
import java.security.GeneralSecurityException;
import java.util.Arrays;

/* JADX INFO: renamed from: ed.i */
/* JADX INFO: loaded from: classes3.dex */
public abstract class AbstractC6948i {
    /* JADX INFO: renamed from: a */
    public static byte[] m27013a(byte[] bArr, byte[] bArr2) {
        if (bArr.length != 32) {
            throw new IllegalArgumentException("The key length in bytes must be 32.");
        }
        long jM27015c = m27015c(bArr, 0, 0) & 67108863;
        int i10 = 3;
        int i11 = 2;
        long jM27015c2 = m27015c(bArr, 3, 2) & 67108611;
        long jM27015c3 = m27015c(bArr, 6, 4) & 67092735;
        long jM27015c4 = m27015c(bArr, 9, 6) & 66076671;
        long jM27015c5 = m27015c(bArr, 12, 8) & 1048575;
        long j10 = jM27015c2 * 5;
        long j11 = jM27015c3 * 5;
        long j12 = jM27015c4 * 5;
        long j13 = jM27015c5 * 5;
        byte[] bArr3 = new byte[17];
        long j14 = 0;
        int i12 = 0;
        long j15 = 0;
        long j16 = 0;
        long j17 = 0;
        long j18 = 0;
        while (i12 < bArr2.length) {
            m27014b(bArr3, bArr2, i12);
            long jM27015c6 = j18 + m27015c(bArr3, 0, 0);
            long jM27015c7 = j14 + m27015c(bArr3, i10, i11);
            long jM27015c8 = j15 + m27015c(bArr3, 6, 4);
            long jM27015c9 = j16 + m27015c(bArr3, 9, 6);
            long j19 = jM27015c2;
            long jM27015c10 = j17 + (m27015c(bArr3, 12, 8) | ((long) (bArr3[16] << 24)));
            long j20 = (jM27015c6 * jM27015c) + (jM27015c7 * j13) + (jM27015c8 * j12) + (jM27015c9 * j11) + (jM27015c10 * j10);
            long j21 = (jM27015c6 * j19) + (jM27015c7 * jM27015c) + (jM27015c8 * j13) + (jM27015c9 * j12) + (jM27015c10 * j11);
            long j22 = (jM27015c6 * jM27015c3) + (jM27015c7 * j19) + (jM27015c8 * jM27015c) + (jM27015c9 * j13) + (jM27015c10 * j12);
            long j23 = (jM27015c6 * jM27015c4) + (jM27015c7 * jM27015c3) + (jM27015c8 * j19) + (jM27015c9 * jM27015c) + (jM27015c10 * j13);
            long j24 = (jM27015c6 * jM27015c5) + (jM27015c7 * jM27015c4) + (jM27015c8 * jM27015c3) + (jM27015c9 * j19) + (jM27015c10 * jM27015c);
            long j25 = j21 + (j20 >> 26);
            long j26 = j22 + (j25 >> 26);
            j15 = j26 & 67108863;
            long j27 = j23 + (j26 >> 26);
            j16 = j27 & 67108863;
            long j28 = j24 + (j27 >> 26);
            j17 = j28 & 67108863;
            long j29 = (j20 & 67108863) + ((j28 >> 26) * 5);
            j18 = j29 & 67108863;
            j14 = (j25 & 67108863) + (j29 >> 26);
            i12 += 16;
            jM27015c2 = j19;
            i10 = 3;
            i11 = 2;
        }
        long j30 = j15 + (j14 >> 26);
        long j31 = j30 & 67108863;
        long j32 = j16 + (j30 >> 26);
        long j33 = j32 & 67108863;
        long j34 = j17 + (j32 >> 26);
        long j35 = j34 & 67108863;
        long j36 = j18 + ((j34 >> 26) * 5);
        long j37 = j36 & 67108863;
        long j38 = (j14 & 67108863) + (j36 >> 26);
        long j39 = j37 + 5;
        long j40 = j39 & 67108863;
        long j41 = (j39 >> 26) + j38;
        long j42 = j31 + (j41 >> 26);
        long j43 = j33 + (j42 >> 26);
        long j44 = j43 & 67108863;
        long j45 = (j35 + (j43 >> 26)) - 67108864;
        long j46 = j45 >> 63;
        long j47 = j37 & j46;
        long j48 = j38 & j46;
        long j49 = j31 & j46;
        long j50 = j33 & j46;
        long j51 = j35 & j46;
        long j52 = ~j46;
        long j53 = (j41 & 67108863 & j52) | j48;
        long j54 = (j42 & 67108863 & j52) | j49;
        long j55 = (j44 & j52) | j50;
        long j56 = j51 | (j45 & j52);
        long j57 = (j47 | (j40 & j52) | (j53 << 26)) & KeyboardMap.kValueMask;
        long j58 = ((j53 >> 6) | (j54 << 20)) & KeyboardMap.kValueMask;
        long j59 = ((j54 >> 12) | (j55 << 14)) & KeyboardMap.kValueMask;
        long j60 = ((j55 >> 18) | (j56 << 8)) & KeyboardMap.kValueMask;
        long jM27016d = j57 + m27016d(bArr, 16);
        long j61 = jM27016d & KeyboardMap.kValueMask;
        long jM27016d2 = j58 + m27016d(bArr, 20) + (jM27016d >> 32);
        long j62 = jM27016d2 & KeyboardMap.kValueMask;
        long jM27016d3 = j59 + m27016d(bArr, 24) + (jM27016d2 >> 32);
        long j63 = jM27016d3 & KeyboardMap.kValueMask;
        long jM27016d4 = (j60 + m27016d(bArr, 28) + (jM27016d3 >> 32)) & KeyboardMap.kValueMask;
        byte[] bArr4 = new byte[16];
        m27017e(bArr4, j61, 0);
        m27017e(bArr4, j62, 4);
        m27017e(bArr4, j63, 8);
        m27017e(bArr4, jM27016d4, 12);
        return bArr4;
    }

    /* JADX INFO: renamed from: b */
    public static void m27014b(byte[] bArr, byte[] bArr2, int i10) {
        int iMin = Math.min(16, bArr2.length - i10);
        System.arraycopy(bArr2, i10, bArr, 0, iMin);
        bArr[iMin] = 1;
        if (iMin != 16) {
            Arrays.fill(bArr, iMin + 1, bArr.length, (byte) 0);
        }
    }

    /* JADX INFO: renamed from: c */
    public static long m27015c(byte[] bArr, int i10, int i11) {
        return (m27016d(bArr, i10) >> i11) & 67108863;
    }

    /* JADX INFO: renamed from: d */
    public static long m27016d(byte[] bArr, int i10) {
        return ((long) (((bArr[i10 + 3] & 255) << 24) | (bArr[i10] & 255) | ((bArr[i10 + 1] & 255) << 8) | ((bArr[i10 + 2] & 255) << 16))) & KeyboardMap.kValueMask;
    }

    /* JADX INFO: renamed from: e */
    public static void m27017e(byte[] bArr, long j10, int i10) {
        int i11 = 0;
        while (i11 < 4) {
            bArr[i10 + i11] = (byte) (255 & j10);
            i11++;
            j10 >>= 8;
        }
    }

    /* JADX INFO: renamed from: f */
    public static void m27018f(byte[] bArr, byte[] bArr2, byte[] bArr3) throws GeneralSecurityException {
        if (!Bytes.equal(m27013a(bArr, bArr2), bArr3)) {
            throw new GeneralSecurityException("invalid MAC");
        }
    }
}
