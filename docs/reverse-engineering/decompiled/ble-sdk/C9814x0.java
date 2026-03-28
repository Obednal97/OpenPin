package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import th.AbstractC11072r;

/* JADX INFO: renamed from: oh.x0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9814x0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public int f28026b;

    /* JADX INFO: renamed from: c */
    public int f28027c;

    /* JADX INFO: renamed from: d */
    public int f28028d;

    /* JADX INFO: renamed from: e */
    public int f28029e;

    /* JADX INFO: renamed from: f */
    public byte[] f28030f;

    /* JADX INFO: renamed from: g */
    public int f28031g;

    public C9814x0(byte[] bArr) {
        super(bArr);
        this.f28026b = 0;
        this.f28031g = -1;
        try {
            this.f28026b = (int) TntBleCommUtils.m26098b().m26111n(bArr, 3);
        } catch (NumberFormatException e10) {
            AbstractC11072r.m40041m(this.f27941a, e10);
        }
        if (this.f28026b == 0) {
            this.f28031g = TntBleCommUtils.m26098b().m26108k(bArr, 7);
            return;
        }
        this.f28027c = TntBleCommUtils.m26098b().m26108k(bArr, 7);
        this.f28028d = TntBleCommUtils.m26098b().m26108k(bArr, 8);
        int iM26108k = TntBleCommUtils.m26098b().m26108k(bArr, 9);
        this.f28029e = iM26108k;
        byte[] bArr2 = new byte[iM26108k];
        this.f28030f = bArr2;
        try {
            System.arraycopy(bArr, 10, bArr2, 0, iM26108k);
        } catch (Exception e11) {
            AbstractC11072r.m40031c(this.f27941a, e11);
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 61;
    }

    /* JADX INFO: renamed from: b */
    public byte[] m36089b() {
        return this.f28030f;
    }

    /* JADX INFO: renamed from: c */
    public int m36090c() {
        return this.f28026b;
    }

    /* JADX INFO: renamed from: d */
    public int m36091d() {
        return this.f28028d;
    }

    /* JADX INFO: renamed from: e */
    public int m36092e() {
        return this.f28031g;
    }

    /* JADX INFO: renamed from: f */
    public int m36093f() {
        return this.f28027c;
    }

    public String toString() {
        return "SyncStatFileRsp{date=" + this.f28026b + ", total=" + this.f28027c + ", index=" + this.f28028d + ", dataLength=" + this.f28029e + ", reason=" + this.f28031g + '}';
    }
}
