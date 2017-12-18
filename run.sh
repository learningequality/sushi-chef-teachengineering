#!/usr/bin/env bash
set -e

ARCHIVE_NAME="Teach Engineering"

echo ""
echo "1. RUNNING SOUCHEF >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
./souschef.py

if [ ! -f  "${ARCHIVE_NAME}".zip ]; then
  echo "Cannot find archive "${ARCHIVE_NAME}".zip in current directpry"
  echo "Please check variable ARCHIVE_NAME is properly set in ./run.sh script"
fi



echo ""
echo "2. UNZIPPING ARCHIVE in content/ dir  >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
if [ ! -d  content ]; then
  mkdir content
fi
rm -rf content/*
mv "$ARCHIVE_NAME".zip content/
cd content
unzip -oq "$ARCHIVE_NAME".zip
cd ..




echo ""
echo "3. RUNNING LINECOOK CHEF >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
./sushichef.py -v --reset --channeldir="./content/${ARCHIVE_NAME}" --token=".token"
