package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.o */
/* JADX INFO: loaded from: classes3.dex */
public class C9795o extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public int f27978b;

    public C9795o(byte[] bArr) {
        super(bArr);
        this.f27978b = 0;
        if (bArr.length > 3) {
            this.f27978b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 5;
    }

    /* JADX INFO: renamed from: b */
    public int m36050b() {
        return this.f27978b;
    }

    public String toString() {
        return "DepairRsp{status=" + this.f27978b + '}';
    }
}
