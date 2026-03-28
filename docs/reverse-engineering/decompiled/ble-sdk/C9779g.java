package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.g */
/* JADX INFO: loaded from: classes3.dex */
public class C9779g extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final boolean f27943b;

    /* JADX INFO: renamed from: c */
    public final int f27944c;

    public C9779g(byte[] bArr) {
        super(bArr);
        this.f27943b = TntBleCommUtils.m26098b().m26108k(bArr, 3) == 1;
        this.f27944c = TntBleCommUtils.m26098b().m26108k(bArr, 4);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 9;
    }

    /* JADX INFO: renamed from: b */
    public int m36023b() {
        return this.f27944c;
    }

    /* JADX INFO: renamed from: c */
    public boolean m36024c() {
        return this.f27943b;
    }

    public String toString() {
        return "BattStatusRsp{charging=" + this.f27943b + ", level=" + this.f27944c + "} ";
    }
}
