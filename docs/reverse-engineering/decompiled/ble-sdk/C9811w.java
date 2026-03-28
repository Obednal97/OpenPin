package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/* JADX INFO: renamed from: oh.w */
/* JADX INFO: loaded from: classes3.dex */
public class C9811w extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final byte[] f28016b;

    /* JADX INFO: renamed from: c */
    public final int f28017c;

    /* JADX INFO: renamed from: d */
    public final int f28018d;

    /* JADX INFO: renamed from: e */
    public final List f28019e;

    /* JADX INFO: renamed from: oh.w$a */
    public static class a {

        /* JADX INFO: renamed from: a */
        public final long f28020a;

        /* JADX INFO: renamed from: b */
        public final int f28021b;

        /* JADX INFO: renamed from: c */
        public final int f28022c;

        /* JADX INFO: renamed from: d */
        public final byte[] f28023d;

        public a(long j10, int i10, int i11, byte[] bArr) {
            this.f28020a = j10;
            this.f28021b = i10;
            this.f28022c = i11;
            this.f28023d = (bArr == null || bArr.length != 2) ? new byte[2] : bArr;
        }

        /* JADX INFO: renamed from: a */
        public byte[] m36084a() {
            return this.f28023d;
        }

        /* JADX INFO: renamed from: b */
        public int m36085b() {
            return this.f28022c;
        }

        /* JADX INFO: renamed from: c */
        public long m36086c() {
            return this.f28020a;
        }

        /* JADX INFO: renamed from: d */
        public int m36087d() {
            return this.f28021b;
        }

        public String toString() {
            return String.format(Locale.getDefault(), "RecordTagInfo{timestamp=%d, type=%d, status=%d, reserved=[%d,%d]}", Long.valueOf(this.f28020a), Integer.valueOf(this.f28021b), Integer.valueOf(this.f28022c), Integer.valueOf(this.f28023d[0] & 255), Integer.valueOf(this.f28023d[1] & 255));
        }
    }

    public C9811w(byte[] bArr) {
        super(bArr);
        byte[] bArr2 = new byte[4];
        this.f28016b = bArr2;
        System.arraycopy(bArr, 3, bArr2, 0, 4);
        this.f28017c = TntBleCommUtils.m26098b().m26109l(bArr, 7);
        this.f28018d = TntBleCommUtils.m26098b().m26109l(bArr, 9);
        this.f28019e = new ArrayList();
        int i10 = 11;
        while (i10 + 8 <= bArr.length && this.f28019e.size() < 20) {
            long jM26111n = TntBleCommUtils.m26098b().m26111n(bArr, i10);
            int iM26108k = TntBleCommUtils.m26098b().m26108k(bArr, i10 + 4);
            int iM26108k2 = TntBleCommUtils.m26098b().m26108k(bArr, i10 + 5);
            byte[] bArr3 = {bArr[i10 + 6], bArr[i10 + 7]};
            i10 += 8;
            this.f28019e.add(new a(jM26111n, iM26108k, iM26108k2, bArr3));
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 38;
    }

    /* JADX INFO: renamed from: b */
    public int m36078b() {
        return this.f28019e.size();
    }

    /* JADX INFO: renamed from: c */
    public int m36079c() {
        return this.f28018d;
    }

    /* JADX INFO: renamed from: d */
    public List m36080d() {
        return this.f28019e;
    }

    /* JADX INFO: renamed from: e */
    public int m36081e() {
        return this.f28017c;
    }

    /* JADX INFO: renamed from: f */
    public String m36082f() {
        StringBuilder sb2 = new StringBuilder();
        for (byte b10 : this.f28016b) {
            sb2.append(String.format("%02X", Byte.valueOf(b10)));
        }
        return sb2.toString();
    }

    /* JADX INFO: renamed from: g */
    public String m36083g() {
        byte[] bArr = this.f28016b;
        return String.format("%08X", Integer.valueOf(((bArr[3] & 255) << 24) | (bArr[0] & 255) | ((bArr[1] & 255) << 8) | ((bArr[2] & 255) << 16)));
    }

    public String toString() {
        return String.format(Locale.getDefault(), "GetRecTagsRsp{uid=%s, totals=%d, index=%d, actualCount=%d, tags=%s}", m36082f(), Integer.valueOf(this.f28017c), Integer.valueOf(this.f28018d), Integer.valueOf(m36078b()), this.f28019e);
    }
}
