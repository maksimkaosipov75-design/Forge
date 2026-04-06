import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


log = logging.getLogger(__name__)


class StatusHttpServer:
    def __init__(
        self,
        host: str,
        port: int,
        health_provider: Callable[[], str],
        metrics_provider: Callable[[], str],
    ):
        self.host = host
        self.port = port
        self.health_provider = health_provider
        self.metrics_provider = metrics_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        if self._server is not None:
            return

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    body = outer.health_provider().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if self.path == "/metrics":
                    body = outer.metrics_provider().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args):
                log.debug("status-http: " + format, *args)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("Status HTTP server started on http://%s:%s", self.host, self.port)

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
