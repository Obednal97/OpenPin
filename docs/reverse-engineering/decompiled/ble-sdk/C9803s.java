package p279oh;

import java.util.Locale;
import th.AbstractC11072r;

/* JADX INFO: renamed from: oh.s */
/* JADX INFO: loaded from: classes3.dex */
public class C9803s extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final byte[] f28002b;

    public C9803s(byte[] bArr) throws Exception {
        super(bArr);
        if (bArr.length < 7) {
            throw new Exception("GetDeviceStatusRsp: data length insufficient, expected at least 7 bytes, got " + bArr.length);
        }
        this.f28002b = new byte[4];
        for (int i10 = 0; i10 < 4; i10++) {
            this.f28002b[i10] = bArr[3 + i10];
        }
        AbstractC11072r.m40029a(this.f27941a, "Device status received: rawStatus=[%02X,%02X,%02X,%02X]", Integer.valueOf(this.f28002b[0] & 255), Integer.valueOf(this.f28002b[1] & 255), Integer.valueOf(this.f28002b[2] & 255), Integer.valueOf(this.f28002b[3] & 255));
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 141;
    }

    /* JADX INFO: renamed from: b */
    public byte[] m36068b() {
        return (byte[]) this.f28002b.clone();
    }

    public String toString() {
        return String.format(Locale.getDefault(), "GetDeviceStatusRsp{rawStatus=[%02X,%02X,%02X,%02X]}", Integer.valueOf(this.f28002b[0] & 255), Integer.valueOf(this.f28002b[1] & 255), Integer.valueOf(this.f28002b[2] & 255), Integer.valueOf(this.f28002b[3] & 255));
    }
}
