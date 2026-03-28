package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;
import th.AbstractC11066l;

/* JADX INFO: renamed from: oh.n */
/* JADX INFO: loaded from: classes3.dex */
public class C9793n extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27976b;

    public C9793n(byte[] bArr) {
        super(bArr);
        System.out.println("ChenTian DeleteWifiRsp:[" + AbstractC11066l.m39994a(bArr) + "]");
        this.f27976b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 125;
    }

    /* JADX INFO: renamed from: b */
    public int m36048b() {
        return this.f27976b;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "DeleteWifiRsp", new Object[0]);
    }
}
