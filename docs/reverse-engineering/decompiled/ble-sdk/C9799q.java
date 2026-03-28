package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;
import th.AbstractC11073s;

/* JADX INFO: renamed from: oh.q */
/* JADX INFO: loaded from: classes3.dex */
public class C9799q extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public int f27986b;

    /* JADX INFO: renamed from: c */
    public long f27987c;

    /* JADX INFO: renamed from: d */
    public int f27988d;

    /* JADX INFO: renamed from: e */
    public boolean f27989e;

    /* JADX INFO: renamed from: f */
    public String f27990f;

    public C9799q(byte[] bArr, int i10) {
        super(bArr);
        this.f27990f = AbstractC11073s.m40048d(bArr);
        if (bArr.length < 4) {
            return;
        }
        this.f27986b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
        if (bArr.length < 8) {
            return;
        }
        this.f27987c = TntBleCommUtils.m26098b().m26111n(bArr, 4);
        if (bArr.length < 10) {
            return;
        }
        this.f27988d = TntBleCommUtils.m26098b().m26109l(bArr, 8);
        if (bArr.length < 11) {
            return;
        }
        boolean z10 = true;
        if (TntBleCommUtils.m26098b().m26108k(bArr, 10) != 1 && this.f27988d > 0) {
            z10 = false;
        }
        this.f27989e = z10;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 113;
    }

    /* JADX INFO: renamed from: b */
    public long m36054b() {
        return this.f27987c;
    }

    /* JADX INFO: renamed from: c */
    public int m36055c() {
        return this.f27988d;
    }

    /* JADX INFO: renamed from: d */
    public int m36056d() {
        return this.f27986b;
    }

    /* JADX INFO: renamed from: e */
    public boolean m36057e() {
        return this.f27989e;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "oriByteStr = [%s] SyncRecFileDelRsp{type=%d, offset=%d, size=%d, isFinished=%s} ", this.f27990f, Integer.valueOf(this.f27986b), Long.valueOf(this.f27987c), Integer.valueOf(this.f27988d), Boolean.valueOf(this.f27989e));
    }
}
