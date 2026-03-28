package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.ArrayList;
import java.util.Locale;
import th.AbstractC11066l;

/* JADX INFO: renamed from: oh.b0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9770b0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27918b;

    /* JADX INFO: renamed from: c */
    public final ArrayList f27919c;

    public C9770b0(byte[] bArr) {
        super(bArr);
        this.f27919c = new ArrayList();
        System.out.println("ChenTian GetWifiListRsp:[" + AbstractC11066l.m39994a(bArr) + "]");
        this.f27918b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
        int i10 = 4;
        while (true) {
            int i11 = i10 + 4;
            if (i11 > bArr.length) {
                return;
            }
            this.f27919c.add(Long.valueOf(TntBleCommUtils.m26098b().m26111n(bArr, i10)));
            i10 = i11;
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 122;
    }

    /* JADX INFO: renamed from: b */
    public ArrayList m36003b() {
        return this.f27919c;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "GetWifiListRsp{totals=%d, ListSize=%d}", Integer.valueOf(this.f27918b), Integer.valueOf(this.f27919c.size()));
    }
}
