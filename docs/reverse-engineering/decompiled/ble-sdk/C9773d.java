package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.d */
/* JADX INFO: loaded from: classes3.dex */
public class C9773d extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f27924b;

    /* JADX INFO: renamed from: c */
    public final long f27925c;

    /* JADX INFO: renamed from: d */
    public long f27926d;

    public C9773d(byte[] bArr) {
        super(bArr);
        this.f27924b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f27925c = TntBleCommUtils.m26098b().m26111n(bArr, 7);
        if (bArr.length > 11) {
            this.f27926d = TntBleCommUtils.m26098b().m26111n(bArr, 11);
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 52;
    }

    /* JADX INFO: renamed from: b */
    public long m36007b() {
        return this.f27926d;
    }

    /* JADX INFO: renamed from: c */
    public long m36008c() {
        return this.f27925c;
    }

    /* JADX INFO: renamed from: d */
    public long m36009d() {
        return this.f27924b;
    }

    public String toString() {
        return "AppFotaPackRsp{uid=" + this.f27924b + ", start=" + this.f27925c + ", end=" + this.f27926d + '}';
    }
}
