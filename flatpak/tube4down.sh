#!/bin/sh
# Ensure the persistent, writable target dirs exist before Python starts,
# since /app/bin/cache and /app/bin/ffmpeg are symlinks into this location.
mkdir -p /var/data/Tube4Down/cache /var/data/Tube4Down/ffmpeg
exec python3 /app/bin/Tube4Down.pyw "$@"
