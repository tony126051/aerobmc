"""Package an accepted BMC build and verify it before extracting."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

STAGES = {"runtime", "https", "appliance", "release_guard", "openbmc", "diagnostics"}
SCRIPTS = (
    "appliance.py", "gateway.py", "monitor.py", "manage.py", "openbmc_sensors.py",
    "diagnostics.py", "verify.py",
)
MEMBERS = {
    "images/linux.cpio", "images/arceos.bin", "images/platform-commit.txt",
    "vmlinuz-virt", "build-inputs.json", "acceptance.json", "acceptance-manifest.json",
    "comparison.json", "README.md", "requirements.txt",
    *(f"qemu/{name}" for name in SCRIPTS),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_evidence(files):
    acceptance = json.loads(files["acceptance.json"])
    if set(acceptance) != STAGES or any(stage["passed"] is not True for stage in acceptance.values()):
        raise ValueError("all six release stages must pass")
    manifest = json.loads(files["acceptance-manifest.json"])
    inputs = json.loads(files["build-inputs.json"])
    commit = files["images/platform-commit.txt"].decode().strip()
    if commit != manifest["release_platform_commit"] or commit != inputs["platform_commit"]:
        raise ValueError("platform commit does not match acceptance")
    for name in ("linux.cpio", "arceos.bin"):
        if digest(files[f"images/{name}"]) != manifest["release_sha256"][name]:
            raise ValueError(f"{name} does not match acceptance")
    kernel = digest(files["vmlinuz-virt"])
    if kernel != inputs["sha256"]["kernel"] or kernel != manifest["sha256"]["linux_kernel"]:
        raise ValueError("Linux kernel does not match acceptance")
    if json.loads(files["comparison.json"])["matches"] is not True:
        raise ValueError("dual-platform diagnostic comparison failed")


def package(build, kernel, output):
    scripts = Path(__file__).resolve().parent
    sources = {
        "vmlinuz-virt": kernel,
        "build-inputs.json": build / "build-inputs.json",
        "acceptance.json": build / "acceptance/acceptance.json",
        "acceptance-manifest.json": build / "acceptance/manifest.json",
        "comparison.json": build / "acceptance/comparison.json",
        "README.md": scripts.parent / "README.md",
    }
    sources.update({f"images/{name}": build / "ordinary" / name
                    for name in ("linux.cpio", "arceos.bin", "platform-commit.txt")})
    sources.update({f"qemu/{name}": scripts / name for name in SCRIPTS})
    files = {name: path.read_bytes() for name, path in sources.items()}
    files["requirements.txt"] = b"dbus-next==0.2.3\n"
    check_evidence(files)
    manifest = {"format": 1, "sha256": {name: digest(data) for name, data in sorted(files.items())}}
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
        archive.writestr("package-manifest.json", json.dumps(manifest, indent=2) + "\n")


def unpack(archive_path, destination):
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != MEMBERS | {"package-manifest.json"}:
            raise ValueError("unexpected package contents")
        manifest = json.loads(archive.read("package-manifest.json"))
        if manifest["format"] != 1 or set(manifest["sha256"]) != MEMBERS:
            raise ValueError("invalid package manifest")
        files = {name: archive.read(name) for name in MEMBERS}
    for name, data in files.items():
        if digest(data) != manifest["sha256"][name]:
            raise ValueError(f"checksum mismatch: {name}")
    check_evidence(files)
    destination.mkdir()
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (destination / "package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    pack = commands.add_parser("pack")
    for arg in ("build", "kernel", "archive"):
        pack.add_argument(arg, type=Path)
    extract = commands.add_parser("unpack")
    extract.add_argument("archive", type=Path)
    extract.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "pack":
            package(args.build, args.kernel, args.archive)
        else:
            unpack(args.archive, args.destination)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        parser.exit(1, f"release package: {error}\n")


if __name__ == "__main__":
    main()
