"""Stop only this workspace's local application, preserving checkpoints."""
from pathlib import Path
import sys
import httpx
from filelock import FileLock

root = Path(__file__).resolve().parents[1]
try:
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.get("http://127.0.0.1:7861/api/diagnostics")
        response.raise_for_status()
        if Path(response.json()["root"]).resolve() != root:
            raise RuntimeError("Port 7861 belongs to another workspace; nothing stopped")
        response = client.post("http://127.0.0.1:7861/api/shutdown")
        response.raise_for_status()
        with FileLock(root / "runtime" / "server.lock", timeout=25):
            pass
        print("BTranslateVideo stopped. Checkpoints are preserved.")
except (httpx.ConnectError, httpx.ConnectTimeout):
    try:
        with FileLock(root / "runtime" / "server.lock", timeout=25):
            pass
        print("BTranslateVideo is not running.")
    except Exception as error:
        print("BTranslateVideo is still shutting down: " + str(error), file=sys.stderr)
        sys.exit(1)
except Exception as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
