#!/usr/bin/env bash
set -euo pipefail

if [ ! -d .git ]; then
  git init
  git config user.email "sample@example.com"
  git config user.name "Auto Loop Sample"
  git add .
  git commit -m "Initial kanban sample"
fi
