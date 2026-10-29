import argparse
import threading
import webbrowser

import uvicorn
from filelock import FileLock, Timeout

from app.config import ROOT
from app.server import create_app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTranslateVideo 本地中文译制工作台")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    (ROOT / "runtime").mkdir(exist_ok=True)
    try:
        with FileLock(ROOT / "runtime" / "server.lock", timeout=0):
            if not args.no_browser:
                threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
            application = create_app()
            server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=args.port, log_level="info", access_log=False, timeout_graceful_shutdown=8))
            application.state.request_shutdown = lambda: setattr(server, "should_exit", True)
            server.run()
    except Timeout:
        print("BTranslateVideo is already running. Open http://127.0.0.1:7861")
        if not args.no_browser:
            webbrowser.open("http://127.0.0.1:7861")
