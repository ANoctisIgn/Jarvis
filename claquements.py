"""Détection du double claquement de mains.

Réglages :
  SPIKE_RATIO   combien de fois plus fort que le bruit ambiant compte comme un claquement
                (monter si faux déclenchements, baisser si les claquements sont ratés)
  MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S   écart autorisé entre les deux claquements
  MIN_RMS       niveau minimum absolu d'un claquement
  SAMPLE_RATE   essayer 48000 si le micro n'aime pas 44100
Micro : JARVIS_INPUT_DEVICE dans .env (numéro ou morceau du nom), sinon micro par défaut
(et, s'il est muet, le micro le plus sonore).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger("clap_listen")

SAMPLE_RATE = 44100
BLOCK_MS = 30
CHANNELS = 1

SPIKE_RATIO = 9.0
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.30
RETRIGGER_RATIO = 0.30
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.012
QUIET_GATE_MULT = 2.2  # update noise floor only when below floor * this
# Startup mic probe: if default input RMS stays below this, scan for a louder device.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device(blocksize: int) -> int:
    log.info("Audio devices:\n%s", sd.query_devices())

    override = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set JARVIS_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using JARVIS_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). "
                "Check Windows input level or try another JARVIS_INPUT_DEVICE.",
                peak,
            )
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info(
                "Using default microphone [%d]: %s (probe rms=%.5f)",
                default,
                default_name,
                peak,
            )
            return default
        log.warning(
            "Default mic [%d] %s is silent or unavailable (probe rms=%s); "
            "scanning other inputs...",
            default,
            default_name,
            f"{peak:.5f}" if peak is not None else "unopenable",
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


trouver_micro = _resolve_input_device_index


def ecouter_claquements(sur_double_clap: Callable[[], None], autorise: Callable[[], bool]) -> int:
    """Écoute le micro en continu. Au premier double claquement autorisé, lance `sur_double_clap`
    (une seule fois par lancement de Jarvis). Ne rend la main qu'en cas d'erreur audio."""
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True
    deja_fait = False

    log.info("Écoute des claquements activée. Ctrl+C pour arrêter.")
    input_idx = _choose_input_device(blocksize)
    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                data, _ = stream.read(blocksize)
                level = rms_mono(data)

                if level < noise_floor * QUIET_GATE_MULT:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (1.0 - NOISE_FLOOR_ALPHA) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()
                if level < threshold * RETRIGGER_RATIO:
                    spike_armed = True

                if spike_armed and level >= threshold and (now - last_logged_double) >= COOLDOWN_S:
                    spike_armed = False
                    if first_clap_time is None:
                        first_clap_time = now
                        continue
                    gap = now - first_clap_time
                    if MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S:
                        first_clap_time = None
                        last_logged_double = now
                        if not deja_fait and autorise():
                            deja_fait = True
                            log.info("Double claquement ! Lancement de la routine.")
                            threading.Thread(target=sur_double_clap, name="double_clap", daemon=True).start()
                    elif gap > MAX_DOUBLE_GAP_S:
                        first_clap_time = now
    except sd.PortAudioError as e:
        log.error("Erreur audio : %s", e)
        return 1
