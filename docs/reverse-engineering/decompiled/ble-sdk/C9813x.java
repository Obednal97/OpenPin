package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.x */
/* JADX INFO: loaded from: classes3.dex */
public class C9813x extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final String f28024b;

    /* JADX INFO: renamed from: c */
    public String f28025c;

    public C9813x(byte[] bArr) {
        super(bArr);
        int i10 = 3;
        while (true) {
            if (i10 >= 59) {
                i10 = 3;
                break;
            } else if (bArr[i10] == 0) {
                break;
            } else {
                i10++;
            }
        }
        if (i10 == 3) {
            this.f28024b = "0000000000000000000000000000";
        } else {
            this.f28024b = new String(bArr, 3, i10 - 3);
        }
        if (bArr.length > 59) {
            this.f28025c = new String(bArr, 59, 1);
            this.f28025c += String.format(Locale.getDefault(), "%04d", Long.valueOf(TntBleCommUtils.m26098b().m26110m(bArr, 60)));
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 2;
    }

    /* JADX INFO: renamed from: b */
    public String m36088b() {
        return this.f28024b;
    }

    public String toString() {
        return String.format("GetSsnRsp{ssn='%s', versionName='%s'}", this.f28024b, this.f28025c);
    }
}
