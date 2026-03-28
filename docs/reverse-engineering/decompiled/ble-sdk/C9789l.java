package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.l */
/* JADX INFO: loaded from: classes3.dex */
public class C9789l extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27969b;

    /* JADX INFO: renamed from: c */
    public final long f27970c;

    public C9789l(byte[] bArr) {
        super(bArr);
        this.f27969b = TntBleCommUtils.m26098b().m26109l(bArr, 3);
        this.f27970c = TntBleCommUtils.m26098b().m26111n(bArr, 5);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 8;
    }

    /* JADX INFO: renamed from: b */
    public int m36041b() {
        return this.f27969b;
    }

    /* JADX INFO: renamed from: c */
    public long m36042c() {
        return this.f27970c;
    }

    public String toString() {
        return "CommonSettingsRsp{type=" + this.f27969b + ", value=" + this.f27970c + '}';
    }
}
