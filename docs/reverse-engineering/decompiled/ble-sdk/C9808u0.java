package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.u0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9808u0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f28010b;

    /* JADX INFO: renamed from: c */
    public final int f28011c;

    public C9808u0(byte[] bArr) {
        super(bArr);
        this.f28010b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f28011c = TntBleCommUtils.m26098b().m26109l(bArr, 7);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 29;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "SyncFileTailRsp{sessionId=%d, crc=%d}", Long.valueOf(this.f28010b), Integer.valueOf(this.f28011c));
    }
}
