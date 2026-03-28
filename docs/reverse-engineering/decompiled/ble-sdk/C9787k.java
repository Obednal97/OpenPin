package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.k */
/* JADX INFO: loaded from: classes3.dex */
public class C9787k extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27967b;

    public C9787k(byte[] bArr) {
        super(bArr);
        this.f27967b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 13;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "CloseWiFiRsp{status=%d} ", Integer.valueOf(this.f27967b));
    }
}
