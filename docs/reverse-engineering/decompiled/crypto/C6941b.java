package p083ed;

import com.google.crypto.tink.config.internal.TinkFipsUtil;
import com.google.crypto.tink.internal.AbstractC5653y;
import com.google.crypto.tink.subtle.EngineFactory;
import com.google.crypto.tink.subtle.Validators;
import java.nio.ByteBuffer;
import java.security.GeneralSecurityException;
import java.security.spec.AlgorithmParameterSpec;
import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/* JADX INFO: renamed from: ed.b */
/* JADX INFO: loaded from: classes3.dex */
public final class C6941b {

    /* JADX INFO: renamed from: c */
    public static final TinkFipsUtil.AlgorithmFipsCompatibility f20830c = TinkFipsUtil.AlgorithmFipsCompatibility.ALGORITHM_REQUIRES_BORINGCRYPTO;

    /* JADX INFO: renamed from: d */
    public static final ThreadLocal f20831d = new a();

    /* JADX INFO: renamed from: a */
    public final SecretKey f20832a;

    /* JADX INFO: renamed from: b */
    public final boolean f20833b;

    /* JADX INFO: renamed from: ed.b$a */
    public class a extends ThreadLocal {
        @Override // java.lang.ThreadLocal
        /* JADX INFO: renamed from: a, reason: merged with bridge method [inline-methods] */
        public Cipher initialValue() {
            try {
                return EngineFactory.CIPHER.getInstance("AES/GCM/NoPadding");
            } catch (GeneralSecurityException e10) {
                throw new IllegalStateException(e10);
            }
        }
    }

    public C6941b(byte[] bArr, boolean z10) throws GeneralSecurityException {
        if (!f20830c.isCompatible()) {
            throw new GeneralSecurityException("Can not use AES-GCM in FIPS-mode, as BoringCrypto module is not available.");
        }
        Validators.validateAesKeySize(bArr.length);
        this.f20832a = new SecretKeySpec(bArr, "AES");
        this.f20833b = z10;
    }

    /* JADX INFO: renamed from: c */
    public static AlgorithmParameterSpec m26996c(byte[] bArr) {
        return m26997d(bArr, 0, bArr.length);
    }

    /* JADX INFO: renamed from: d */
    public static AlgorithmParameterSpec m26997d(byte[] bArr, int i10, int i11) {
        Integer numM21447a = AbstractC5653y.m21447a();
        return (numM21447a == null || numM21447a.intValue() > 19) ? new GCMParameterSpec(128, bArr, i10, i11) : new IvParameterSpec(bArr, i10, i11);
    }

    /* JADX INFO: renamed from: a */
    public byte[] m26998a(byte[] bArr, byte[] bArr2, byte[] bArr3) throws GeneralSecurityException {
        if (bArr.length != 12) {
            throw new GeneralSecurityException("iv is wrong size");
        }
        boolean z10 = this.f20833b;
        if (bArr2.length < (z10 ? 28 : 16)) {
            throw new GeneralSecurityException("ciphertext too short");
        }
        if (z10 && !ByteBuffer.wrap(bArr).equals(ByteBuffer.wrap(bArr2, 0, 12))) {
            throw new GeneralSecurityException("iv does not match prepended iv");
        }
        AlgorithmParameterSpec algorithmParameterSpecM26996c = m26996c(bArr);
        ThreadLocal threadLocal = f20831d;
        ((Cipher) threadLocal.get()).init(2, this.f20832a, algorithmParameterSpecM26996c);
        if (bArr3 != null && bArr3.length != 0) {
            ((Cipher) threadLocal.get()).updateAAD(bArr3);
        }
        boolean z11 = this.f20833b;
        return ((Cipher) threadLocal.get()).doFinal(bArr2, z11 ? 12 : 0, z11 ? bArr2.length - 12 : bArr2.length);
    }

    /* JADX INFO: renamed from: b */
    public byte[] m26999b(byte[] bArr, byte[] bArr2, byte[] bArr3) throws GeneralSecurityException {
        if (bArr.length != 12) {
            throw new GeneralSecurityException("iv is wrong size");
        }
        if (bArr2.length > 2147483619) {
            throw new GeneralSecurityException("plaintext too long");
        }
        boolean z10 = this.f20833b;
        byte[] bArr4 = new byte[z10 ? bArr2.length + 28 : bArr2.length + 16];
        if (z10) {
            System.arraycopy(bArr, 0, bArr4, 0, 12);
        }
        AlgorithmParameterSpec algorithmParameterSpecM26996c = m26996c(bArr);
        ThreadLocal threadLocal = f20831d;
        ((Cipher) threadLocal.get()).init(1, this.f20832a, algorithmParameterSpecM26996c);
        if (bArr3 != null && bArr3.length != 0) {
            ((Cipher) threadLocal.get()).updateAAD(bArr3);
        }
        int iDoFinal = ((Cipher) threadLocal.get()).doFinal(bArr2, 0, bArr2.length, bArr4, this.f20833b ? 12 : 0);
        if (iDoFinal == bArr2.length + 16) {
            return bArr4;
        }
        throw new GeneralSecurityException(String.format("encryption failed; GCM tag must be %s bytes, but got only %s bytes", 16, Integer.valueOf(iDoFinal - bArr2.length)));
    }
}
