package p279oh;

import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import th.AbstractC11073s;

/* JADX INFO: renamed from: oh.m0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9792m0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public String f27975b;

    public C9792m0(byte[] bArr, int i10) {
        super(bArr);
        this.f27975b = AbstractC11073s.m40048d(bArr);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 130;
    }

    /* JADX INFO: renamed from: b */
    public String m36046b() {
        return this.f27975b.length() > 6 ? m36047c(this.f27975b.substring(6).toLowerCase()) : "";
    }

    /* JADX INFO: renamed from: c */
    public String m36047c(String str) {
        Matcher matcher = Pattern.compile("(?:00)+$").matcher(str);
        return matcher.find() ? str.substring(0, matcher.start()) : str;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "oriByteStr = [%s] SetSoundPlusKeyRsp{} ", this.f27975b);
    }
}
