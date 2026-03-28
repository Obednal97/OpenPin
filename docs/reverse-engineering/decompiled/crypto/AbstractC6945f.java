package p083ed;

import com.google.crypto.tink.config.internal.TinkFipsUtil;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.security.GeneralSecurityException;
import javax.crypto.AEADBadTagException;

/* JADX INFO: renamed from: ed.f */
/* JADX INFO: loaded from: classes3.dex */
public abstract class AbstractC6945f {

    /* JADX INFO: renamed from: c */
    public static final TinkFipsUtil.AlgorithmFipsCompatibility f20836c = TinkFipsUtil.AlgorithmFipsCompatibility.ALGORITHM_NOT_FIPS;

    /* JADX INFO: renamed from: a */
    public final AbstractC6943d f20837a;

    /* JADX INFO: renamed from: b */
    public final AbstractC6943d f20838b;

    public AbstractC6945f(byte[] bArr) throws GeneralSecurityException {
        if (!f20836c.isCompatible()) {
            throw new GeneralSecurityException("Can not use ChaCha20Poly1305 in FIPS-mode.");
        }
        this.f20837a = mo27009e(bArr, 1);
        this.f20838b = mo27009e(bArr, 0);
    }

    /* JADX INFO: renamed from: d */
    public static byte[] m27010d(byte[] bArr, ByteBuffer byteBuffer) {
        int length = bArr.length % 16 == 0 ? bArr.length : (bArr.length + 16) - (bArr.length % 16);
        int iRemaining = byteBuffer.remaining();
        int i10 = iRemaining % 16;
        int i11 = (i10 == 0 ? iRemaining : (iRemaining + 16) - i10) + length;
        ByteBuffer byteBufferOrder = ByteBuffer.allocate(i11 + 16).order(ByteOrder.LITTLE_ENDIAN);
        byteBufferOrder.put(bArr);
        byteBufferOrder.position(length);
        byteBufferOrder.put(byteBuffer);
        byteBufferOrder.position(i11);
        byteBufferOrder.putLong(bArr.length);
        byteBufferOrder.putLong(iRemaining);
        return byteBufferOrder.array();
    }

    /* JADX INFO: renamed from: a */
    public byte[] mo27007a(ByteBuffer byteBuffer, byte[] bArr, byte[] bArr2) throws GeneralSecurityException {
        if (byteBuffer.remaining() < 16) {
            throw new GeneralSecurityException("ciphertext too short");
        }
        int iPosition = byteBuffer.position();
        byte[] bArr3 = new byte[16];
        byteBuffer.position(byteBuffer.limit() - 16);
        byteBuffer.get(bArr3);
        byteBuffer.position(iPosition);
        byteBuffer.limit(byteBuffer.limit() - 16);
        if (bArr2 == null) {
            bArr2 = new byte[0];
        }
        try {
            AbstractC6948i.m27018f(m27011c(bArr), m27010d(bArr2, byteBuffer), bArr3);
            byteBuffer.position(iPosition);
            return this.f20837a.mo27002c(bArr, byteBuffer);
        } catch (GeneralSecurityException e10) {
            throw new AEADBadTagException(e10.toString());
        }
    }

    /* JADX INFO: renamed from: b */
    public void mo27008b(ByteBuffer byteBuffer, byte[] bArr, byte[] bArr2, byte[] bArr3) throws GeneralSecurityException {
        if (byteBuffer.remaining() < bArr2.length + 16) {
            throw new IllegalArgumentException("Given ByteBuffer output is too small");
        }
        int iPosition = byteBuffer.position();
        this.f20837a.mo27003d(byteBuffer, bArr, bArr2);
        byteBuffer.position(iPosition);
        byteBuffer.limit(byteBuffer.limit() - 16);
        if (bArr3 == null) {
            bArr3 = new byte[0];
        }
        byte[] bArrM27013a = AbstractC6948i.m27013a(m27011c(bArr), m27010d(bArr3, byteBuffer));
        byteBuffer.limit(byteBuffer.limit() + 16);
        byteBuffer.put(bArrM27013a);
    }

    /* JADX INFO: renamed from: c */
    public final byte[] m27011c(byte[] bArr) {
        byte[] bArr2 = new byte[32];
        this.f20838b.m27005a(bArr, 0).get(bArr2);
        return bArr2;
    }

    /* JADX INFO: renamed from: e */
    public abstract AbstractC6943d mo27009e(byte[] bArr, int i10);
}
