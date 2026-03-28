package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.u */
/* JADX INFO: loaded from: classes3.dex */
public class C9807u extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final boolean f28009b;

    public C9807u(byte[] bArr) {
        super(bArr);
        this.f28009b = TntBleCommUtils.m26098b().m26108k(bArr, 3) != 1;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 33;
    }

    /* JADX INFO: renamed from: b */
    public boolean m36074b() {
        return this.f28009b;
    }

    public String toString() {
        return String.format("GetLedStatusRsp{onOff=%s}", Boolean.valueOf(this.f28009b));
    }
}
