package p083ed;

import java.nio.ByteBuffer;
import java.security.GeneralSecurityException;
import java.util.Arrays;

/* JADX INFO: renamed from: ed.g */
/* JADX INFO: loaded from: classes3.dex */
public class C6946g extends AbstractC6943d {
    public C6946g(byte[] bArr, int i10) {
        super(bArr, i10);
    }

    /* JADX INFO: renamed from: g */
    public static int[] m27012g(int[] iArr, int[] iArr2) {
        int[] iArr3 = new int[16];
        AbstractC6940a.m26993c(iArr3, iArr);
        iArr3[12] = iArr2[0];
        iArr3[13] = iArr2[1];
        iArr3[14] = iArr2[2];
        iArr3[15] = iArr2[3];
        AbstractC6940a.m26994d(iArr3);
        iArr3[4] = iArr3[12];
        iArr3[5] = iArr3[13];
        iArr3[6] = iArr3[14];
        iArr3[7] = iArr3[15];
        return Arrays.copyOf(iArr3, 8);
    }

    @Override // p083ed.AbstractC6943d
    /* JADX INFO: renamed from: b */
    public int[] mo27001b(int[] iArr, int i10) {
        if (iArr.length != mo27004e() / 4) {
            throw new IllegalArgumentException(String.format("XChaCha20 uses 192-bit nonces, but got a %d-bit nonce", Integer.valueOf(iArr.length * 32)));
        }
        int[] iArr2 = new int[16];
        AbstractC6940a.m26993c(iArr2, m27012g(this.f20834a, iArr));
        iArr2[12] = i10;
        iArr2[13] = 0;
        iArr2[14] = iArr[4];
        iArr2[15] = iArr[5];
        return iArr2;
    }

    @Override // p083ed.AbstractC6943d
    /* JADX INFO: renamed from: c */
    public /* bridge */ /* synthetic */ byte[] mo27002c(byte[] bArr, ByteBuffer byteBuffer) {
        return super.mo27002c(bArr, byteBuffer);
    }

    @Override // p083ed.AbstractC6943d
    /* JADX INFO: renamed from: d */
    public /* bridge */ /* synthetic */ void mo27003d(ByteBuffer byteBuffer, byte[] bArr, byte[] bArr2) throws GeneralSecurityException {
        super.mo27003d(byteBuffer, bArr, bArr2);
    }

    @Override // p083ed.AbstractC6943d
    /* JADX INFO: renamed from: e */
    public int mo27004e() {
        return 24;
    }
}
