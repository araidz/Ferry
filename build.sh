#!/bin/sh
# Build the single-file `ferry` executable with stdlib zipapp.
# Nests the package under an absolute-import launcher so the package's relative
# imports resolve inside the archive (a bare archive-root __main__ can't).
set -e
cd "$(dirname "$0")"
rm -rf build dist
mkdir -p build dist
cp -r ferry build/ferry
rm -rf build/ferry/__pycache__
printf 'import sys\nfrom ferry.__main__ import main\nsys.exit(main())\n' > build/__main__.py
python3 -m zipapp build -o dist/ferry -p "/usr/bin/env python3"
chmod +x dist/ferry
rm -rf build
echo "built dist/ferry  —  install: ln -sf \"$(pwd)/dist/ferry\" /usr/local/bin/ferry"
