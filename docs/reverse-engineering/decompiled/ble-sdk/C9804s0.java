package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import kotlin.jvm.internal.AbstractC8694o;

/* JADX INFO: renamed from: oh.s0 */
/* JADX INFO: loaded from: classes3.dex */
public final class C9804s0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final int f28003b;

    /* JADX INFO: renamed from: c */
    public final int f28004c;

    /* JADX INFO: renamed from: d */
    public final int f28005d;

    /* JADX WARN: 'super' call moved to the top of the method (can break code semantics) */
    public C9804s0(byte[] data) {
        super(data);
        AbstractC8694o.m32338e(data, "data");
        TntBleCommUtils tntBleCommUtilsM26098b = TntBleCommUtils.m26098b();
        AbstractC8694o.m32337d(tntBleCommUtilsM26098b, "getInstant(...)");
        this.f28003b = tntBleCommUtilsM26098b.m26108k(data, 3);
        this.f28004c = tntBleCommUtilsM26098b.m26108k(data, 4);
        this.f28005d = tntBleCommUtilsM26098b.m26109l(data, 5);
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 144;
    }

    /* JADX INFO: renamed from: b */
    public final int m36069b() {
        return this.f28005d;
    }

    /* JADX INFO: renamed from: c */
    public final int m36070c() {
        return this.f28004c;
    }

    /* JADX INFO: renamed from: d */
    public final int m36071d() {
        return this.f28003b;
    }

    public String toString() {
        return "SyncDeviceLogFileEndRsp(logType=" + this.f28003b + ", logIndex=" + this.f28004c + ", crc=" + this.f28005d + ')';
    }
}
