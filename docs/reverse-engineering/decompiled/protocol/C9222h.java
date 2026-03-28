package p225lh;

import bg.InterfaceC2607c;
import java.util.List;
import kotlin.jvm.internal.AbstractC8694o;

/* JADX INFO: renamed from: lh.h */
/* JADX INFO: loaded from: classes3.dex */
public final class C9222h {

    /* JADX INFO: renamed from: a */
    @InterfaceC2607c("logType")
    private final int f26446a;

    /* JADX INFO: renamed from: b */
    @InterfaceC2607c("logIndex")
    private final int f26447b;

    /* JADX INFO: renamed from: c */
    @InterfaceC2607c("isLZ4")
    private final int f26448c;

    /* JADX INFO: renamed from: d */
    @InterfaceC2607c("fileSize")
    private final long f26449d;

    /* JADX INFO: renamed from: e */
    @InterfaceC2607c("reserved")
    private final List<Integer> f26450e;

    public C9222h(int i10, int i11, int i12, long j10, List reserved) {
        AbstractC8694o.m32338e(reserved, "reserved");
        this.f26446a = i10;
        this.f26447b = i11;
        this.f26448c = i12;
        this.f26449d = j10;
        this.f26450e = reserved;
    }

    public boolean equals(Object obj) {
        if (this == obj) {
            return true;
        }
        if (!(obj instanceof C9222h)) {
            return false;
        }
        C9222h c9222h = (C9222h) obj;
        return this.f26446a == c9222h.f26446a && this.f26447b == c9222h.f26447b && this.f26448c == c9222h.f26448c && this.f26449d == c9222h.f26449d && AbstractC8694o.m32334a(this.f26450e, c9222h.f26450e);
    }

    public int hashCode() {
        return (((((((Integer.hashCode(this.f26446a) * 31) + Integer.hashCode(this.f26447b)) * 31) + Integer.hashCode(this.f26448c)) * 31) + Long.hashCode(this.f26449d)) * 31) + this.f26450e.hashCode();
    }

    public String toString() {
        return "DeviceLogFile(logType=" + this.f26446a + ", logIndex=" + this.f26447b + ", isLZ4=" + this.f26448c + ", fileSize=" + this.f26449d + ", reserved=" + this.f26450e + ')';
    }
}
