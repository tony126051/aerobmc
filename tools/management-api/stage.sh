#!/bin/sh
set -eu
test "$#" -eq 1
root=$(cd "$(dirname "$0")/../.." && pwd)
arceos=$(cd "$1/arceos" && pwd)
app="$arceos/examples/bmc-management-api"
test ! -e "$app"
mkdir -p "$app/src"
cp "$root/tools/management-api/src/main.rs" "$app/src/"
cp -R "$root/crates/axbmc-core" "$app/"
cat > "$app/Cargo.toml" <<'TOML'
[package]
name = "bmc-management-api"
version = "0.1.0"
edition.workspace = true

[dependencies]
axstd.workspace = true
arceos_api.workspace = true
axbmc-core = { path = "axbmc-core" }
TOML
python3 - "$arceos/Cargo.toml" <<'PY'
from pathlib import Path
import sys
manifest = Path(sys.argv[1])
text = manifest.read_text()
anchor = "members = [\n"
assert text.count(anchor) == 1
# The pinned G1 fork uses a Rust 2024 module with one old FFI declaration.
paging = manifest.parent / "modules/axmm/src/vmsa_uma.rs"
source = paging.read_text()
declaration = '        extern "C" {\n            fn early_page_alloc()'
if declaration in source:
    paging.write_text(source.replace(declaration, declaration.replace("extern", "unsafe extern", 1), 1))
manifest.write_text(text.replace(anchor, anchor + '    "examples/bmc-management-api",\n', 1))
PY
