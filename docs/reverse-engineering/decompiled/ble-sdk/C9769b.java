package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.b */
/* JADX INFO: loaded from: classes3.dex */
public class C9769b extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27915b;

    /* JADX INFO: renamed from: c */
    public final int f27916c;

    /* JADX INFO: renamed from: d */
    public final int f27917d;

    public C9769b(byte[] bArr) {
        super(bArr);
        this.f27915b = (int) TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f27916c = (int) TntBleCommUtils.m26098b().m26111n(bArr, 7);
        this.f27917d = (int) TntBleCommUtils.m26098b().m26111n(bArr, 11);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 36;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "AnglesRsp{pitch=%d,rollback=%d,yaw=%d}", Integer.valueOf(this.f27915b), Integer.valueOf(this.f27916c), Integer.valueOf(this.f27917d));
    }
}
