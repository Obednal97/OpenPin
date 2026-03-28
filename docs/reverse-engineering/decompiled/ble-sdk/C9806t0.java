package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.t0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9806t0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f28007b;

    /* JADX INFO: renamed from: c */
    public final int f28008c;

    public C9806t0(byte[] bArr) {
        super(bArr);
        this.f28007b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f28008c = TntBleCommUtils.m26098b().m26108k(bArr, 7);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 28;
    }

    /* JADX INFO: renamed from: b */
    public int m36073b() {
        return this.f28008c;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "SyncFileHeadRsp{sessionId=%d, status=%d}", Long.valueOf(this.f28007b), Integer.valueOf(this.f28008c));
    }
}
