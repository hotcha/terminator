#!/bin/sh
# Update translation files
cd "$(dirname "$0")" || exit 1
for po_file in `ls *.po`; do
  msgmerge -N -U ${po_file} terminator.pot
done
