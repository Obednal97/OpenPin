package p279oh;

import com.klaviyo.analytics.networking.requests.KlaviyoErrorResponse;
import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.LinkedHashMap;
import java.util.Map;
import kotlin.jvm.internal.DefaultConstructorMarker;

/* JADX INFO: renamed from: oh.o0 */
/* JADX INFO: loaded from: classes3.dex */
public final class C9796o0 extends AbstractC9777f {

    /* JADX INFO: renamed from: d */
    public static final a f27979d = new a(null);

    /* JADX INFO: renamed from: b */
    public final byte[] f27980b;

    /* JADX INFO: renamed from: c */
    public Map f27981c;

    /* JADX INFO: renamed from: oh.o0$a */
    public static final class a {
        public /* synthetic */ a(DefaultConstructorMarker defaultConstructorMarker) {
            this();
        }

        public a() {
        }
    }

    public C9796o0(byte[] bArr) {
        super(bArr);
        this.f27980b = bArr;
        this.f27981c = new LinkedHashMap();
        m36052c();
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 143;
    }

    /* JADX INFO: renamed from: b */
    public final Map m36051b() {
        return this.f27981c;
    }

    /* JADX INFO: renamed from: c */
    public final void m36052c() {
        byte[] bArr = this.f27980b;
        if (bArr == null || bArr.length < 6) {
            return;
        }
        int iM26108k = TntBleCommUtils.m26098b().m26108k(this.f27980b, 3);
        int iM26108k2 = TntBleCommUtils.m26098b().m26108k(this.f27980b, 4);
        int iM26108k3 = TntBleCommUtils.m26098b().m26108k(this.f27980b, 5);
        this.f27981c.put("logType", Integer.valueOf(iM26108k));
        this.f27981c.put("logIndex", Integer.valueOf(iM26108k2));
        this.f27981c.put(KlaviyoErrorResponse.STATUS, Integer.valueOf(iM26108k3));
    }
}
