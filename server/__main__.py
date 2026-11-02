"""`python -m server [--port 8765] [--no-browser]` from the pcbstudio folder."""
import argparse
import threading
import webbrowser

import uvicorn

p = argparse.ArgumentParser(prog="pcbstudio")
p.add_argument("--port", type=int, default=8765)
p.add_argument("--no-browser", action="store_true")
a = p.parse_args()

url = f"http://127.0.0.1:{a.port}/"
if not a.no_browser:
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
print(f"PCB Studio on {url}  (Ctrl+C to stop)")
# One worker process: the pcbnew thread and open boards live in this process.
uvicorn.run("server.app:app", host="127.0.0.1", port=a.port, workers=1, log_level="warning")
