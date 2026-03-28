package p279oh;

import java.util.Arrays;
import kotlin.jvm.internal.AbstractC8694o;
import kotlin.jvm.internal.DefaultConstructorMarker;
import kotlin.text.C8713c;

/* JADX INFO: renamed from: oh.h */
/* JADX INFO: loaded from: classes3.dex */
public final class C9781h extends AbstractC9777f {

    /* JADX INFO: renamed from: c */
    public static final a f27949c = new a(null);

    /* JADX INFO: renamed from: b */
    public final byte[] f27950b;

    /* JADX INFO: renamed from: oh.h$a */
    public static final class a {
        public /* synthetic */ a(DefaultConstructorMarker defaultConstructorMarker) {
            this();
        }

        public a() {
        }
    }

    public C9781h(byte[] bArr) {
        super(bArr);
        this.f27950b = bArr;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 108;
    }

    /* JADX INFO: renamed from: b */
    public final String m36028b() {
        byte[] bArr = this.f27950b;
        if (bArr == null) {
            return "";
        }
        int length = bArr.length - 3;
        StringBuffer stringBuffer = new StringBuffer();
        for (byte b10 : this.f27950b) {
            String str = String.format("%02X ", Arrays.copyOf(new Object[]{Byte.valueOf(b10)}, 1));
            AbstractC8694o.m32337d(str, "format(...)");
            stringBuffer.append(str);
        }
        int length2 = this.f27950b.length;
        int i10 = 3;
        while (true) {
            if (i10 >= length2) {
                break;
            }
            if (this.f27950b[i10] == 0) {
                length = i10 - 3;
                break;
            }
            i10++;
        }
        return new String(this.f27950b, 3, length, C8713c.f25417b);
    }
}
