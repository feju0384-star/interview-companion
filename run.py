"""The background service. Use launcher.py for normal startup."""
import logging
import os

import uvicorn

from backend.app import app


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("INTERVIEW_PORT", "8765")),
                            access_log=False, log_level="warning", ws_max_size=4096, ws_max_queue=8,
                            timeout_graceful_shutdown=8)
    server = uvicorn.Server(config)
    app.state.shutdown_hook = lambda: setattr(server, "should_exit", True)
    server.run()
