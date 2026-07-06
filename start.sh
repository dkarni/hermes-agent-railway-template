#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions
mkdir -p /data/.hermes/skills
mkdir -p /data/.hermes/workspace
mkdir -p /data/.hermes/pairing

python /app/bootstrap_marco.py
python /app/bootstrap_max.py
python /app/bootstrap_poly.py

exec python /app/server.py
