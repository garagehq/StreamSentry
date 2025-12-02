#!/bin/bash
# Native build script for running directly on ARM device (e.g., Youyeetoo R1)
set -e

BUILD_TYPE=Release

# Native compilation - use system gcc
C_COMPILER=gcc
CXX_COMPILER=g++

TARGET_ARCH=aarch64
TARGET_PLATFORM=linux_${TARGET_ARCH}

ROOT_PWD="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${ROOT_PWD}/build/build_${TARGET_PLATFORM}_${BUILD_TYPE}"

mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

cmake ../.. \
    -DCMAKE_C_COMPILER=${C_COMPILER} \
    -DCMAKE_CXX_COMPILER=${CXX_COMPILER} \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE}

make -j4
make install
