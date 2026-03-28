package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.j0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9786j0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f27963b;

    /* JADX INFO: renamed from: c */
    public final int f27964c;

    /* JADX INFO: renamed from: d */
    public boolean f27965d;

    /* JADX INFO: renamed from: e */
    public long f27966e;

    public C9786j0(byte[] bArr) {
        super(bArr);
        this.f27963b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f27964c = TntBleCommUtils.m26098b().m26108k(bArr, 7);
        if (bArr.length >= 9) {
            this.f27965d = TntBleCommUtils.m26098b().m26108k(bArr, 8) == 1;
            if (bArr.length >= 13) {
                this.f27966e = TntBleCommUtils.m26098b().m26111n(bArr, 9);
            }
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 23;
    }

    /* JADX INFO: renamed from: b */
    public long m36037b() {
        return this.f27966e;
    }

    /* JADX INFO: renamed from: c */
    public int m36038c() {
        return this.f27964c;
    }

    /* JADX INFO: renamed from: d */
    public long m36039d() {
        return this.f27963b;
    }

    public String toString() {
        return "RecordStopRsp{sessionId=" + this.f27963b + ", reason=" + this.f27964c + ", fileExist=" + this.f27965d + ", fileSize=" + this.f27966e + '}';
    }
}
