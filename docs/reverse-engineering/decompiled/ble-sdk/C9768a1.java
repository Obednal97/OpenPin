package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.a1 */
/* JADX INFO: loaded from: classes3.dex */
public class C9768a1 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f27912b;

    /* JADX INFO: renamed from: c */
    public int f27913c;

    /* JADX INFO: renamed from: d */
    public boolean f27914d;

    public C9768a1(byte[] bArr) {
        super(bArr);
        this.f27912b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f27913c = (byte) (TntBleCommUtils.m26098b().m26108k(bArr, 7) & 255);
        if (bArr.length >= 9) {
            this.f27914d = TntBleCommUtils.m26098b().m26108k(bArr, 8) == 1;
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 4;
    }

    /* JADX INFO: renamed from: b */
    public int m36001b() {
        return this.f27913c;
    }

    /* JADX INFO: renamed from: c */
    public boolean m36002c() {
        return this.f27914d;
    }

    public String toString() {
        return "TimeSyncRsp{stamp=" + this.f27912b + ", timezone=" + this.f27913c + ", hasStatistics=" + this.f27914d + '}';
    }
}
