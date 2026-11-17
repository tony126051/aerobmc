"""Expose a provisioned BMC Runtime through an HTTPS gateway."""
import argparse
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import threading


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, backend_port, cert, key):
        self.backend_port = backend_port
        self.slots = threading.BoundedSemaphore(16)
        self.backend_keepalive_slots = threading.BoundedSemaphore(4)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls.load_cert_chain(cert, key)
        probe = HTTPConnection("127.0.0.1", backend_port, timeout=2)
        try:
            probe.request("GET", "/v1/status")
            if probe.getresponse().status != 401:
                raise RuntimeError("backend access control must be enabled before starting the gateway")
        finally:
            probe.close()
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(3)
            request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.tls.wrap_socket(request, server_side=True) as secure:
                self.finish_request(secure, address)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.remaining_requests = 256
        self.upstream = None

    def close_upstream(self):
        if self.upstream is not None:
            self.upstream.close()
            self.upstream = None
            self.server.backend_keepalive_slots.release()

    def finish(self):
        try:
            self.close_upstream()
        finally:
            super().finish()

    def log_message(self, format, *args):
        # Do not log paths or headers supplied by the client.
        pass

    def respond(self, status, payload, headers=()):
        self.remaining_requests -= 1
        keep_alive = (self.command == "GET" and status == 200
                      and self.request_version == "HTTP/1.1"
                      and self.headers.get_all("Connection") == ["keep-alive"]
                      and self.remaining_requests > 0)
        self.close_connection = not keep_alive
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "keep-alive" if keep_alive else "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def error(self, status, message, headers=()):
        self.respond(status, json.dumps({"error": message}).encode(),
                     (*headers, ("Content-Type", "application/json")))

    def forward(self):
        if not self.path.startswith(("/v1/", "/redfish/")) or "#" in self.path:
            return self.error(404, "not_found")
        if self.headers.get_all("Transfer-Encoding"):
            return self.error(400, "unsupported_transfer_encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and (not lengths[0].isascii() or not lengths[0].isdigit())):
            return self.error(400, "invalid_content_length")
        if lengths and len(lengths[0]) > 3:
            return self.error(413, "body_too_large")
        length = int(lengths[0]) if lengths else 0
        if length > 256:
            return self.error(413, "body_too_large")
        if self.command == "GET" and length:
            return self.error(400, "unexpected_body")
        credentials = self.headers.get_all("Authorization", [])
        if len(credentials) != 1 or not credentials[0].startswith("Bearer ") or not credentials[0][7:]:
            return self.error(401, "unauthorized", (("WWW-Authenticate", "Bearer"),))
        outgoing = {"Authorization": credentials[0], "Connection": "close", "X-BMC-Require-Auth": "1"}
        for name in ("Content-Type", "X-BMC-Action", "Idempotency-Key"):
            values = self.headers.get_all(name, [])
            if len(values) > 1:
                return self.error(400, "duplicate_header")
            if values:
                outgoing[name] = values[0]
        body = self.rfile.read(length)
        if len(body) != length:
            return self.error(400, "incomplete_body")

        reuse = (self.command == "GET" and self.request_version == "HTTP/1.1"
                 and self.headers.get_all("Connection") == ["keep-alive"]
                 and self.remaining_requests > 1)
        if not reuse:
            self.close_upstream()
        elif self.upstream is None and self.server.backend_keepalive_slots.acquire(blocking=False):
            self.upstream = HTTPConnection("127.0.0.1", self.server.backend_port, timeout=2)
        if self.upstream is not None:
            outgoing["Connection"] = "keep-alive"
        reused = self.upstream is not None and self.upstream.sock is not None
        try:
            status, payload, headers = self.exchange(outgoing, body)
        except (OSError, HTTPException, ValueError):
            self.close_upstream()
            if not reused:
                return self.error(502, "upstream_unavailable")
            # Only a bodyless GET can reach this retry after a stale connection.
            outgoing["Connection"] = "close"
            try:
                status, payload, headers = self.exchange(outgoing, body)
            except (OSError, HTTPException, ValueError):
                return self.error(502, "upstream_unavailable")
        self.respond(status, payload, headers)

    def exchange(self, outgoing, body):
        conn = self.upstream or HTTPConnection("127.0.0.1", self.server.backend_port, timeout=2)
        try:
            conn.request(self.command, self.path, body=body, headers=outgoing)
            reply = conn.getresponse()
            payload = reply.read(65537)
            if len(payload) > 65536 or reply.length not in (None, 0):
                raise ValueError("invalid upstream body length")
            headers = [(name, value) for name, value in reply.getheaders()
                       if name.lower() in {"content-type", "location", "odata-version",
                                           "www-authenticate", "allow", "retry-after"}]
            return reply.status, payload, headers
        finally:
            if conn is not self.upstream:
                conn.close()

    do_GET = forward
    do_POST = forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--backend-port", type=int, default=5555)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    with Gateway((args.listen, args.port), args.backend_port, args.cert, args.key) as gateway:
        gateway.serve_forever()


if __name__ == "__main__":
    main()
