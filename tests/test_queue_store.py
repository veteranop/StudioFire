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

        # ---- reorder / remove operate only on the pending tail ----
        s = QueueState(queue_version=1, current_index=1, entries=[
            {"id": "p0", "path": "0"}, {"id": "cur", "path": "1"},
            {"id": "a", "path": "a"}, {"id": "b", "path": "b"},
            {"id": "c", "path": "c"}])
        ok, _ = apply_mutation(s, {"op": "reorder", "queue_version": 2,
                                   "order": ["c", "a", "b"]})
        check("reorder rewrites pending order",
              ok and [e["id"] for e in s.entries]
              == ["p0", "cur", "c", "a", "b"])
        # played + current are immune even if named in order
        ok, _ = apply_mutation(s, {"op": "reorder", "queue_version": 3,
                                   "order": ["cur", "p0", "b"]})
        check("reorder cannot touch played/current",
              ok and s.entries[0]["id"] == "p0"
              and s.entries[1]["id"] == "cur"
              and [e["id"] for e in s.entries[2:]] == ["b", "c", "a"])
        # unnamed pending ids keep their place at the end
        ok, _ = apply_mutation(s, {"op": "reorder", "queue_version": 4,
                                   "order": ["a"]})
        check("reorder keeps unnamed pending",
              ok and [e["id"] for e in s.entries[2:]] == ["a", "b", "c"])
        ok, _ = apply_mutation(s, {"op": "remove", "queue_version": 5,
                                   "ids": ["b", "cur", "p0"]})
        check("remove drops pending only (current/played immune)",
              ok and [e["id"] for e in s.entries]
              == ["p0", "cur", "a", "c"])

        # ---- trim_history: bound the runtime queue, keep current + pending ----
        def mk(n, cur):
            st = QueueState(entries=[{"id": f"e{i}", "path": f"{i}"}
                                     for i in range(n)], current_index=cur)
            return st
        st = mk(100, 90)          # 90 played, current at 90, 9 pending
        dropped = st.trim_history(20)
        check("trim drops old played beyond keep", dropped == 70
              and len(st.entries) == 30 and st.current_index == 20)
        check("trim keeps the currently-playing entry",
              st.current_entry()["id"] == "e90")
        check("trim keeps all pending after current",
              st.entries[-1]["id"] == "e99" and st.next_entry()["id"] == "e91")
        check("trim is a no-op when history is already short",
              mk(25, 10).trim_history(20) == 0)
        check("trim no-op keeps entries intact", len(mk(25, 10).entries) == 25)

    print(f"QUEUE STORE OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
