#!/usr/bin/env bash
# List audio devices so you can pick MIC_DEVICE (capture) and OUT_DEVICE (HDMI playback).
echo "================ PLAYBACK (TV / HDMI) -> use for OUT_DEVICE ================"
aplay -l 2>/dev/null | sed -n 's/^card \([0-9]\).*device \([0-9]\).*\[\(.*\)\].*/  plughw:\1,\2   (\3)/p'
echo
echo "================ CAPTURE (USB mic) -> use for MIC_DEVICE ==================="
arecord -l 2>/dev/null | sed -n 's/^card \([0-9]\).*device \([0-9]\).*\[\(.*\)\].*/  plughw:\1,\2   (\3)/p'
arecord -l 2>/dev/null | grep -q card || echo "  (no capture device found — plug in your USB mic)"
echo
echo "Example:  MIC_DEVICE=plughw:1,0 OUT_DEVICE=plughw:0,3 ./run.sh"
