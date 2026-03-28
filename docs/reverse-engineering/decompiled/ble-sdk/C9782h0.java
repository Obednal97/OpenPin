package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.h0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9782h0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f27951b;

    /* JADX INFO: renamed from: c */
    public final long f27952c;

    /* JADX INFO: renamed from: d */
    public final int f27953d;

    /* JADX INFO: renamed from: e */
    public int f27954e;

    /* JADX INFO: renamed from: f */
    public long f27955f;

    public C9782h0(byte[] bArr) {
        super(bArr);
        this.f27954e = 0;
        this.f27955f = 0L;
        this.f27951b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f27952c = TntBleCommUtils.m26098b().m26111n(bArr, 7);
        this.f27953d = TntBleCommUtils.m26098b().m26108k(bArr, 11);
        if (bArr.length >= 17) {
            this.f27954e = TntBleCommUtils.m26098b().m26108k(bArr, 12);
            this.f27955f = TntBleCommUtils.m26098b().m26111n(bArr, 13);
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 22;
    }

    /* JADX INFO: renamed from: b */
    public int m36029b() {
        return this.f27954e;
    }

    /* JADX INFO: renamed from: c */
    public long m36030c() {
        return this.f27951b;
    }

    /* JADX INFO: renamed from: d */
    public long m36031d() {
        return this.f27952c;
    }

    /* JADX INFO: renamed from: e */
    public int m36032e() {
        return this.f27953d;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "RecordResumeRsp{sessionId=%d, start=%d, status=%d, scene=%d, startTime=%d}", Long.valueOf(this.f27951b), Long.valueOf(this.f27952c), Integer.valueOf(this.f27953d), Integer.valueOf(this.f27954e), Long.valueOf(this.f27955f));
    }
}
