"""Connect a private local Unix socket to an OpenBMC system bus over SSH."""
import argparse
import os
from pathlib import Path
import select
import socketserver
import subprocess

from appliance import stop_process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ssh_host", help="SSH host or configured alias with key authentication")
    parser.add_argument("socket", type=Path, help="new socket in a private directory")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--ssh-config", type=Path)
    args = parser.parse_args()
    ssh = ["ssh", "-o", "BatchMode=yes", "-p", str(args.port)]
    if args.ssh_config:
        ssh.extend(["-F", str(args.ssh_config)])

    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            with subprocess.Popen(
                [*ssh, "--", args.ssh_host,
                 "systemd-stdio-bridge --system"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
            ) as process:
                try:
                    while True:
                        ready, _, _ = select.select([self.request, process.stdout], [], [])
                        if self.request in ready:
                            data = self.request.recv(65536)
                            if not data:
                                return
                            remaining = memoryview(data)
                            while remaining:
                                remaining = remaining[os.write(process.stdin.fileno(), remaining):]
                        if process.stdout in ready:
                            data = os.read(process.stdout.fileno(), 65536)
                            if not data:
                                return
                            self.request.sendall(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stop_process(process)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    os.umask(0o077)
    with Server(str(args.socket), Relay) as server:
        try:
            server.serve_forever()
        finally:
            args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
