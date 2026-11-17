#!/bin/sh
set -eu
test "$#" -eq 2
project=$(cd "$(dirname "$0")/../.." && pwd)
arceos=$(cd "$1" && pwd)/arceos
linux_app=$2
app="$arceos/examples/bmc-gpio-irq"
test ! -e "$app"
test ! -e "$linux_app"
mkdir -p "$app/src" "$linux_app/src"
cp "$project/tools/gpio-irq/guest.rs" "$app/src/main.rs"
cp "$project/tools/gpio-irq/guest.rs" "$linux_app/src/main.rs"
cp -R "$project/crates/axbmc-core" "$app/"
cp -R "$project/crates/axbmc-core" "$linux_app/"
cat > "$app/Cargo.toml" <<'TOML'
[package]
name = "bmc-gpio-irq"
version = "0.1.0"
edition.workspace = true

[features]
timeout = []
fan = []


[dependencies]
axstd.workspace = true
arceos_api.workspace = true
axhal.workspace = true
axbmc-core = { path = "axbmc-core" }
TOML
cat > "$linux_app/Cargo.toml" <<'TOML'
[package]
name = "bmc-gpio-irq"
version = "0.1.0"
edition = "2024"

[workspace]

[features]
timeout = []
fan = []


[dependencies]
axbmc-core = { path = "axbmc-core" }

[profile.release]
lto = true
codegen-units = 1
TOML
python3 - "$arceos/Cargo.toml" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
text = p.read_text()
assert text.count("members = [\n") == 1
p.write_text(text.replace("members = [\n", 'members = [\n    "examples/bmc-gpio-irq",\n', 1))
PY
