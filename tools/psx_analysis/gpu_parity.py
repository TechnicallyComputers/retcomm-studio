#!/usr/bin/env python3
"""gpu_parity.py -- frame-locked image parity between psx-runtime and DuckStation.

    python3 gpu_parity.py --frame 41230 --out analysis/frames/parity-41230

Drives both emulators to the SAME guest frame, screenshots both, and reports how
far apart the presented images are. That answers the first question any render
bug has to answer:

  images match      -> the game code executed the same on both; a divergence
                       you can see on screen is in the renderer
  images differ     -> the guest ran differently; the GP0 stream is the place
                       to look, and tools/cosim.py will bracket where

Scope, stated honestly: this is IMAGE parity, not GP0-stream parity. The
DuckStation oracle patch (tools/duckstation/psxrecomp_oracle.patch) implements
screenshot / read_vram / gpu_state / run_to_frame / step / pause, but not
gpu_frame_dump, so there is no packet stream to compare on that side. Adding one
to the patch is the obvious next step if image parity keeps saying "the streams
must differ" without saying where.

Only DuckStation gets driven. psx-runtime removed pause / step / run_to_frame
(runtime/src/debug_server.c) -- it is designed to be read from, not steered --
so the native side is sampled where it already is, and DuckStation is advanced
to meet it. If the native run has already passed the frame DuckStation is on,
there is nothing to do but restart the DuckStation side; that is reported rather
than papered over.

Getting the oracle: `python3 duckstation_oracle.py all` builds and installs a
patched DuckStation into the RetComM data root (once, ~10 minutes), and
`duckstation_oracle.py start --disc <cue>` runs it headless on 4371. Pass
--start-oracle here to have this tool do that for you when nothing is listening.

Both instances must already be running with their debug servers up, on the same
disc, from the same starting state -- typically both loaded from the same
savestate, or both run from boot with the same input script. Parity between two
runs that were not driven identically means nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psx_gpu_frame import wait_for_class  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from psx_gpu_frame import (  # noqa: E402
    DEFAULT_DUCKSTATION_PORT, DEFAULT_NATIVE_PORT, DebugConn, DebugError,
)

GPU_STATE_FIELDS = ("display_area_x", "display_area_y", "disp_w", "disp_h",
                    "da_x", "da_y", "display_enable")


def ensure_oracle(args) -> int:
    """Start the installed DuckStation oracle if nothing is answering yet.

    Opt-in (--start-oracle), because launching an emulator as a side effect of
    running a diff tool is a surprise nobody asked for. When it is not
    requested, a missing oracle just reports how to get one.
    """
    import subprocess as sp
    probe = DebugConn(args.host, args.ds_port, timeout=2.0)
    try:
        probe.cmd("ping")
        return 0
    except DebugError:
        pass
    if not args.disc:
        print("error: --start-oracle needs --disc (the same image psx-runtime "
              "is running)", file=sys.stderr)
        return 2
    tool = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "duckstation_oracle.py")
    cmd = [sys.executable, tool, "start", "--disc", args.disc,
           "--port", str(args.ds_port), "--wait", "90"]
    print("starting the DuckStation oracle: " + " ".join(cmd))
    if sp.run(cmd).returncode != 0:
        print("error: could not start the oracle. `python3 duckstation_oracle.py "
              "status` will say what is missing.", file=sys.stderr)
        return 2
    return 0


def display_origin(state):
    """Where in VRAM this side's visible framebuffer starts.

    The two emulators do not put it in the same place. Measured: psx-runtime
    reports (0, 0) and the oracle (1, 8), consistently, in both halves of the
    double buffer (0/240 against 8/248). Each screenshot is that side's own
    display area, so pixel (0,0) of one is NOT pixel (0,0) of the other.
    """
    c = canon(state)
    try:
        return int(c.get("display_x", 0)), int(c.get("display_y", 0))
    except (TypeError, ValueError):
        return 0, 0


def compare_images(pa, pb, out_path, origin_a=(0, 0), origin_b=(0, 0)):
    a = Image.open(pa).convert("RGB")
    b = Image.open(pb).convert("RGB")
    note = None

    # Line the two up by their VRAM origins before comparing anything.
    #
    # Without this the comparison was meaningless: a 1-pixel horizontal and
    # 8-pixel vertical shift makes almost every pixel differ, and 91.9% is
    # exactly what two CORRECT images produce when they are offset. The
    # difference was being read as a rendering fault when it was a framebuffer
    # placed elsewhere in VRAM.
    #
    # Only the horizontal origin and the vertical offset WITHIN the buffer
    # matter; the 240-line jump between buffer halves is double buffering, not
    # a displacement, so it is taken modulo the frame height.
    dx = origin_b[0] - origin_a[0]
    dy = (origin_b[1] - origin_a[1]) % 240
    if dy > 120:
        dy -= 240
    if dx or dy:
        note = (f"aligned by VRAM origin: the oracle's framebuffer sits "
                f"{dx:+d},{dy:+d} from psx-runtime's")
        # Shift into a common frame by cropping the overlapping region.
        ax0, ay0 = max(0, dx), max(0, dy)
        bx0, by0 = max(0, -dx), max(0, -dy)
        w = min(a.width - ax0, b.width - bx0)
        h = min(a.height - ay0, b.height - by0)
        if w > 0 and h > 0:
            a = a.crop((ax0, ay0, ax0 + w, ay0 + h))
            b = b.crop((bx0, by0, bx0 + w, by0 + h))

    if a.size != b.size:
        note = ((note + "; " if note else "")
                + f"size mismatch: {a.size} vs {b.size}; compared over the "
                  f"common region")
        w, h = min(a.width, b.width), min(a.height, b.height)
        a, b = a.crop((0, 0, w, h)), b.crop((0, 0, w, h))
    na = np.asarray(a, dtype=np.int16)
    nb = np.asarray(b, dtype=np.int16)
    d = np.abs(na - nb)
    per_px = d.max(axis=2)
    differing = int((per_px > 0).sum())
    total = int(per_px.size)

    # The PSX writes 5 bits per channel; the runtime and DuckStation both
    # expand to 8 by shifting left 3, so an exact match is achievable and a
    # 1-2 level difference is still a real difference, not rounding.
    vis = np.zeros((*per_px.shape, 3), dtype=np.uint8)
    vis[..., 0] = np.clip(per_px * 4, 0, 255)
    vis[..., 1] = np.clip(per_px, 0, 255) // 2
    Image.fromarray(vis, "RGB").save(out_path)

    ys, xs = np.nonzero(per_px)
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if differing else None
    return {
        "width": a.width, "height": a.height,
        "differing_pixels": differing,
        "total_pixels": total,
        "differing_pct": round(100.0 * differing / max(1, total), 4),
        "max_channel_delta": int(d.max()),
        "mean_abs_delta": round(float(d.mean()), 4),
        "diff_bbox": bbox,
        "note": note,
        "diff_image": out_path,
    }


# The two servers name the same GPU facts differently. Comparing raw keys made
# every field read "native=None", which looks like the runtime failing to report
# its display state and is really just a vocabulary mismatch -- a difference
# invented by the tool, printed under a heading that says the emulators differ.
FIELD_ALIASES = {
    "display_x":     ("display_x", "display_area_x"),
    "display_y":     ("display_y", "display_area_y"),
    "width":         ("width", "disp_w", "display_width"),
    "height":        ("height", "disp_h", "display_height"),
    "depth24":       ("depth24", "display_24bit"),
    "display_enable": ("display_enable", "display_disable"),
    "interlaced":    ("interlaced", "display_interlaced"),
    "pal":           ("pal", "pal_mode"),
}


def canon(state):
    """One vocabulary, so a delta means a real difference."""
    out = {}
    for name, keys in FIELD_ALIASES.items():
        for k in keys:
            if k in state:
                v = state[k]
                # display_disable is the inverse of display_enable; folding them
                # into one name without inverting would report every frame as a
                # difference.
                if k == "display_disable" and isinstance(v, (int, bool)):
                    v = 0 if v else 1
                out[name] = v
                break
    for k, v in state.items():
        if k not in ("id", "ok") and not any(k in ks for ks in FIELD_ALIASES.values()):
            out.setdefault(k, v)
    return out


def gpu_state_delta(a, b):
    ca, cb = canon(a), canon(b)
    out = {}
    for k in sorted(set(ca) | set(cb)):
        av, bv = ca.get(k), cb.get(k)
        if av != bv:
            # Only ONE side knowing a field is not the two disagreeing about it.
            out[k] = {"native": av, "duckstation": bv,
                      "one_sided": (av is None) != (bv is None)}
    return out


def align_frames(native, ds, want, result, settle=400):
    """Park both emulators on the same guest frame. True if it worked.

    Forward-only by necessity: run_to_frame refuses a target already passed, and
    there is no rewind command on either side. The target is therefore the
    LATER of the two positions -- whichever emulator is behind steps up to it.

    psx-runtime is parked for the whole operation, which is the point. Leaving
    it running while DuckStation was steered guaranteed the two screenshots came
    from different frames, and the old code then printed that as a warning to be
    read past rather than a reason the comparison was void.
    """
    try:
        native.cmd("pause")
    except DebugError as e:
        result["native_pause_error"] = str(e)
        result["native_frame"], result["duckstation_frame"] = native.frame(), ds.frame()
        return False

    fa, fb = native.frame(), ds.frame()
    target = want if want is not None else max(fa, fb)
    result["target_frame"] = target

    # A large gap cannot be closed meaningfully.
    #
    # Frame numbers count from each emulator's OWN boot, so equal numbers are
    # not the same guest moment -- driving the oracle 1818 frames forward moves
    # it thirty seconds past the effect without bringing it any closer to what
    # psx-runtime is showing. Measured on a live pair: native 15822 against
    # oracle 14004.
    MEANINGFUL_GAP = 120
    if abs(fa - fb) > MEANINGFUL_GAP:
        result["frame_gap"] = fa - fb
        result["alignment_note"] = (
            f"psx-runtime is at {fa} and the oracle at {fb}, {abs(fa - fb)} "
            f"frames apart. These count from each emulator's own boot, so "
            f"closing that gap would not put them at the same guest moment — it "
            f"would only run one of them past the effect. Not attempted.")
        result["native_frame"], result["duckstation_frame"] = fa, fb
        return False

    if fb < target:
        try:
            ds.run_to_frame(target)
            # run_to_frame ARMS a target; it does not block. Reading the frame
            # straight afterwards reports where the oracle still is, which is
            # how alignment "failed" while never having been waited for.
            for _ in range(settle):
                if ds.frame() >= target:
                    break
                time.sleep(0.05)
        except DebugError as e:
            result["duckstation_drive_error"] = str(e)
    if fa < target:
        try:
            native.cmd("step", n=int(target - fa))
            for _ in range(settle):
                st = native.raw("pause_state")
                if st.get("paused") and native.frame() >= target:
                    break
                time.sleep(0.02)
        except DebugError as e:
            result["native_drive_error"] = str(e)

    fa, fb = native.frame(), ds.frame()
    result["native_frame"], result["duckstation_frame"] = fa, fb
    return fa == fb


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--native-port", type=int, default=DEFAULT_NATIVE_PORT)
    ap.add_argument("--ds-port", type=int, default=DEFAULT_DUCKSTATION_PORT)
    ap.add_argument("--frame", type=int, default=None,
                    help="target guest frame. Default: whichever emulator is "
                         "further ahead, since neither can rewind and both can "
                         "step forward.")
    ap.add_argument("--out", default="analysis/frames/parity")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--start-oracle", action="store_true",
                    help="if nothing answers on the DuckStation port, start the "
                         "installed oracle first (needs --disc)")
    ap.add_argument("--disc", default=None,
                    help="disc image for --start-oracle; must be the same one "
                         "psx-runtime is running")
    ap.add_argument("--wait-for", default="",
                    help="wait for this primitive class on BOTH "
                         "sides before comparing, e.g. PolyG4+semi. "
                         "Comparing images of a scene without the "
                         "effect answers a different question.")
    ap.add_argument("--wait-secs", type=float, default=120.0)
    ap.add_argument("--dump-native", action="store_true",
                    help="also capture the native GP0 stream (no DuckStation counterpart)")
    args = ap.parse_args(argv)

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    result = {"kind": "psx-gpu-parity", "version": 1, "frame": args.frame}

    if args.start_oracle:
        rc = ensure_oracle(args)
        if rc:
            return rc

    try:
        with DebugConn(args.host, args.native_port, args.timeout) as native, \
             DebugConn(args.host, args.ds_port, args.timeout) as ds:
            # Align BOTH sides, not just DuckStation.
            #
            # This used to read the native frame, drive DuckStation at it, and
            # shoot -- while the native run kept going, so the two images were
            # always of different frames. It also failed outright whenever the
            # oracle had already passed the target, because run_to_frame cannot
            # go backwards.
            #
            # Neither emulator can rewind on demand, but both can move FORWARD:
            # psx-runtime by stepping (pause/step are real again), DuckStation
            # by run_to_frame. So the meeting point is whichever is further
            # ahead, and the one behind catches up. Both are parked when the
            # screenshots are taken.
            # Wait for the effect on both sides before aligning. Comparing
            # images of a scene that does not contain the effect answers a
            # question nobody asked, and this tool has the same race every
            # other one here had.
            if args.wait_for:
                for label, conn in (("psx-runtime", native), ("oracle", ds)):
                    on, drawing = wait_for_class(conn, args.wait_for,
                                                 args.wait_secs, out=sys.stderr)
                    if not on:
                        top = ", ".join(f"{k} x{v}" for k, v in
                                        sorted(drawing.items(),
                                               key=lambda kv: -kv[1])[:4])
                        print(f"error: {args.wait_for} never appeared on "
                              f"{label} within {args.wait_secs:.0f}s. Drawing "
                              f"instead: {top or 'nothing'}.", file=sys.stderr)
                        result["wait_error"] = label
                        with open(os.path.join(outdir, "parity.json"), "w",
                                  encoding="utf-8") as f:
                            json.dump(result, f, indent=1)
                        return 1

            aligned = align_frames(native, ds, args.frame, result)
            fa, fb = result["native_frame"], result["duckstation_frame"]
            if not aligned:
                result["frame_mismatch"] = True
                print(f"warning: could not align (native {fa}, duckstation {fb}); "
                      f"the images below are of DIFFERENT frames and a pixel "
                      f"diff between them means nothing.", file=sys.stderr)
            else:
                print(f"aligned both at frame {fa}")

            pa = os.path.join(outdir, "native.png")
            pb = os.path.join(outdir, "duckstation.png")
            native.screenshot(pa)
            ds.screenshot(pb)
            result["native_image"] = pa
            result["duckstation_image"] = pb

            try:
                st_a = native.cmd("gpu_state")
                st_b = ds.cmd("gpu_state")
                result["gpu_state"] = gpu_state_delta(st_a, st_b)
                # Kept for compare_images: the framebuffers are not at the same
                # place in VRAM, and comparing them un-shifted compares nothing.
                result["origin_native"] = list(display_origin(st_a))
                result["origin_oracle"] = list(display_origin(st_b))
            except DebugError as e:
                result["gpu_state_error"] = str(e)

            if args.dump_native:
                from psx_gpu_frame import capture, save_dump
                d = capture(native, frame=fa, label="native")
                p = os.path.join(outdir, "native-gp0.json")
                save_dump(d, p)
                result["native_gp0"] = p
                result["native_gp0_note"] = (
                    "no DuckStation counterpart: the oracle patch does not "
                    "implement gpu_frame_dump")

            # Hand the game back. The park has a client-silence timeout and
            # would resume itself eventually, but leaving someone's emulator
            # frozen because a tool exited is not something to rely on a
            # watchdog to undo.
            try:
                native.cmd("continue")
            except DebugError:
                pass
    except DebugError as e:
        # align_frames() parks psx-runtime; if anything after that raised, the
        # game is still parked and the tool is about to exit. The park has a
        # client-silence timeout, but leaving someone's emulator frozen and
        # relying on a watchdog to undo it is not acceptable behaviour.
        try:
            with DebugConn(args.host, args.native_port, 5.0) as n2:
                n2.cmd("continue")
        except DebugError:
            pass
        print(f"error: {e}", file=sys.stderr)
        return 2

    result["image"] = compare_images(
        pa, pb, os.path.join(outdir, "diff.png"),
        origin_a=tuple(result.get("origin_native", (0, 0))),
        origin_b=tuple(result.get("origin_oracle", (0, 0))))
    with open(os.path.join(outdir, "parity.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)

    img = result["image"]
    print(f"native frame {result['native_frame']}  duckstation frame "
          f"{result['duckstation_frame']}")
    if img["note"]:
        print(f"  {img['note']}")
    print(f"  {img['differing_pixels']}/{img['total_pixels']} pixels differ "
          f"({img['differing_pct']}%), max channel delta {img['max_channel_delta']}")
    if img["diff_bbox"]:
        print(f"  differences confined to bbox {img['diff_bbox']}")
    if result.get("gpu_state"):
        print("  display state differs:")
        for k, v in result["gpu_state"].items():
            print(f"    {k}: native={v['native']} duckstation={v['duckstation']}")
    print()
    if img["differing_pixels"] == 0:
        print("VERDICT: images identical -- the guest ran the same on both. A visible "
              "render bug here is in the renderer, not the game code.")
    else:
        print("VERDICT: images differ -- the guest state or the GP0 stream diverged. "
              "Capture both sides' frames and run tools/cosim.py to bracket where.")
    print(f"wrote {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
