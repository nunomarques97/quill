"""quill.audio tests with a fake waveIn: the microphone is never opened and no sound is played."""

import threading
import unittest
from array import array

from quill.audio import BYTES_PER_SECOND, AudioError, Capture, MmeBuffer


def tone(seconds, level=2000):
    return array("h", [level if i % 2 else -level for i in range(int(seconds * 16_000))]).tobytes()


class FakeWaveIn:
    """Stands in for WinMM: queued buffers are filled by ``advance``."""

    def __init__(self):
        self.pending = []
        self.opened = self.closed = self.unprepared = 0
        self.level = 2000

    def open(self, index, rate):
        self.opened += 1
        return "handle"

    def new_buffer(self, handle, size):
        return MmeBuffer(size, None, {"done": False, "data": b""})

    def add(self, handle, buffer):
        buffer.header.update(done=False, data=b"")
        self.pending.append(buffer)

    def is_done(self, buffer):
        return buffer.header["done"]

    def recorded(self, buffer):
        return buffer.header["data"]

    def start(self, handle):
        pass

    def reset(self, handle):
        for buffer in self.pending:
            buffer.header["done"] = True
        self.pending = []

    def unprepare(self, handle, buffer):
        self.unprepared += 1

    def close(self, handle):
        self.closed += 1

    def advance(self, seconds):
        data = tone(seconds, self.level)
        while data and self.pending:
            buffer = self.pending[0]
            room = buffer.size - len(buffer.header["data"])
            buffer.header["data"] += data[:room]
            data = data[room:]
            if len(buffer.header["data"]) == buffer.size:
                buffer.header["done"] = True
                self.pending.pop(0)


class IncrementalReadTest(unittest.TestCase):
    def capture(self, **kwargs):
        fake = FakeWaveIn()
        return fake, Capture(fake, 0, background=False, buffer_s=0.05, buffers=8, **kwargs)

    def test_read_new_returns_each_chunk_once_in_order(self):
        fake, capture = self.capture()
        capture.start()
        self.assertEqual(capture.read_new(), b"")
        fake.level = 1000
        fake.advance(0.1)
        capture.pump()
        first = capture.read_new()
        self.assertEqual(len(first), int(0.1 * BYTES_PER_SECOND))
        self.assertEqual(capture.read_new(), b"")
        fake.level = 3000
        fake.advance(0.05)
        capture.pump()
        second = capture.read_new()
        self.assertEqual(second, tone(0.05, 3000))
        self.assertAlmostEqual(capture.collected_s, 0.15)
        # A partly filled buffer is collected at stop; the result holds everything once.
        fake.advance(0.02)
        result = capture.stop()
        self.assertEqual(result.pcm, first + second + capture.read_new())
        self.assertEqual((fake.opened, fake.closed), (1, 1))

    def test_restart_resets_the_read_position(self):
        fake, capture = self.capture()
        capture.start()
        fake.advance(0.1)
        capture.pump()
        capture.read_new()
        capture.stop()
        capture.start()
        self.assertEqual((capture.read_new(), capture.collected_s), (b"", 0.0))
        fake.advance(0.05)
        capture.pump()
        self.assertEqual(len(capture.read_new()), int(0.05 * BYTES_PER_SECOND))
        capture.stop()

    def test_on_data_receives_chunks_in_order(self):
        received = []
        fake, capture = self.capture(on_data=received.append)
        capture.start()
        for level in (500, 1500, 2500):
            fake.level = level
            fake.advance(0.05)
            capture.pump()
        result = capture.stop()
        self.assertEqual(b"".join(received), result.pcm)
        self.assertEqual(received[:3], [tone(0.05, 500), tone(0.05, 1500), tone(0.05, 2500)])

    def test_on_data_failure_in_the_thread_fails_the_capture_and_releases(self):
        fake = FakeWaveIn()
        failed = threading.Event()

        def broken(chunk):
            failed.set()
            raise ValueError("consumer failed")

        capture = Capture(fake, 0, buffer_s=0.05, buffers=4, poll_s=0.001, on_data=broken)
        capture.start()
        fake.advance(0.05)
        self.assertTrue(failed.wait(5))
        with self.assertRaises(AudioError):
            capture.stop()
        self.assertEqual((fake.closed, capture.active), (1, False))


if __name__ == "__main__":
    unittest.main()
