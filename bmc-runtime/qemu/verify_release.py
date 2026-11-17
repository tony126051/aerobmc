"""Run the dual-platform BMC acceptance suite and retain its evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from diagnostics import compare, export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qemu")
    parser.add_argument("kernel", type=Path)
    parser.add_argument("images", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--release-images", type=Path, required=True,
                        help="ordinary build without control-stall injection")
    parser.add_argument("--dbus-daemon", default="dbus-daemon")
    args = parser.parse_args()
    daemon = shutil.which(args.dbus_daemon)
    if daemon is None:
        parser.error("dbus-daemon is required for OpenBMC acceptance")
    try:
        from importlib.metadata import version
        dbus_next_version = version("dbus-next")
    except ImportError:
        parser.error("install dbus-next in the Python environment running this verifier")
    args.output.mkdir()
    script = Path(__file__).resolve().parent
    manifest = {
        "platform_commit": (args.images / "platform-commit.txt").read_text().strip(),
        "sha256": {label: hashlib.sha256(path.read_bytes()).hexdigest() for label, path in (
            ("linux_kernel", args.kernel), ("linux_initramfs", args.images / "linux.cpio"),
            ("arceos_image", args.images / "arceos.bin"),
        )},
        "qemu": subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0],
        "release_sha256": {
            name: hashlib.sha256((args.release_images / name).read_bytes()).hexdigest()
            for name in ("linux.cpio", "arceos.bin")
        },
        "release_platform_commit": (args.release_images / "platform-commit.txt").read_text().strip(),
        "dbus_daemon": subprocess.check_output([daemon, "--version"], text=True).splitlines()[0],
        "dbus_next": dbus_next_version,
    }
    if manifest["platform_commit"] != manifest["release_platform_commit"]:
        parser.error("release and fault-injection images must use the same platform commit")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    stages = {}

    def report():
        (args.output / "acceptance.json").write_text(json.dumps(stages, indent=2) + "\n")

    for stage, verifier in (("runtime", "verify.py"), ("https", "verify_gateway.py"),
                            ("appliance", "verify_appliance.py"),
                            ("release_guard", "verify_gateway.py"), ("openbmc", "verify_openbmc.py")):
        destination = args.output / stage
        command = [sys.executable, str(script / verifier), args.qemu, str(args.kernel)]
        if stage == "runtime":
            # The functional verifier reads its images from the result directory.
            destination.mkdir()
            for name in ("linux.cpio", "arceos.bin"):
                (destination / name).symlink_to((args.images / name).resolve())
        else:
            command.append(str(args.release_images if stage in ("release_guard", "openbmc") else args.images))
        command.append(str(destination))
        if stage == "release_guard":
            command.append("--check-release")
        if stage in ("openbmc", "appliance"):
            command.extend(["--dbus-daemon", daemon])
        print(f"Running {stage} acceptance", flush=True)
        with (args.output / f"{stage}.log").open("wb") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        stages[stage] = {"passed": result.returncode == 0, "exit_code": result.returncode}
        report()
        if result.returncode:
            return 1

    archives = []
    try:
        for guest in ("linux", "arceos"):
            archive = args.output / f"{guest}.zip"
            export(args.output / "appliance" / guest, archive)
            archives.append(archive)
        comparison = compare(*archives)
        (args.output / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
        stages["diagnostics"] = {"passed": comparison["matches"],
                                 "compared_events": comparison["compared_events"]}
    except Exception as error:
        stages["diagnostics"] = {"passed": False, "error": str(error)}
    report()
    return 0 if stages["diagnostics"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
