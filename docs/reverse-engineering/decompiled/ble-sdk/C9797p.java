package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;
import th.AbstractC11073s;

/* JADX INFO: renamed from: oh.p */
/* JADX INFO: loaded from: classes3.dex */
public class C9797p extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public int f27982b;

    /* JADX INFO: renamed from: c */
    public String f27983c;

    public C9797p(byte[] bArr, int i10) {
        super(bArr);
        this.f27983c = AbstractC11073s.m40048d(bArr);
        if (bArr.length < 4) {
            return;
        }
        this.f27982b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 117;
    }

    /* JADX INFO: renamed from: b */
    public int m36053b() {
        return this.f27982b;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "oriByteStr = [%s] FileDataCheckRsp{result=%d} ", this.f27983c, Integer.valueOf(this.f27982b));
    }
}
