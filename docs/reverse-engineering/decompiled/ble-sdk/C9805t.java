package p279oh;

import java.util.Locale;
import th.AbstractC11073s;

/* JADX INFO: renamed from: oh.t */
/* JADX INFO: loaded from: classes3.dex */
public class C9805t extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public String f28006b;

    public C9805t(byte[] bArr, int i10) {
        super(bArr);
        this.f28006b = AbstractC11073s.m40048d(bArr);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 131;
    }

    /* JADX INFO: renamed from: b */
    public String m36072b() {
        return this.f28006b.length() > 6 ? this.f28006b.substring(6).toLowerCase() : "";
    }

    public String toString() {
        return String.format(Locale.getDefault(), "oriByteStr = [%s] GetFlashCidRsp{} ", this.f28006b);
    }
}
