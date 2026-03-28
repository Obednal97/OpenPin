package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.l0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9790l0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final boolean f27971b;

    public C9790l0(byte[] bArr) {
        super(bArr);
        this.f27971b = TntBleCommUtils.m26098b().m26108k(bArr, 3) != 1;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 34;
    }

    /* JADX INFO: renamed from: b */
    public boolean m36043b() {
        return this.f27971b;
    }

    public String toString() {
        return String.format("GetLedStatusRsp{onOff=%s}", Boolean.valueOf(this.f27971b));
    }
}
