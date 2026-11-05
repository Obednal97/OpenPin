"""Progress wording shared by the Bluetooth sync and the Wi-Fi sync worker.

Progress counts only the recordings that are actually being downloaded (not
every file on the device), and moves with the bytes received, so a sync of
2 new recordings out of 47 on the device goes 1 of 2, 2 of 2, 0 to 100%.
"""


def format_mb(n_bytes):
    """Bytes as megabytes with one decimal place (decimal MB, as Finder shows)."""
    mb = max(0, n_bytes or 0) / 1_000_000
    if 0 < mb < 0.1:
        return "0.1 MB"
    return f"{mb:.1f} MB"


def plan_message(count, total_bytes):
    """What the device has that this Mac doesn't, before any download starts."""
    if count <= 0:
        return "No new recordings to sync"
    noun = "recording" if count == 1 else "recordings"
    return f"{count} new {noun} to sync ({format_mb(total_bytes)})"


def progress(index, count, done_bytes, total_bytes):
    """(message, fraction 0..1) while downloading recording `index` (1-based) of `count`.

    `done_bytes` counts every byte received so far across the whole sync,
    including the recording in flight.
    """
    count = max(count, 1)
    index = min(max(index, 1), count)
    if total_bytes and total_bytes > 0:
        done_bytes = min(max(done_bytes, 0), total_bytes)
        fraction = done_bytes / total_bytes
        sizes = f" · {format_mb(done_bytes)} of {format_mb(total_bytes)}"
    else:
        fraction = (index - 1) / count
        sizes = ""
    return f"Syncing recording {index} of {count}{sizes}", fraction
