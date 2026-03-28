package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.ArrayList;
import java.util.List;
import kotlin.jvm.internal.AbstractC8694o;
import kotlin.jvm.internal.DefaultConstructorMarker;
import p145hj.AbstractC7437a0;
import p145hj.AbstractC7458l;
import p225lh.C9222h;

/* JADX INFO: renamed from: oh.r */
/* JADX INFO: loaded from: classes3.dex */
public final class C9801r extends AbstractC9777f {

    /* JADX INFO: renamed from: e */
    public static final a f27994e = new a(null);

    /* JADX INFO: renamed from: b */
    public final byte[] f27995b;

    /* JADX INFO: renamed from: c */
    public int f27996c;

    /* JADX INFO: renamed from: d */
    public List f27997d;

    /* JADX INFO: renamed from: oh.r$a */
    public static final class a {
        public /* synthetic */ a(DefaultConstructorMarker defaultConstructorMarker) {
            this();
        }

        public a() {
        }
    }

    public C9801r(byte[] bArr) {
        super(bArr);
        this.f27995b = bArr;
        this.f27997d = new ArrayList();
        m36063d(bArr);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 142;
    }

    /* JADX INFO: renamed from: b */
    public final List m36061b() {
        return this.f27997d;
    }

    /* JADX INFO: renamed from: c */
    public final int m36062c() {
        return this.f27996c;
    }

    /* JADX INFO: renamed from: d */
    public final void m36063d(byte[] bArr) {
        int iM26108k = TntBleCommUtils.m26098b().m26108k(bArr, 3);
        this.f27996c = iM26108k;
        if (iM26108k > 0) {
            int i10 = 4;
            for (int i11 = 0; i11 < iM26108k; i11++) {
                int iM26108k2 = TntBleCommUtils.m26098b().m26108k(bArr, i10);
                int iM26108k3 = TntBleCommUtils.m26098b().m26108k(bArr, i10 + 1);
                int iM26108k4 = TntBleCommUtils.m26098b().m26108k(bArr, i10 + 2);
                long jM26111n = TntBleCommUtils.m26098b().m26111n(bArr, i10 + 3);
                int i12 = i10 + 7;
                AbstractC8694o.m32335b(bArr);
                i10 += 9;
                byte[] bArrM29070k = AbstractC7458l.m29070k(bArr, i12, i10);
                ArrayList arrayList = new ArrayList(bArrM29070k.length);
                for (byte b10 : bArrM29070k) {
                    arrayList.add(Integer.valueOf(b10));
                }
                this.f27997d = AbstractC7437a0.m28991k0(this.f27997d, new C9222h(iM26108k2, iM26108k3, iM26108k4, jM26111n, arrayList));
            }
        }
    }
}
