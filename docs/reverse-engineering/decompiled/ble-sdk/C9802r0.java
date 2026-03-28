package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import io.flutter.embedding.android.KeyboardMap;
import kotlin.jvm.internal.AbstractC8694o;

/* JADX INFO: renamed from: oh.r0 */
/* JADX INFO: loaded from: classes3.dex */
public final class C9802r0 {

    /* JADX INFO: renamed from: a */
    public final int f27998a;

    /* JADX INFO: renamed from: b */
    public final int f27999b;

    /* JADX INFO: renamed from: c */
    public final long f28000c;

    /* JADX INFO: renamed from: d */
    public final byte[] f28001d;

    public C9802r0(byte[] data, int i10) {
        AbstractC8694o.m32338e(data, "data");
        this.f27998a = i10;
        this.f27999b = TntBleCommUtils.m26098b().m26108k(data, 1);
        this.f28000c = TntBleCommUtils.m26098b().m26111n(data, 2) & KeyboardMap.kValueMask;
        int iMin = (int) Math.min(TntBleCommUtils.m26098b().m26108k(data, 6), data.length - 7);
        byte[] bArr = new byte[iMin];
        this.f28001d = bArr;
        System.arraycopy(data, 7, bArr, 0, iMin);
    }

    /* JADX INFO: renamed from: a */
    public final long m36064a() {
        return this.f28000c;
    }

    /* JADX INFO: renamed from: b */
    public final int m36065b() {
        return this.f27998a;
    }

    /* JADX INFO: renamed from: c */
    public final int m36066c() {
        return this.f27999b;
    }

    /* JADX INFO: renamed from: d */
    public final byte[] m36067d() {
        return this.f28001d;
    }
}
