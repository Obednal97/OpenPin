package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import kotlin.jvm.internal.AbstractC8694o;
import kotlin.jvm.internal.DefaultConstructorMarker;
import kotlin.text.C8713c;
import th.AbstractC11066l;

/* JADX INFO: renamed from: oh.a0 */
/* JADX INFO: loaded from: classes3.dex */
public final class C9767a0 extends AbstractC9777f {

    /* JADX INFO: renamed from: c */
    public static final a f27910c = new a(null);

    /* JADX INFO: renamed from: b */
    public final byte[] f27911b;

    /* JADX INFO: renamed from: oh.a0$a */
    public static final class a {
        public /* synthetic */ a(DefaultConstructorMarker defaultConstructorMarker) {
            this();
        }

        public a() {
        }
    }

    public C9767a0(byte[] bArr) {
        super(bArr);
        this.f27911b = bArr;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 120;
    }

    /* JADX INFO: renamed from: b */
    public final String m35997b() {
        if (this.f27911b == null) {
            return "";
        }
        int iM26108k = TntBleCommUtils.m26098b().m26108k(this.f27911b, 40);
        int length = this.f27911b.length;
        byte[] bArr = this.f27911b;
        return bArr.length < 41 + iM26108k ? "" : new String(bArr, 41, iM26108k, C8713c.f25417b);
    }

    /* JADX INFO: renamed from: c */
    public final String m35998c() {
        String strM39994a = AbstractC11066l.m39994a(this.f27911b);
        AbstractC8694o.m32337d(strM39994a, "bytesToHex(...)");
        return strM39994a;
    }

    /* JADX INFO: renamed from: d */
    public final String m35999d() {
        if (this.f27911b == null) {
            return "";
        }
        int iM26108k = TntBleCommUtils.m26098b().m26108k(this.f27911b, 7);
        int length = this.f27911b.length;
        byte[] bArr = this.f27911b;
        return bArr.length < 8 + iM26108k ? "" : new String(bArr, 8, iM26108k, C8713c.f25417b);
    }

    /* JADX INFO: renamed from: e */
    public final long m36000e() {
        if (this.f27911b == null) {
            return 0L;
        }
        return TntBleCommUtils.m26098b().m26111n(this.f27911b, 3);
    }
}
