package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.j */
/* JADX INFO: loaded from: classes3.dex */
public class C9785j extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public int f27962b;

    public C9785j(byte[] bArr) {
        super(bArr);
        this.f27962b = 0;
        if (bArr.length >= 4) {
            this.f27962b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 104;
    }

    public String toString() {
        return "ClearRecordFileRsp{status=" + this.f27962b + '}';
    }
}
