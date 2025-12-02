#!/bin/bash
# Native build script for running directly on ARM device (e.g., Youyeetoo R1)
set -e

rm -rf build
mkdir build && cd build

cmake .. \
    -DCMAKE_CXX_COMPILER=g++ \
    -DCMAKE_C_COMPILER=gcc \
    -DCMAKE_BUILD_TYPE=Release

make -j4
make install
