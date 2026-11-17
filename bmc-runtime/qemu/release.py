"""Build ordinary and fault-injection images and run release acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PLATFORM_COMMIT = "39220889fae5184885778feb222afa30be18c986"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", type=Path, help="local AeroTensor-G1 Git repository")
    parser.add_argument("kernel", type=Path)
    parser.add_argument("modules", type=Path)
    parser.add_argument("kernel_release")
    parser.add_argument("busybox", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--dbus-daemon", default="dbus-daemon")
    args = parser.parse_args()
    for path in (args.kernel, args.modules, args.busybox):
        if not path.is_file():
            parser.error(f"missing build input: {path}")
    args.output.mkdir()
    scripts = Path(__file__).resolve().parent
    inputs = {label: hashlib.sha256(path.read_bytes()).hexdigest() for label, path in (
        ("kernel", args.kernel), ("modules_apk", args.modules), ("busybox_apk", args.busybox),
    )}
    (args.output / "build-inputs.json").write_text(json.dumps(
        dict(platform_commit=PLATFORM_COMMIT, kernel_release=args.kernel_release,
             sha256=inputs, rust_toolchain="nightly-2025-05-20"), indent=2,
    ) + "\n")

    for mode in ("ordinary", "fault-injection"):
        print(f"Building {mode} images", flush=True)
        with tempfile.TemporaryDirectory(prefix="bmc-release-", dir="/tmp") as temporary:
            checkout = Path(temporary) / "platform"
            environment = dict(os.environ, BMC_BUILD_ONLY="1")
            environment.pop("BMC_TEST_CONTROL_STALL", None)
            if mode == "fault-injection":
                environment["BMC_TEST_CONTROL_STALL"] = "1"
            commands = [
                ["git", "clone", "--quiet", "--no-checkout", str(args.platform.resolve()), str(checkout)],
                ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", PLATFORM_COMMIT],
                ["sh", str(scripts / "run.sh"), str(checkout), str(args.kernel), str(args.modules),
                 args.kernel_release, str(args.busybox), str(args.output / mode)],
            ]
            with (args.output / f"{mode}-build.log").open("wb") as log:
                for command in commands:
                    result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT)
                    if result.returncode:
                        print(f"{mode} build failed; see {log.name}", file=sys.stderr)
                        return result.returncode

    return subprocess.call([
        sys.executable, str(scripts / "verify_release.py"), args.qemu, str(args.kernel),
        str(args.output / "fault-injection"), str(args.output / "acceptance"),
        "--release-images", str(args.output / "ordinary"), "--dbus-daemon", args.dbus_daemon,
    ])


if __name__ == "__main__":
    sys.exit(main())
