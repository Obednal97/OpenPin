package p279oh;

import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import kotlin.jvm.internal.DefaultConstructorMarker;
import okhttp3.internal.p284ws.WebSocketProtocol;

/* JADX INFO: renamed from: oh.c0 */
/* JADX INFO: loaded from: classes3.dex */
public final class C9772c0 extends AbstractC9777f {

    /* JADX INFO: renamed from: c */
    public static final a f27922c = new a(null);

    /* JADX INFO: renamed from: b */
    public final byte[] f27923b;

    /* JADX INFO: renamed from: oh.c0$a */
    public static final class a {
        public /* synthetic */ a(DefaultConstructorMarker defaultConstructorMarker) {
            this();
        }

        public a() {
        }
    }

    public C9772c0(byte[] bArr) {
        super(bArr);
        this.f27923b = bArr;
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return WebSocketProtocol.PAYLOAD_SHORT;
    }

    /* JADX INFO: renamed from: b */
    public final int m36006b() {
        if (this.f27923b == null) {
            return 0;
        }
        return TntBleCommUtils.m26098b().m26108k(this.f27923b, 3);
    }
}
