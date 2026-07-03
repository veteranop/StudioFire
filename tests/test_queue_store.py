"""Unit tests: queue_store persistence, corruption recovery, version protocol.

Run: python tests/test_queue_store.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.queue_store import QueueState, QueueStore, apply_mutation  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def main():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "queue_state.json")
        store = QueueStore(path)

        # fresh start -> safe default, emergency mode
        s = store.load()
        check("fresh start is emergency", s.emergency_mode and s.entries == [])

        # roundtrip
        s = QueueState(queue_version=5,
                       entries=[{"id": "a", "path": "C:/x/a.mp3", "source": "playlist"},
                                {"id": "b", "path": "C:/x/b.mp3", "source": "playlist"}],
                       current_index=0, emergency_mode=False)
        store.save(s)
        s2 = store.load()
        check("roundtrip version", s2.queue_version == 5)
        check("roundtrip entries", len(s2.entries) == 2 and s2.entries[1]["id"] == "b")
        check("roundtrip index", s2.current_index == 0)
        check("current/next entry", s2.current_entry()["id"] == "a"
              and s2.next_entry()["id"] == "b")

        # leftover temp file from a crash mid-save is harmless
        with open(path + ".tmp", "w") as f:
            f.write("garbage from a crash")
        check("stale tmp ignored", store.load().queue_version == 5)

        # corrupt state file -> quarantined, safe default
        with open(path, "w") as f:
            f.write("{ not json !!!")
        s3 = store.load()
        check("corrupt -> emergency default", s3.emergency_mode and s3.queue_version == 0)
        check("corrupt file quarantined",
              any(".corrupt-" in fn for fn in os.listdir(td)))

        # structurally invalid (valid JSON, bad shape) -> quarantined
        with open(path, "w") as f:
            json.dump({"queue_version": 1, "entries": [{"nope": 1}],
                       "current_index": 0, "emergency_mode": False}, f)
        check("bad entry shape -> safe default", store.load().emergency_mode)

        # ---- version protocol ----
        s = QueueState(queue_version=10,
                       entries=[{"id": "a", "path": "a"}], current_index=0)
        ok, why = apply_mutation(s, {"op": "append", "queue_version": 10,
                                     "entries": [{"id": "b", "path": "b"}]})
        check("stale version rejected", not ok and "stale" in why)
        ok, _ = apply_mutation(s, {"op": "append", "queue_version": 11,
                                   "entries": [{"id": "b", "path": "b"}]})
        check("newer version accepted", ok and s.queue_version == 11
              and len(s.entries) == 2)
        ok, _ = apply_mutation(s, {"op": "insert_next", "queue_version": 12,
                                   "entries": [{"id": "spot", "path": "s"}]})
        check("insert_next lands after current",
              ok and s.entries[1]["id"] == "spot")
        ok, _ = apply_mutation(s, {"op": "replace", "queue_version": 13,
                                   "entries": [{"id": "z", "path": "z"}]})
        check("replace resets index", ok and s.current_index == -1
              and len(s.entries) == 1)
        ok, why = apply_mutation(s, {"op": "explode", "queue_version": 14})
        check("unknown op rejected, version unchanged",
              not ok and s.queue_version == 13)

    print(f"QUEUE STORE OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
