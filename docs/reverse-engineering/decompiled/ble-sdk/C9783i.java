package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.i */
/* JADX INFO: loaded from: classes3.dex */
public class C9783i extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27956b;

    public C9783i(byte[] bArr) {
        super(bArr);
        this.f27956b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 10;
    }

    public String toString() {
        return String.format(this.f27941a + "{status=%d}", Integer.valueOf(this.f27956b));
    }
}
