package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.c */
/* JADX INFO: loaded from: classes3.dex */
public class C9771c extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f27920b;

    /* JADX INFO: renamed from: c */
    public int f27921c;

    public C9771c(byte[] bArr) {
        super(bArr);
        this.f27920b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        if (bArr.length > 7) {
            this.f27921c = TntBleCommUtils.m26098b().m26108k(bArr, 7);
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 51;
    }

    /* JADX INFO: renamed from: b */
    public int m36004b() {
        return this.f27921c;
    }

    /* JADX INFO: renamed from: c */
    public long m36005c() {
        return this.f27920b;
    }

    public String toString() {
        return "AppFotaPackFinishRsp{uid=" + this.f27920b + '}';
    }
}
