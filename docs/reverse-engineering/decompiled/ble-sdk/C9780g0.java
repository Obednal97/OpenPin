package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.g0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9780g0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public long f27945b;

    /* JADX INFO: renamed from: c */
    public int f27946c;

    /* JADX INFO: renamed from: d */
    public boolean f27947d;

    /* JADX INFO: renamed from: e */
    public long f27948e;

    public C9780g0(byte[] bArr) {
        super(bArr);
        if (bArr.length < 13) {
            return;
        }
        this.f27945b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f27946c = TntBleCommUtils.m26098b().m26108k(bArr, 7);
        if (bArr.length >= 9) {
            this.f27947d = TntBleCommUtils.m26098b().m26108k(bArr, 8) == 1;
            if (bArr.length >= 13) {
                this.f27948e = TntBleCommUtils.m26098b().m26111n(bArr, 9);
            }
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 21;
    }

    /* JADX INFO: renamed from: b */
    public long m36025b() {
        return this.f27948e;
    }

    /* JADX INFO: renamed from: c */
    public int m36026c() {
        return this.f27946c;
    }

    /* JADX INFO: renamed from: d */
    public long m36027d() {
        return this.f27945b;
    }

    public String toString() {
        return "RecordPauseRsp{sessionId=" + this.f27945b + ", reason=" + this.f27946c + ", fileExist=" + this.f27947d + ", fileSize=" + this.f27948e + '}';
    }
}
