"""Compositor screen-capture helper (mutter ScreenCast -> PipeWire).

Runs under the *system* python3 (needs PyGObject + GStreamer + the
pipewiresrc plugin), not the bundled interpreter:

    python3 -m app.pipewiregrab [--fps N]

It records the session's primary monitor through the compositor's own
ScreenCast API (org.gnome.Mutter.ScreenCast - the same mechanism the
desktop-portal uses, no approval dialog involved) and writes raw BGRA
frames to stdout:

    line 1: {"width": W, "height": H, "monitor": "DP-0"}\\n
    then:   W*H*4 raw BGRA bytes per frame, back to back

The D-Bus session must outlive the stream, so this process stays in a
mainloop until killed; whoever spawned us just reads frames off the pipe.

Why this exists: on many desktops the X11 root window carries no usable
pixels - XWayland keeps them off the X screen entirely, and NVIDIA's GLX
compositor leaves the root grab pure black (the classic "screenshot is
black" problem).  The compositor's PipeWire stream is the only honest
capture path there.  Works for both X11 and Wayland GNOME sessions.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def fail(msg: str) -> "None":
    print(msg, file=sys.stderr, flush=True)
    sys.exit(1)


def resolve_node_name(node_id: int) -> str:
    """Map a PipeWire node id to its node.name (pipewiresrc targets by name)."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, timeout=5)
        for obj in json.loads(out.stdout.decode(errors="replace")):
            if obj.get("id") == node_id and "Node" in str(obj.get("type", "")):
                props = (obj.get("info") or {}).get("props") or {}
                name = props.get("node.name")
                if name:
                    return name
    except Exception:
        pass
    return "gnome-shell"  # mutter's stream nodes are named this


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=float, default=0.0,
                    help="cap the output frame rate (0 = as delivered)")
    args = ap.parse_args()

    try:
        import gi
        from gi.repository import Gio, GLib
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except Exception as exc:
        fail(f"compositor capture unavailable on this system (gi/Gst: {exc})")

    DEST = "org.gnome.Mutter.ScreenCast"
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(path, iface, method, params, reply):
        return bus.call_sync(DEST, path, iface, method, params,
                             GLib.VariantType(reply), Gio.DBusCallFlags.NONE,
                             15000, None)

    try:
        session = call("/org/gnome/Mutter/ScreenCast", DEST, "CreateSession",
                       GLib.Variant("(a{sv})", ({},)), "(o)").unpack()[0]
        stream = call(session, DEST + ".Session", "RecordMonitor",
                      GLib.Variant("(sa{sv})", ("", {})), "(o)").unpack()[0]
    except GLib.Error as exc:
        fail(f"compositor ScreenCast API unavailable: {exc.message}")
    node: dict = {}
    loop = GLib.MainLoop()

    def on_node(_c, _s, _p, _i, _sig, params, _u):
        node["id"] = params.unpack()[0]
        loop.quit()

    bus.signal_subscribe(DEST, DEST + ".Stream", "PipeWireStreamAdded", stream,
                         None, Gio.DBusSignalFlags.NONE, on_node, None)
    try:
        call(session, DEST + ".Session", "Start", None, "()")
    except GLib.Error as exc:
        fail(f"could not start screen-cast session: {exc.message}")

    GLib.timeout_add_seconds(10, loop.quit)
    loop.run()
    if "id" not in node:
        fail("compositor never published a video stream")

    props = bus.call_sync(
        DEST, stream, "org.freedesktop.DBus.Properties", "GetAll",
        GLib.Variant("(s)", (DEST + ".Stream",)), GLib.VariantType("(a{sv})"),
        Gio.DBusCallFlags.NONE, 15000, None).unpack()[0]
    p = props.get("Parameters", {})
    width, height = p.get("size", (0, 0))
    monitor = p.get("output-name", "?")
    if not width or not height:
        fail(f"stream reported no size: {props}")
    node_name = resolve_node_name(node["id"])
    print(f"compositor capture: {monitor} {width}x{height} "
          f"node={node['id']} name={node_name}", file=sys.stderr, flush=True)

    Gst.init(None)
    throttle = (1.0 / args.fps) if args.fps > 0 else 0.0
    desc = (f"pipewiresrc target-object={node_name} ! "
            f"videoconvert ! video/x-raw,format=BGRA,width={width},height={height} ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true")
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error as exc:
        fail(f"cannot build capture pipeline: {exc.message}")
    sink = pipe.get_by_name("sink")
    out = sys.stdout.buffer
    state = {"last": 0.0}

    header = json.dumps({"width": width, "height": height, "monitor": monitor})
    out.write(header.encode() + b"\n")
    out.flush()

    def on_sample(_sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        now = time.monotonic()
        if throttle and now - state["last"] < throttle:
            return Gst.FlowReturn.OK  # keep only the newest frame
        state["last"] = now
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            out.write(bytes(mapinfo.data))
            out.flush()
        finally:
            buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    sink.connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        pipe.set_state(Gst.State.NULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
