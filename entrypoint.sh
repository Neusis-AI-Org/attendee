#!/usr/bin/env bash
set -euo pipefail
# Debug mode disabled by default (set PA_DEBUG=1 to enable)
[[ "${PA_DEBUG:-0}" = "1" ]] && set -x

die(){ echo "FATAL: $*" >&2; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }

for b in pulseaudio pactl; do have "$b" || die "Missing $b"; done

# ---- Safe XDG_RUNTIME_DIR selection ----
UID_CUR="$(id -u)"
CANDIDATE="${XDG_RUNTIME_DIR:-}"

usable_dir() {
  local d="$1"
  [[ -n "$d" ]] && [[ -d "$d" ]] && [[ -w "$d" ]] && [[ "$(stat -c %u "$d" 2>/dev/null || echo -1)" -eq "$UID_CUR" ]]
}

if usable_dir "$CANDIDATE"; then
  export XDG_RUNTIME_DIR="$CANDIDATE"
else
  # Prefer /run/user/$UID if available, else fall back to /tmp
  if usable_dir "/run/user/$UID_CUR"; then
    export XDG_RUNTIME_DIR="/run/user/$UID_CUR"
  else
    export XDG_RUNTIME_DIR="/tmp/xdg-${UID_CUR}"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
  fi
fi

# Pulse runtime lives under XDG_RUNTIME_DIR
export PULSE_RUNTIME_PATH="$XDG_RUNTIME_DIR/pulse"
mkdir -p "$PULSE_RUNTIME_PATH"
chmod 700 "$XDG_RUNTIME_DIR" || true


# Make ALSA 'default' point at Pulse
HOME_DIR="${HOME:-/home/$(id -un)}"
mkdir -p "$HOME_DIR"
cat > "$HOME_DIR/.asoundrc" <<'EOF'
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

# ---- PulseAudio daemon.conf overrides ----
# Chrome/Teams render audio at 48 kHz. PulseAudio's default is 44.1 kHz with
# the speex-float-1 resampler (the lowest-quality option on its 1-10 scale).
# Without these overrides every audio frame gets downsampled 48 → 44.1 with
# a cheap resampler, then ffmpeg captures at 44.1 and the MP3 transcode
# upsamples back to 48 — two unnecessary resampling passes that shave off
# the upper end of the speech band and contribute to Whisper transcription
# errors. Per-user daemon.conf is read on daemon start, so write it BEFORE
# the pulseaudio invocation below.
#
# Setting default and alternate to the same rate means the sink never
# auto-switches rate at stream connect time, so module-suspend-on-idle
# can't bring it back at a different rate after a quiet stretch either.
PULSE_CFG_DIR="${XDG_CONFIG_HOME:-$HOME_DIR/.config}/pulse"
mkdir -p "$PULSE_CFG_DIR"
cat > "$PULSE_CFG_DIR/daemon.conf" <<'EOF'
default-sample-rate = 48000
alternate-sample-rate = 48000
default-sample-format = s16le
default-sample-channels = 2
resample-method = speex-float-5
EOF

if [[ "${PA_DEBUG:-0}" = "1" ]]; then
  echo "==== ENV ===="
  echo "USER=$(id -un) UID=$(id -u) GID=$(id -g)"
  echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
  echo "PULSE_RUNTIME_PATH=$PULSE_RUNTIME_PATH"
  echo "PULSE_SERVER=${PULSE_SERVER:-<unset>}"
  echo "=============="
fi

# Start our own server unless PULSE_SERVER is preset (shared server case)
if [[ -z "${PULSE_SERVER:-}" ]]; then
  rm -f "${PULSE_RUNTIME_PATH}/pid" 2>/dev/null || true
  echo "Starting PulseAudio (per-user)…"
  pulseaudio --daemonize=yes \
             --exit-idle-time="${PA_IDLE_TIME:--1}" \
             --realtime=no --high-priority=no \
             --log-level="${PA_LOG_LEVEL:-info}" --log-target=stderr \
             --disallow-exit || die "pulseaudio failed to start"
  export PULSE_SERVER="unix:${PULSE_RUNTIME_PATH}/native"
else
  echo "Using external Pulse server at $PULSE_SERVER"
fi

# Wait for server
for i in {1..50}; do pactl info >/dev/null 2>&1 && break; sleep 0.1; done
pactl info >/dev/null || die "pactl cannot reach PulseAudio"

if [[ "${PA_DEBUG:-0}" = "1" ]]; then
  echo "==== PACTL INFO ===="
  pactl info || true
  echo "==== SINKS (short) ===="
  pactl list short sinks || true
  echo "==== SOURCES (short) ===="
  pactl list short sources || true
fi

# ---- Pre-load a named 48 kHz null sink ----
# Replace the auto_null spawned by module-always-sink with an explicit
# null sink that has a deterministic name, an explicit format that won't
# get re-negotiated on resume, and a human-readable device description.
# Chrome inspects the device description when choosing audio constraints;
# a generic "Dummy Output" is the kind of device clients are most likely
# to downgrade audio quality for. Idempotent — only loaded if not already
# present.
DEFAULT_SINK="$(pactl info | sed -n 's/^Default Sink: //p')"
DEFAULT_SOURCE="$(pactl info | sed -n 's/^Default Source: //p')"

if ! pactl list short sinks | awk '{print $2}' | grep -qx "bot_sink"; then
  pactl load-module module-null-sink \
    sink_name=bot_sink \
    rate=48000 channels=2 format=s16le \
    sink_properties="device.description=Bot_Capture_Sink" >/dev/null || true
fi

# Prefer our named sink; if for any reason it didn't load, fall back to
# auto_null (the previous behaviour).
if pactl list short sinks | awk '{print $2}' | grep -qx "bot_sink"; then
  pactl set-default-sink bot_sink || true
  pactl set-default-source bot_sink.monitor || true
elif pactl list short sinks | awk '{print $2}' | grep -qx "auto_null"; then
  pactl set-default-sink auto_null || true
  pactl set-default-source auto_null.monitor || true
fi

if [[ "${PA_DEBUG:-0}" = "1" ]]; then
  echo "==== FINAL ===="
  echo "Default Sink:   $(pactl info | sed -n 's/^Default Sink: //p')"
  echo "Default Source: $(pactl info | sed -n 's/^Default Source: //p')"
  pactl list short sinks || true
  pactl list short sources || true
  echo "================"
fi

echo "[entrypoint] PulseAudio ready. Exec: $*"
exec "$@"
