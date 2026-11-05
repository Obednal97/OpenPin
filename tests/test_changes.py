import threading

from app.utils import changes


def test_bump_increments_and_current_reads():
    start = changes.current()
    assert changes.bump() == start + 1
    assert changes.current() == start + 1


def test_bump_is_thread_safe():
    start = changes.current()
    threads = [threading.Thread(target=lambda: [changes.bump() for _ in range(200)]) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert changes.current() == start + 1000
