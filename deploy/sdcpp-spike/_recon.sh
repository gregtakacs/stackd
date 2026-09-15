#!/usr/bin/env bash
# Host-env recon for running the live sd.cpp probe: does the daemon container
# have httpx, and can we exec into it? Results written to stdout.
set +e
echo "== python on host =="
python3 --version 2>&1
echo "== ensurepip available on host? =="
python3 -m ensurepip --version 2>&1 | head -1
echo "== httpx importable on host? =="
python3 -c 'import httpx, sys; sys.stderr.write("HOST_HTTPX " + httpx.__version__ + "\n")' 2>&1 | head -1
echo "== daemon container python + httpx =="
docker exec stackd python3 --version 2>&1 | head -1
docker exec stackd python3 -c 'import httpx, sys; sys.stderr.write("CONT_HTTPX " + httpx.__version__ + "\n")' 2>&1 | head -1
echo "== done =="
