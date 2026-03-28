package p083ed;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.IntBuffer;

/* JADX INFO: renamed from: ed.a */
/* JADX INFO: loaded from: classes3.dex */
public abstract class AbstractC6940a {

    /* JADX INFO: renamed from: a */
    public static final int[] f20829a = m26995e(new byte[]{101, 120, 112, 97, 110, 100, 32, 51, 50, 45, 98, 121, 116, 101, 32, 107});

    /* JADX INFO: renamed from: a */
    public static void m26991a(int[] iArr, int i10, int i11, int i12, int i13) {
        int i14 = iArr[i10] + iArr[i11];
        iArr[i10] = i14;
        int iM26992b = m26992b(i14 ^ iArr[i13], 16);
        iArr[i13] = iM26992b;
        int i15 = iArr[i12] + iM26992b;
        iArr[i12] = i15;
        int iM26992b2 = m26992b(iArr[i11] ^ i15, 12);
        iArr[i11] = iM26992b2;
        int i16 = iArr[i10] + iM26992b2;
        iArr[i10] = i16;
        int iM26992b3 = m26992b(iArr[i13] ^ i16, 8);
        iArr[i13] = iM26992b3;
        int i17 = iArr[i12] + iM26992b3;
        iArr[i12] = i17;
        iArr[i11] = m26992b(iArr[i11] ^ i17, 7);
    }

    /* JADX INFO: renamed from: b */
    public static int m26992b(int i10, int i11) {
        return (i10 >>> (-i11)) | (i10 << i11);
    }

    /* JADX INFO: renamed from: c */
    public static void m26993c(int[] iArr, int[] iArr2) {
        int[] iArr3 = f20829a;
        System.arraycopy(iArr3, 0, iArr, 0, iArr3.length);
        System.arraycopy(iArr2, 0, iArr, iArr3.length, 8);
    }

    /* JADX INFO: renamed from: d */
    public static void m26994d(int[] iArr) {
        for (int i10 = 0; i10 < 10; i10++) {
            m26991a(iArr, 0, 4, 8, 12);
            m26991a(iArr, 1, 5, 9, 13);
            m26991a(iArr, 2, 6, 10, 14);
            m26991a(iArr, 3, 7, 11, 15);
            m26991a(iArr, 0, 5, 10, 15);
            m26991a(iArr, 1, 6, 11, 12);
            m26991a(iArr, 2, 7, 8, 13);
            m26991a(iArr, 3, 4, 9, 14);
        }
    }

    /* JADX INFO: renamed from: e */
    public static int[] m26995e(byte[] bArr) {
        IntBuffer intBufferAsIntBuffer = ByteBuffer.wrap(bArr).order(ByteOrder.LITTLE_ENDIAN).asIntBuffer();
        int[] iArr = new int[intBufferAsIntBuffer.remaining()];
        intBufferAsIntBuffer.get(iArr);
        return iArr;
    }
}
