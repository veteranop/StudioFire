"""Feed the running engine the demo queue (real speakers). Usage:
python tests/demo_feed.py            -> queue both test MP3s + print status
python tests/demo_feed.py status     -> just print status
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:7701"


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=5) as r:
        return json.loads(r.read())


if len(sys.argv) > 1 and sys.argv[1] == "status":
    print(json.dumps(call("GET", "/status"), indent=2))
    sys.exit(0)

audio = os.path.join(ROOT, "media", "Audio")
entries = [
    {"id": "demo1", "path": os.path.join(audio, "test-one.mp3"),
     "title": "Test One", "source": "playlist"},
    {"id": "demo2", "path": os.path.join(audio, "test2.mp3"),
     "title": "Test Two", "source": "playlist"},
]
print(call("POST", "/queue",
           {"op": "replace", "queue_version": 1, "entries": entries}))
print(json.dumps(call("GET", "/status"), indent=2))
