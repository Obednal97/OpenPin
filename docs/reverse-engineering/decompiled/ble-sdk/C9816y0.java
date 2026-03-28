package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;
import th.AbstractC11066l;

/* JADX INFO: renamed from: oh.y0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9816y0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f28042b;

    public C9816y0(byte[] bArr) {
        super(bArr);
        System.out.println("ChenTian TestWifiInfoRsp:[" + AbstractC11066l.m39994a(bArr) + "]");
        this.f28042b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 123;
    }

    /* JADX INFO: renamed from: b */
    public long m36102b() {
        return this.f28042b;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "TestWifiInfoRsp", new Object[0]);
    }
}
