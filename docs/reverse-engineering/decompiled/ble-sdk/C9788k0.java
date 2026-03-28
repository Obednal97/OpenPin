package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.k0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9788k0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27968b;

    public C9788k0(byte[] bArr) {
        super(bArr);
        this.f27968b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 128;
    }

    /* JADX INFO: renamed from: b */
    public int m36040b() {
        return this.f27968b;
    }

    public String toString() {
        return String.format("ResetFindMyRsp{onOff=%d}", Integer.valueOf(this.f27968b));
    }
}
