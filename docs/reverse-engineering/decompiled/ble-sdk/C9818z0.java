package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;
import th.AbstractC11066l;

/* JADX INFO: renamed from: oh.z0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9818z0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f28043b;

    /* JADX INFO: renamed from: c */
    public final int f28044c;

    /* JADX INFO: renamed from: d */
    public final long f28045d;

    public C9818z0(byte[] bArr) {
        super(bArr);
        System.out.println("ChenTian TestWifiResultRsp:[" + AbstractC11066l.m39994a(bArr) + "]");
        this.f28043b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f28044c = TntBleCommUtils.m26098b().m26108k(bArr, 7);
        this.f28045d = TntBleCommUtils.m26098b().m26111n(bArr, 8);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 124;
    }

    /* JADX INFO: renamed from: b */
    public long m36103b() {
        return this.f28045d;
    }

    /* JADX INFO: renamed from: c */
    public long m36104c() {
        return this.f28043b;
    }

    /* JADX INFO: renamed from: d */
    public int m36105d() {
        return this.f28044c;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "TestWifiResultRsp", new Object[0]);
    }
}
