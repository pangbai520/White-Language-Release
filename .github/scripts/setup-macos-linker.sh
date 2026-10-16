#!/usr/bin/env bash
set -euo pipefail

llvm=${1:?expected an LLVM toolchain directory}
if [ ! -x "$llvm/bin/clang" ]; then
  echo "::error::The macOS package has no runnable Clang: $llvm/bin/clang" >&2
  exit 1
fi
if [ ! -x "$llvm/bin/ld64.lld" ]; then
  if [ ! -x "$llvm/bin/lld" ]; then
    echo "::error::This LLVM package has neither ld64.lld nor its matching lld driver. A complete macOS LLVM toolchain is required." >&2
    exit 1
  fi
  # lld selects the Mach-O driver from argv[0]. Keep this alias in the package
  # so copying the toolchain into another bootstrap generation still works.
  ln -s lld "$llvm/bin/ld64.lld"
fi

touch "$llvm/bin/clang.cfg"
while IFS= read -r -d '' config; do
  if grep -E '^[[:space:]]*(-fuse-ld=|--ld-path=)' "$config" |
      grep -Evq '^[[:space:]]*-fuse-ld=lld[[:space:]]*$'; then
    echo "::error::Conflicting linker selection in $config" >&2
    exit 1
  fi
  if grep -Eq '^[[:space:]]*-fuse-ld=lld[[:space:]]*$' "$config"; then
    continue
  fi
  # Clang resolves ld64.lld beside itself, no runner-specific path is embedded
  # in the published package. Apple SDK discovery stays with the driver.
  printf '\n-fuse-ld=lld\n' >> "$config"
done < <(find "$llvm/bin" -maxdepth 1 -type f \( -name clang.cfg -o -name '*darwin*.cfg' \) -print0)

"$llvm/bin/ld64.lld" --version
