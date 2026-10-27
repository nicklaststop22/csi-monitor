#!/usr/bin/env python3
"""
Capture Channel State Information (CSI) from the ESP32-CSI-Tool AP over serial,
parse each frame into per-subcarrier amplitudes, and save to JSON for visualization.

The AP (SparkFun ESP32 Thing Plus running active_ap) prints one CSI_DATA line per
received frame. The final bracketed array is interleaved [imag, real, imag, real, ...]
for each OFDM subcarrier. Amplitude = sqrt(real^2 + imag^2).

Usage:  python capture_csi.py COM27 20 out.json         (capture 20s)
        python capture_csi.py COM27 20 out.json --mark 10  (print a MOVE marker at 10s)
"""
import sys, json, time, math
import serial  # pyserial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM27"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
OUTFILE = sys.argv[3] if len(sys.argv) > 3 else "csi_capture.json"
BAUD = 921600

# The CSV columns the tool emits before the CSI array (index of each field).
# ...,noise_floor,...,local_timestamp,...,CSI_DATA[array]
# We parse by splitting on the first '[' to isolate the array.

def parse_line(line):
    if not line.startswith("CSI_DATA"):
        return None
    try:
        head, arr = line.split("[", 1)
        arr = arr.split("]", 1)[0]
        fields = head.split(",")
        mac = fields[2]
        rssi = int(fields[3])
        # local_timestamp is field 18 (0-based) in this tool's format
        ts = int(fields[18])
        vals = [int(x) for x in arr.split()]
        # interleaved imag,real pairs -> amplitude per subcarrier
        amps = []
        for i in range(0, len(vals) - 1, 2):
            im = vals[i]
            re = vals[i + 1]
            amps.append(math.sqrt(re * re + im * im))
        return {"mac": mac, "rssi": rssi, "ts": ts, "amps": amps}
    except (ValueError, IndexError):
        return None

def main():
    mark_at = None
    if "--mark" in sys.argv:
        mark_at = float(sys.argv[sys.argv.index("--mark") + 1])

    ser = serial.Serial(PORT, BAUD, timeout=1)
    ser.dtr = False   # don't reset the AP on open
    ser.rts = False
    ser.reset_input_buffer()

    print(f"Capturing {DURATION}s of CSI from {PORT} ...")
    frames = []
    start = time.time()
    marked = False
    while time.time() - start < DURATION:
        if mark_at and not marked and (time.time() - start) >= mark_at:
            print(f">>> {mark_at}s: START MOVING NOW <<<")
            marked = True
        try:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        rec = parse_line(raw)
        if rec:
            rec["t"] = round(time.time() - start, 3)
            frames.append(rec)
    ser.close()

    # summarize per MAC
    macs = {}
    for f in frames:
        macs[f["mac"]] = macs.get(f["mac"], 0) + 1

    out = {
        "port": PORT,
        "duration": DURATION,
        "mark_at": mark_at,
        "n_frames": len(frames),
        "macs": macs,
        "frames": frames,
    }
    with open(OUTFILE, "w") as fh:
        json.dump(out, fh)
    print(f"Captured {len(frames)} CSI frames -> {OUTFILE}")
    print("Per-MAC counts:", macs)
    if frames:
        print("Subcarriers per frame:", len(frames[0]["amps"]))

if __name__ == "__main__":
    main()
