package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.v0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9810v0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public long f28014b;

    /* JADX INFO: renamed from: c */
    public final int f28015c;

    public C9810v0(byte[] bArr, int i10) {
        super(bArr);
        int i11 = 3;
        if (i10 >= 7) {
            this.f28014b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
            i11 = 7;
        }
        this.f28015c = TntBleCommUtils.m26098b().m26108k(bArr, i11);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 31;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "SyncRecFileDelRsp{sessionId=%d, status=%d} ", Long.valueOf(this.f28014b), Integer.valueOf(this.f28015c));
    }
}
