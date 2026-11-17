"""Operate a simulated BMC over authenticated HTTPS using Redfish discovery."""
import argparse
import json
from pathlib import Path
import secrets
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, origin, token, ca):
        url = urlsplit(origin)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("expected an HTTPS origin")
        self.origin = origin.rstrip("/")
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=ca)))
        self.token = token

    def request(self, path, body=None, key=None):
        if (not path.startswith(("/redfish/", "/v1/actions/")) and path != "/v1/status") or any(char in path for char in "\\#\r\n"):
            raise ValueError("invalid Redfish resource path")
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            headers.update({"Content-Type": "application/json", "X-BMC-Action": "1", "Idempotency-Key": key})
        request = Request(self.origin + path, headers=headers,
                          data=(body if isinstance(body, bytes) else json.dumps(body).encode())
                          if body is not None else None)
        try:
            response = self.opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read(65537)
            if len(payload) > 65536:
                raise ValueError("response exceeds client limit")
            return response.status, response.headers, json.loads(payload) if payload else None

    def get(self, path):
        code, _, data = self.request(path)
        if code != 200:
            raise ValueError(f"GET {path} returned {code}")
        return data

    def system(self):
        root = self.get("/redfish/v1/")
        systems = self.get(root["Systems"]["@odata.id"])
        members = systems["Members"]
        if len(members) != 1:
            raise ValueError("expected exactly one managed system")
        return self.get(members[0]["@odata.id"])

    def diagnosis(self, system):
        services = self.get(system["LogServices"]["@odata.id"])
        log = self.get(services["Members"][0]["@odata.id"])
        entries = self.get(log["Entries"]["@odata.id"])
        return [self.get(item["@odata.id"]) for item in entries["Members"][-5:]]


def operate(client, action, timeout, key):
    system = client.system()
    reset = system["Actions"]["#ComputerSystem.Reset"]
    if action not in reset["ResetType@Redfish.AllowableValues"]:
        raise ValueError("reset type unavailable")
    code, headers, task = client.request(reset["target"], {"ResetType": action}, key)
    if code != 202:
        return {"ok": False, "http_status": code, "response": task, "idempotency_key": key}
    monitor = headers["Location"]
    deadline = time.monotonic() + timeout
    expected = "Off" if action == "ForceOff" else "On"
    accepted = False
    while time.monotonic() < deadline:
        if not accepted:
            code, _, _ = client.request(monitor)
            if code not in (202, 204):
                break
            accepted = code == 204
            if accepted and action == "PowerCycle":
                completed = client.get(task["@odata.id"])
                if completed.get("Oem", {}).get("BmcG1", {}).get("Event") != "power_cycle_scheduled":
                    break
        if accepted:
            system = client.get(system["@odata.id"])
            fault = system.get("Oem", {}).get("BmcG1", {}).get("PowerFault")
            if fault:
                break
            if system["PowerState"] == expected:
                return {"ok": True, "power": expected, "task": task["@odata.id"], "idempotency_key": key}
        time.sleep(0.05)
    system = client.get(system["@odata.id"])
    return {"ok": False, "power": system["PowerState"], "task": client.get(task["@odata.id"]),
            "idempotency_key": key, "events": client.diagnosis(system)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--idempotency-key")
    parser.add_argument("action", choices=("status", "On", "ForceOff", "PowerCycle"))
    args = parser.parse_args()
    if not 0 < args.timeout < float("inf"):
        parser.error("timeout must be finite and positive")
    try:
        client = Client(args.origin, args.token_file.read_text().strip(), args.ca_file)
        if args.action == "status":
            report = {"ok": True, "system": client.system()}
        else:
            key = args.idempotency_key or secrets.token_hex(16)
            print(f"Idempotency-Key: {key}", file=sys.stderr)
            report = operate(client, args.action, args.timeout, key)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 2
    except (ValueError, OSError, URLError, KeyError) as error:
        print(f"manage: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
