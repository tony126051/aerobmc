#!/bin/sh
set -eu
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 DISPOSABLE_ARCEOS_CHECKOUT EMPTY_LINUX_APP_DIR" >&2
    exit 2
fi
project=$(cd "$(dirname "$0")/../.." && pwd)
arceos=$(cd "$1" && pwd)/arceos
linux_app=$2
arceos_app=$arceos/examples/bmc-qemu-replay
test -f "$arceos/Cargo.toml"
test ! -e "$arceos_app"
test ! -e "$linux_app"
mkdir -p "$arceos_app" "$linux_app"
for app in "$arceos_app" "$linux_app"; do
    mkdir "$app/src"
    cp "$project/tools/qemu-replay/guest.rs" "$app/src/main.rs"
    cp "$project/tools/qemu-replay/control.rs" "$project/traces/sensor.csv" "$app/src/"
    cp -R "$project/crates/axbmc-core" "$app/"
done
cp "$project/tools/qemu-replay/arceos.Cargo.toml" "$arceos_app/Cargo.toml"
cp "$project/tools/qemu-replay/platform.rs" "$arceos_app/src/"
cp "$project/tools/qemu-replay/linux.Cargo.toml" "$linux_app/Cargo.toml"
python3 - "$arceos/Cargo.toml" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
anchor = "members = [\n"
if text.count(anchor) != 1:
    raise SystemExit("unexpected ArceOS workspace layout")
path.write_text(text.replace(anchor, anchor + '    "examples/bmc-qemu-replay",\n', 1))
PY
