package p083ed;

import com.google.crypto.tink.subtle.Bytes;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.security.GeneralSecurityException;
import java.security.InvalidKeyException;

/* JADX INFO: renamed from: ed.d */
/* JADX INFO: loaded from: classes3.dex */
public abstract class AbstractC6943d {

    /* JADX INFO: renamed from: a */
    public int[] f20834a;

    /* JADX INFO: renamed from: b */
    public final int f20835b;

    public AbstractC6943d(byte[] bArr, int i10) throws InvalidKeyException {
        if (bArr.length != 32) {
            throw new InvalidKeyException("The key length in bytes must be 32.");
        }
        this.f20834a = AbstractC6940a.m26995e(bArr);
        this.f20835b = i10;
    }

    /* JADX INFO: renamed from: a */
    public ByteBuffer m27005a(byte[] bArr, int i10) {
        int[] iArrMo27001b = mo27001b(AbstractC6940a.m26995e(bArr), i10);
        int[] iArr = (int[]) iArrMo27001b.clone();
        AbstractC6940a.m26994d(iArr);
        for (int i11 = 0; i11 < iArrMo27001b.length; i11++) {
            iArrMo27001b[i11] = iArrMo27001b[i11] + iArr[i11];
        }
        ByteBuffer byteBufferOrder = ByteBuffer.allocate(64).order(ByteOrder.LITTLE_ENDIAN);
        byteBufferOrder.asIntBuffer().put(iArrMo27001b, 0, 16);
        return byteBufferOrder;
    }

    /* JADX INFO: renamed from: b */
    public abstract int[] mo27001b(int[] iArr, int i10);

    /* JADX INFO: renamed from: c */
    public byte[] mo27002c(byte[] bArr, ByteBuffer byteBuffer) throws GeneralSecurityException {
        ByteBuffer byteBufferAllocate = ByteBuffer.allocate(byteBuffer.remaining());
        m27006f(bArr, byteBufferAllocate, byteBuffer);
        return byteBufferAllocate.array();
    }

    /* JADX INFO: renamed from: d */
    public void mo27003d(ByteBuffer byteBuffer, byte[] bArr, byte[] bArr2) throws GeneralSecurityException {
        if (byteBuffer.remaining() < bArr2.length) {
            throw new IllegalArgumentException("Given ByteBuffer output is too small");
        }
        m27006f(bArr, byteBuffer, ByteBuffer.wrap(bArr2));
    }

    /* JADX INFO: renamed from: e */
    public abstract int mo27004e();

    /* JADX INFO: renamed from: f */
    public final void m27006f(byte[] bArr, ByteBuffer byteBuffer, ByteBuffer byteBuffer2) throws GeneralSecurityException {
        if (bArr.length != mo27004e()) {
            throw new GeneralSecurityException("The nonce length (in bytes) must be " + mo27004e());
        }
        int iRemaining = byteBuffer2.remaining();
        int i10 = iRemaining / 64;
        int i11 = i10 + 1;
        for (int i12 = 0; i12 < i11; i12++) {
            ByteBuffer byteBufferM27005a = m27005a(bArr, this.f20835b + i12);
            if (i12 == i10) {
                Bytes.xor(byteBuffer, byteBuffer2, byteBufferM27005a, iRemaining % 64);
            } else {
                Bytes.xor(byteBuffer, byteBuffer2, byteBufferM27005a, 64);
            }
        }
    }
}
