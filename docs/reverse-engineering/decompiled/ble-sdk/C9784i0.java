package p279oh;

import androidx.media3.exoplayer.rtsp.RtspMediaSource;
import com.tinnotech.penblesdk.utils.TntBleCommUtils;
import java.util.Locale;

/* JADX INFO: renamed from: oh.i0 */
/* JADX INFO: loaded from: classes3.dex */
public class C9784i0 extends AbstractC9777f {

    /* JADX INFO: renamed from: b */
    public final long f27957b;

    /* JADX INFO: renamed from: c */
    public long f27958c;

    /* JADX INFO: renamed from: d */
    public final int f27959d;

    /* JADX INFO: renamed from: e */
    public int f27960e;

    /* JADX INFO: renamed from: f */
    public long f27961f;

    public C9784i0(byte[] bArr) {
        super(bArr);
        this.f27960e = 0;
        this.f27961f = 0L;
        this.f27957b = TntBleCommUtils.m26098b().m26111n(bArr, 3);
        this.f27958c = TntBleCommUtils.m26098b().m26111n(bArr, 7);
        this.f27959d = TntBleCommUtils.m26098b().m26108k(bArr, 11);
        if (bArr.length >= 13) {
            this.f27960e = TntBleCommUtils.m26098b().m26108k(bArr, 12);
            if (bArr.length >= 17) {
                this.f27961f = TntBleCommUtils.m26098b().m26111n(bArr, 13);
            }
        }
        if (this.f27958c <= RtspMediaSource.DEFAULT_TIMEOUT_MS) {
            this.f27958c = 0L;
        }
    }

    @Override // p279oh.AbstractC9777f
    /* JADX INFO: renamed from: a */
    public int mo35995a() {
        return 20;
    }

    /* JADX INFO: renamed from: b */
    public int m36033b() {
        return this.f27960e;
    }

    /* JADX INFO: renamed from: c */
    public long m36034c() {
        return this.f27957b;
    }

    /* JADX INFO: renamed from: d */
    public long m36035d() {
        return this.f27958c;
    }

    /* JADX INFO: renamed from: e */
    public int m36036e() {
        return this.f27959d;
    }

    public String toString() {
        return String.format(Locale.getDefault(), "RecordStartRsp{sessionId=%d, start=%d, status=%d, scene=%d, startTime=%d}", Long.valueOf(this.f27957b), Long.valueOf(this.f27958c), Integer.valueOf(this.f27959d), Integer.valueOf(this.f27960e), Long.valueOf(this.f27961f));
    }
}
