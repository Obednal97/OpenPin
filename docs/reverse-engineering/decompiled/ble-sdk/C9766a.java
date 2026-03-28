package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.a */
/* JADX INFO: loaded from: classes3.dex */
public final class C9766a extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final byte[] f27909b;

    public C9766a(byte[] bArr) {
        super(bArr);
        this.f27909b = bArr;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 109;
    }

    /* JADX INFO: renamed from: b */
    public final boolean m35996b() {
        return TntBleCommUtils.m26098b().m26108k(this.f27909b, 3) == 1;
    }
}
