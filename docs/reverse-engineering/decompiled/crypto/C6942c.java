package p083ed;

import java.nio.ByteBuffer;
import java.security.GeneralSecurityException;

/* JADX INFO: renamed from: ed.c */
/* JADX INFO: loaded from: classes3.dex */
public class C6942c extends AbstractC6943d {
    public C6942c(byte[] bArr, int i10) {
        super(bArr, i10);
    }

    @Override // p083ed.AbstractC6943d
    /* JADX INFO: renamed from: b */
    public int[] mo27001b(int[] iArr, int i10) {
        if (iArr.length != mo27004e() / 4) {
            throw new IllegalArgumentException(String.format("ChaCha20 uses 96-bit nonces, but got a %d-bit nonce", Integer.valueOf(iArr.length * 32)));
        }
        int[] iArr2 = new int[16];
        AbstractC6940a.m26993c(iArr2, this.f20834a);
        iArr2[12] = i10;
        System.arraycopy(iArr, 0, iArr2, 13, iArr.length);
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
        return 12;
    }
}
