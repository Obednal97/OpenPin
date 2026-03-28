package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.f0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9778f0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final boolean f27942b;

    public C9778f0(byte[] bArr) {
        super(bArr);
        this.f27942b = TntBleCommUtils.m26098b().m26108k(bArr, 3) == 1;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 103;
    }

    /* JADX INFO: renamed from: b */
    public boolean m36022b() {
        return this.f27942b;
    }

    public String toString() {
        return String.format("PrivacySetRsp{onOff=%s}", Boolean.valueOf(this.f27942b));
    }
}
