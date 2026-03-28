package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.e0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9776e0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27940b;

    public C9776e0(byte[] bArr) {
        super(bArr);
        this.f27940b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 10;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "OpenWiFiRsp{status=%d} ", Integer.valueOf(this.f27940b));
    }
}
