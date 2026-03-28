package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.LinkedHashMap;
import java.util.Map;
import kotlin.jvm.internal.DefaultConstructorMarker;

/* JADX INFO: renamed from: oh.m */
/* JADX INFO: loaded from: classes3.dex */
public final class C9791m extends AbstractC9777f {

    /* JADX INFO: renamed from: d */
    public static final a f27972d = new a(null);

    /* JADX INFO: renamed from: b */
    public final byte[] f27973b;

    /* JADX INFO: renamed from: c */
    public Map f27974c;

    /* JADX INFO: renamed from: oh.m$a */
    public static final class a {
        public /* synthetic */ a(DefaultConstructorMarker defaultConstructorMarker) {
            this();
        }

        public a() {
        }
    }

    public C9791m(byte[] bArr) {
        super(bArr);
        this.f27973b = bArr;
        this.f27974c = new LinkedHashMap();
        m36044c();
    }

    /* JADX INFO: renamed from: c */
    private final void m36044c() {
        byte[] bArr = this.f27973b;
        if (bArr == null || bArr.length < 6) {
            return;
        }
        int iM26108k = TntBleCommUtils.m26098b().m26108k(this.f27973b, 3);
        int iM26108k2 = TntBleCommUtils.m26098b().m26108k(this.f27973b, 4);
        int iM26108k3 = TntBleCommUtils.m26098b().m26108k(this.f27973b, 5);
        this.f27974c.put("logType", Integer.valueOf(iM26108k));
        this.f27974c.put("logIndex", Integer.valueOf(iM26108k2));
        this.f27974c.put("result", Integer.valueOf(iM26108k3));
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 146;
    }

    /* JADX INFO: renamed from: b */
    public final Map m36045b() {
        return this.f27974c;
    }
}
