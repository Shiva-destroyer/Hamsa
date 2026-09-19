"""50 rapid messages from 10 numbers (5 each, all numbers in parallel), in-process, no network.
Passes when: no handler error, no exception, every number got exactly ONE verdict (the 10 s cooldown was respected, the other four
lookups got the 'one moment' reply), and a re-delivered wamid produced nothing.
    DATABASE_URL=... python scripts/simulate_load.py"""
import os, sys, threading, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import Chat
import flow, wa
from texts import S

NUMBERS, PER_NUMBER = 10, 5
BATCHES = ["AX2291", "EN3302", "MQ7756", "NS3340", "FM9184"]
ERRORS = {S["error"][l] for l in S["error"]}
WAIT = {S["wait"][l] for l in S["wait"]}


def main():
    chats = [Chat() for _ in range(NUMBERS)]
    for c in chats: c.wipe(); c.onboard("en")
    log, errors = {c.sender: [] for c in chats}, []

    def worker(c):
        try:
            for b in BATCHES[:PER_NUMBER]:
                flow.handle(wa.Inbound("wamid." + uuid.uuid4().hex, c.sender, "text", b))
                log[c.sender].extend(list(c.out)); c.out.clear()
        except Exception as e:                                   # flow.handle must never raise
            errors.append(repr(e))

    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(c,)) for c in chats]
    [x.start() for x in threads]; [x.join() for x in threads]
    dt = time.time() - t0
    dup = Chat(chats[0].sender); dup.out.clear()
    m = wa.Inbound("wamid.load-dup", chats[0].sender, "text", "HELP"); flow.handle(m); n1 = len(dup.out); flow.handle(m)

    verdicts = {s: sum(1 for o in msgs if o["type"] == "audio") for s, msgs in log.items()}
    waits = {s: sum(1 for o in msgs if o["type"] == "text" and o["text"]["body"] in WAIT) for s, msgs in log.items()}
    fallbacks = sum(1 for msgs in log.values() for o in msgs if o["type"] == "text" and o["text"]["body"] in ERRORS)
    problems = list(errors)
    if fallbacks: problems.append(f"{fallbacks} handler-error fallback replies")
    problems += [f"number {s[-4:]} got {v} verdicts (want 1)" for s, v in verdicts.items() if v != 1]
    problems += [f"number {s[-4:]} got {w} 'wait' replies (want {PER_NUMBER - 1})" for s, w in waits.items() if w != PER_NUMBER - 1]
    if len(dup.out) != n1: problems.append("re-delivered wamid was processed twice")
    for c in chats: c.wipe()
    print(f"{NUMBERS * PER_NUMBER} messages / {NUMBERS} numbers in {dt:.1f}s ({NUMBERS * PER_NUMBER / dt:.0f} msg/s); verdicts={sum(verdicts.values())} waits={sum(waits.values())}")
    if problems:
        print("LOAD TEST FAILED:\n  " + "\n  ".join(problems)); sys.exit(1)
    print("LOAD OK: no exceptions, cooldown respected, dedupe held")


if __name__ == "__main__":
    main()
