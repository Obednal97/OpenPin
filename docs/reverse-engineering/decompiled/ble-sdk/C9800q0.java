package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.q0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9800q0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f27991b;

    /* JADX INFO: renamed from: c */
    public final long f27992c;

    /* JADX INFO: renamed from: d */
    public long f27993d;

    public C9800q0(byte[] bArr) {
        super(bArr);
        this.f27991b = TntBleCommUtils.m26098b().m26112o(bArr, 3);
        this.f27992c = TntBleCommUtils.m26098b().m26112o(bArr, 11);
        if (bArr.length >= 27) {
            this.f27993d = TntBleCommUtils.m26098b().m26112o(bArr, 19);
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 6;
    }

    /* JADX INFO: renamed from: b */
    public long m36058b() {
        return this.f27993d;
    }

    /* JADX INFO: renamed from: c */
    public long m36059c() {
        return this.f27991b;
    }

    /* JADX INFO: renamed from: d */
    public long m36060d() {
        return this.f27992c;
    }

    public String toString() {
        return "StorageRsp{free=" + this.f27991b + ", total=" + this.f27992c + ", duration=" + this.f27993d + '}';
    }
}
