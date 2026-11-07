#!/usr/bin/env python3
"""
Listening loop checks — fake microphone, fake clock, no hardware.

    python test_listen.py

The case from Oct 12: a loud blip right after he stops talking (his own echo
or the jaw servo) used to end the listening window, and the voice detector's
"not speech" verdict became "no answer" -> taunt within a second. Noises must
be ignored and he keeps listening for the real answer.
"""

import types

import numpy as np

import listener

CHUNK_S = 1024 / 48000


class Clock:
    t = 1000.0

    def time(self):
        return self.t


def chunk(loud: bool) -> bytes:
    amp = 3000 if loud else 100
    return (np.full(1024, amp, np.int16)).tobytes()


class FakeStream:
    def __init__(self, script, clock, stall_at=None):
        self.script, self.clock, self.i = script, clock, 0
        self.stall_at = stall_at
        self.closed = False

    def read(self, n, exception_on_overflow=False):
        self.clock.t += CHUNK_S
        loud = self.script[self.i] if self.i < len(self.script) else False
        self.i += 1
        return chunk(loud)

    def get_read_available(self):
        if self.stall_at is not None and self.i >= self.stall_at:
            return 0  # the USB mic went silent with the stream open
        return 1024

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}{'' if cond or not extra else '  ' + str(extra)}")
    if not cond:
        raise SystemExit(1)


def run(script, vad_verdicts, stall_at=None, streams=None):
    """Run listen_for_response over a script of loud/quiet chunks.
    vad_verdicts: what the voice detector says, in order. Returns (result, heard frames, vad calls).
    stall_at: the mic stops sending audio after this many chunks."""
    clock = Clock()
    listener.time = types.SimpleNamespace(time=clock.time, sleep=lambda s: setattr(clock, "t", clock.t + s))
    listener.PYAUDIO_AVAILABLE = True
    listener._log_to_dashboard = lambda *a: None
    lst = listener.Listener.__new__(listener.Listener)
    streams = [] if streams is None else streams
    lst._pa = types.SimpleNamespace(open=lambda **kw: streams.append(FakeStream(script, clock, stall_at)) or streams[-1])
    lst._mic_index, lst._sample_rate = None, 48000
    verdicts, calls, heard = list(vad_verdicts), [], []
    lst._check_vad = lambda frames: calls.append(len(frames)) or (verdicts.pop(0) if verdicts else True)
    lst._transcribe = lambda frames, vad_checked=False: heard.append(frames) or "TRANSCRIBED"
    return lst.listen_for_response(), heard, calls


def secs(n):
    return int(n / CHUNK_S)


def main():
    import config
    config.LISTEN_DURATION, config.LISTEN_SILENCE_TIMEOUT, config.LISTEN_ENERGY_THRESHOLD = 6.0, 0.5, 700

    print("the Oct 12 case")
    blip = [False] * secs(0.18) + [True] * 7 + [False] * secs(2.0)
    answer = [True] * secs(1.0) + [False] * secs(1.0)
    result, heard, calls = run(blip + answer, vad_verdicts=[False, True])
    check("blip rejected, he keeps listening, the real answer is transcribed", result == "TRANSCRIBED" and len(calls) == 2)
    loud_in_heard = sum(1 for f in heard[0] if np.frombuffer(f, np.int16)[0] > 700)
    check("only the answer is transcribed, not the blip", loud_in_heard == secs(1.0), loud_in_heard)

    print("other cases")
    result, heard, calls = run([False] * 10 + [True] * 2 + [False] * secs(7), vad_verdicts=[])
    check("a 2-chunk click is dismissed without the voice detector", result is None and calls == [])
    result, heard, calls = run(blip, vad_verdicts=[False])
    check("only noise for the whole window -> no answer (taunt is right)", result is None)
    result, heard, calls = run([False] * secs(5.5) + [True] * secs(2), vad_verdicts=[True])
    check("still talking when time runs out -> keeps what they said", result == "TRANSCRIBED" and len(heard) == 1)
    result, heard, calls = run([False] * secs(7), vad_verdicts=[])
    check("silence -> no answer", result is None and calls == [])
    result, heard, calls = run([False] * 20 + [True] * secs(0.6) + [False] * secs(1), vad_verdicts=[True])
    pre = 0
    for f in heard[0]:
        if np.frombuffer(f, np.int16)[0] > 700:
            break
        pre += 1
    # the pre-roll holds PREROLL_CHUNKS including the chunk that triggered it
    check("a little audio from just before they spoke is kept (first syllable)", pre == listener.PREROLL_CHUNKS - 1, pre)

    print("the Oct 27 case")
    streams = []
    result, heard, calls = run([False] * secs(7), vad_verdicts=[], stall_at=10, streams=streams)
    took = listener.time.time() - Clock.t
    check("mic goes silent mid-listen -> gives up instead of hanging, stream closed",
          result is None and streams[0].closed)
    check("...after MIC_STALL_SECONDS, not the whole window", abs(took - (10 * CHUNK_S + listener.MIC_STALL_SECONDS)) < 0.1, took)

    print("\nAll listening checks passed.")


if __name__ == "__main__":
    main()
