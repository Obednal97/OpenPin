package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.d0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9774d0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f27927b;

    /* JADX INFO: renamed from: c */
    public final int f27928c;

    /* JADX INFO: renamed from: d */
    public final int f27929d;

    /* JADX INFO: renamed from: e */
    public int f27930e;

    /* JADX INFO: renamed from: f */
    public int f27931f;

    /* JADX INFO: renamed from: g */
    public boolean f27932g;

    /* JADX INFO: renamed from: h */
    public boolean f27933h;

    /* JADX INFO: renamed from: i */
    public boolean f27934i;

    /* JADX INFO: renamed from: j */
    public String f27935j;

    /* JADX INFO: renamed from: k */
    public int f27936k;

    public C9774d0(byte[] bArr) {
        super(bArr);
        this.f27930e = 0;
        this.f27931f = 1;
        this.f27932g = false;
        this.f27933h = false;
        this.f27934i = false;
        this.f27935j = "V";
        this.f27936k = 0;
        this.f27927b = TntBleCommUtils.m26098b().m26108k(bArr, 3);
        this.f27928c = TntBleCommUtils.m26098b().m26109l(bArr, 4);
        this.f27929d = TntBleCommUtils.m26098b().m26108k(bArr, 6);
        if (bArr.length >= 8) {
            this.f27930e = TntBleCommUtils.m26098b().m26108k(bArr, 7);
            if (bArr.length >= 9) {
                this.f27931f = TntBleCommUtils.m26098b().m26108k(bArr, 8);
                if (bArr.length >= 10) {
                    this.f27932g = TntBleCommUtils.m26098b().m26108k(bArr, 9) == 1;
                    if (bArr.length >= 11) {
                        this.f27933h = TntBleCommUtils.m26098b().m26108k(bArr, 10) == 1;
                        if (bArr.length >= 12) {
                            this.f27934i = TntBleCommUtils.m26098b().m26108k(bArr, 11) == 1;
                            if (bArr.length >= 4) {
                                this.f27935j = String.valueOf((char) (bArr[bArr.length - 4] & 255));
                                this.f27936k = ((bArr[bArr.length - 1] & 255) << 16) | (bArr[bArr.length - 3] & 255) | ((bArr[bArr.length - 2] & 255) << 8);
                            }
                        }
                    }
                }
            }
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 1;
    }

    /* JADX INFO: renamed from: b */
    public int m36010b() {
        return this.f27931f;
    }

    /* JADX INFO: renamed from: c */
    public int m36011c() {
        return this.f27928c;
    }

    /* JADX INFO: renamed from: d */
    public int m36012d() {
        return this.f27927b;
    }

    /* JADX INFO: renamed from: e */
    public int m36013e() {
        return this.f27929d;
    }

    /* JADX INFO: renamed from: f */
    public int m36014f() {
        return this.f27930e;
    }

    /* JADX INFO: renamed from: g */
    public int m36015g() {
        return this.f27936k;
    }

    /* JADX INFO: renamed from: h */
    public String m36016h() {
        return this.f27935j;
    }

    /* JADX INFO: renamed from: i */
    public boolean m36017i() {
        return this.f27933h;
    }

    /* JADX INFO: renamed from: j */
    public boolean m36018j() {
        return this.f27934i;
    }

    /* JADX INFO: renamed from: k */
    public boolean m36019k() {
        return this.f27932g;
    }

    public String toString() {
        return String.format(this.f27941a + "{status=%d, portVersion=%d, timezone=%d, timezoneMin=%d, audioChannel=%d, supportWifi=%s, noNsAgc=%s, isOggAudio=%s, versionType=%s, version=%d}", Integer.valueOf(this.f27927b), Integer.valueOf(this.f27928c), Integer.valueOf(this.f27929d), Integer.valueOf(this.f27930e), Integer.valueOf(this.f27931f), Boolean.valueOf(this.f27932g), Boolean.valueOf(this.f27933h), Boolean.valueOf(this.f27934i), this.f27935j, Integer.valueOf(this.f27936k));
    }
}
