package p279oh;

import com.tinnotech.penblesdk.entity.BleFile;
import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.ArrayList;
import java.util.Locale;

/* JADX INFO: renamed from: oh.v */
/* JADX INFO: loaded from: classes3.dex */
public class C9809v extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f28012b;

    /* JADX INFO: renamed from: c */
    public final ArrayList f28013c;

    public C9809v(byte[] bArr) {
        super(bArr);
        int iM26109l = TntBleCommUtils.m26098b().m26109l(bArr, 7);
        this.f28012b = iM26109l;
        this.f28013c = new ArrayList(iM26109l);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 26;
    }

    /* JADX INFO: renamed from: b */
    public void m36075b(byte[] bArr, int i10) {
        int i11 = 11;
        if (bArr.length >= 11 && TntBleCommUtils.m26098b().m26109l(bArr, 9) == this.f28013c.size()) {
            int i12 = 0;
            switch (i10) {
                case 1:
                    int length = (bArr.length - 11) / 8;
                    if (length > 0) {
                        while (i12 < length) {
                            long jM26111n = TntBleCommUtils.m26098b().m26111n(bArr, i11);
                            long jM26111n2 = TntBleCommUtils.m26098b().m26111n(bArr, i11 + 4);
                            i11 += 8;
                            this.f28013c.add(new BleFile(jM26111n, jM26111n2, 0, 0));
                            i12++;
                        }
                    }
                    break;
                case 2:
                case 3:
                case 4:
                case 5:
                case 6:
                    int length2 = (bArr.length - 11) / 9;
                    if (length2 > 0) {
                        while (i12 < length2) {
                            long jM26111n3 = TntBleCommUtils.m26098b().m26111n(bArr, i11);
                            long jM26111n4 = TntBleCommUtils.m26098b().m26111n(bArr, i11 + 4);
                            int iM26108k = TntBleCommUtils.m26098b().m26108k(bArr, i11 + 8);
                            i11 += 9;
                            this.f28013c.add(new BleFile(jM26111n3, jM26111n4, iM26108k, 0));
                            i12++;
                        }
                    }
                    break;
                default:
                    int length3 = (bArr.length - 11) / 10;
                    if (length3 > 0) {
                        while (i12 < length3) {
                            long jM26111n5 = TntBleCommUtils.m26098b().m26111n(bArr, i11);
                            long jM26111n6 = TntBleCommUtils.m26098b().m26111n(bArr, i11 + 4);
                            int iM26108k2 = TntBleCommUtils.m26098b().m26108k(bArr, i11 + 8);
                            int iM26108k3 = TntBleCommUtils.m26098b().m26108k(bArr, i11 + 9);
                            i11 += 10;
                            this.f28013c.add(new BleFile(jM26111n5, jM26111n6, iM26108k3, iM26108k2));
                            i12++;
                        }
                    }
                    break;
            }
        }
    }

    /* JADX INFO: renamed from: c */
    public ArrayList m36076c() {
        return this.f28013c;
    }

    /* JADX INFO: renamed from: d */
    public int m36077d() {
        return this.f28012b;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "GetRecSessionsRsp{totals=%d, fileListSize=%d}", Integer.valueOf(this.f28012b), Integer.valueOf(this.f28013c.size()));
    }
}
