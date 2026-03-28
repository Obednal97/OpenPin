package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;
import th.AbstractC11072r;
import th.AbstractC11073s;

/* JADX INFO: renamed from: oh.y */
/* JADX INFO: loaded from: classes3.dex */
public class C9815y extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f28032b;

    /* JADX INFO: renamed from: c */
    public final boolean f28033c;

    /* JADX INFO: renamed from: d */
    public int f28034d;

    /* JADX INFO: renamed from: e */
    public boolean f28035e;

    /* JADX INFO: renamed from: f */
    public boolean f28036f;

    /* JADX INFO: renamed from: g */
    public int f28037g;

    /* JADX INFO: renamed from: h */
    public int f28038h;

    /* JADX INFO: renamed from: i */
    public int f28039i;

    /* JADX INFO: renamed from: j */
    public int f28040j;

    /* JADX INFO: renamed from: k */
    public long f28041k;

    public C9815y(byte[] bArr) {
        super(bArr);
        AbstractC11072r.m40030b("zdk", "get secnes-" + AbstractC11073s.m40048d(bArr), new Object[0]);
        this.f28032b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f28033c = TntBleCommUtils.m26098b().m26108k(bArr, 7) == 1;
        if (bArr.length < 9) {
            return;
        }
        int iM26108k = TntBleCommUtils.m26098b().m26108k(bArr, 8);
        this.f28034d = iM26108k;
        this.f28035e = iM26108k == 1;
        if (bArr.length < 10) {
            return;
        }
        this.f28036f = TntBleCommUtils.m26098b().m26108k(bArr, 9) == 1;
        if (bArr.length < 15) {
            return;
        }
        AbstractC11072r.m40030b("zdk", "get secnes-----------", new Object[0]);
        this.f28040j = TntBleCommUtils.m26098b().m26108k(bArr, 10);
        this.f28041k = TntBleCommUtils.m26098b().m26111n(bArr, 11);
        if (bArr.length < 16) {
            return;
        }
        this.f28037g = TntBleCommUtils.m26098b().m26108k(bArr, 15);
        if (bArr.length < 17) {
            return;
        }
        this.f28038h = TntBleCommUtils.m26098b().m26108k(bArr, 16);
        if (bArr.length < 18) {
            return;
        }
        this.f28039i = TntBleCommUtils.m26098b().m26108k(bArr, 17);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 3;
    }

    /* JADX INFO: renamed from: b */
    public long m36094b() {
        return this.f28034d;
    }

    /* JADX INFO: renamed from: c */
    public long m36095c() {
        return this.f28041k;
    }

    /* JADX INFO: renamed from: d */
    public long m36096d() {
        return this.f28032b;
    }

    /* JADX INFO: renamed from: e */
    public int m36097e() {
        return this.f28037g;
    }

    /* JADX INFO: renamed from: f */
    public int m36098f() {
        return this.f28039i;
    }

    /* JADX INFO: renamed from: g */
    public int m36099g() {
        return this.f28038h;
    }

    /* JADX INFO: renamed from: h */
    public boolean m36100h() {
        return this.f28033c;
    }

    /* JADX INFO: renamed from: i */
    public boolean m36101i() {
        return this.f28036f;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "GetStateRsp{state=%d, privacyEnable=%s, keyState=%s, usbState=%s,findMyState=%s, scene=%d, sessionId=%d}", Long.valueOf(this.f28032b), Boolean.valueOf(this.f28033c), Boolean.valueOf(this.f28035e), Boolean.valueOf(this.f28036f), Integer.valueOf(this.f28037g), Integer.valueOf(this.f28040j), Long.valueOf(this.f28041k));
    }
}
