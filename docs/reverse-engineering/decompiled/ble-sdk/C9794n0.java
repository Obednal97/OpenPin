package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;
import th.AbstractC11066l;

/* JADX INFO: renamed from: oh.n0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9794n0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27977b;

    public C9794n0(byte[] bArr) {
        super(bArr);
        System.out.println("ChenTian SetWifiRsp:[" + AbstractC11066l.m39994a(bArr) + "]");
        this.f27977b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 121;
    }

    /* JADX INFO: renamed from: b */
    public int m36049b() {
        return this.f27977b;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "SetWifiRsp", new Object[0]);
    }
}
