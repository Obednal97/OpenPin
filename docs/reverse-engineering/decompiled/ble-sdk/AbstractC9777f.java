package p279oh;

import bg.InterfaceC2607c;
import com.tinnotech.penblesdk.utils.TntBleCommUtils;

/* JADX INFO: renamed from: oh.f */
/* JADX INFO: loaded from: classes3.dex */
public abstract class AbstractC9777f {

    /* JADX INFO: renamed from: a */
    @InterfaceC2607c("tag")
    public String f27941a = getClass().getSimpleName();

    public AbstractC9777f(byte[] bArr) throws Exception {
        if (TntBleCommUtils.m26098b().m26109l(bArr, 1) == mo35995a()) {
            return;
        }
        throw new Exception(this.f27941a + " Mismatch");
    }

    /* JADX INFO: renamed from: a */
    public abstract int mo35995a();
}
