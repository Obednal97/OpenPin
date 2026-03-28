package p279oh;

import bg.InterfaceC2607c;
import com.klaviyo.analytics.networking.requests.KlaviyoErrorResponse;
import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.e */
/* JADX INFO: loaded from: classes3.dex */
public class C9775e extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    @InterfaceC2607c("uid")
    private final long f27937b;

    /* JADX INFO: renamed from: c */
    @InterfaceC2607c(KlaviyoErrorResponse.STATUS)
    private final int f27938c;

    /* JADX INFO: renamed from: d */
    @InterfaceC2607c("versionName")
    private String f27939d;

    public C9775e(byte[] bArr) {
        super(bArr);
        this.f27939d = "";
        this.f27937b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        if (bArr.length < 10) {
            this.f27938c = TntBleCommUtils.m26098b().m26109l(bArr, 7);
            return;
        }
        this.f27938c = TntBleCommUtils.m26098b().m26108k(bArr, 7);
        if (bArr.length >= 12) {
            this.f27939d = new String(bArr, 8, 1);
            this.f27939d += String.format(Locale.getDefault(), "%04d", Long.valueOf(TntBleCommUtils.m26098b().m26110m(bArr, 9)));
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 50;
    }

    /* JADX INFO: renamed from: b */
    public int m36020b() {
        return this.f27938c;
    }

    /* JADX INFO: renamed from: c */
    public long m36021c() {
        return this.f27937b;
    }

    public String toString() {
        return "AppFotaPushRsp{uid=" + this.f27937b + ", status=" + this.f27938c + ", versionName='" + this.f27939d + "'}";
    }
}
