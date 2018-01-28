#!/usr/bin/env bash
set -e

echo "1. RUNNING EN CHEF >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
./sushichef.py -v --reset --token=".token" lang=en

echo "2. RUNNING ES CHEF >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
./sushichef.py -v --reset --token=".token" lang=es


