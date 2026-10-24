#!/usr/bin/env python3
"""Manage arbitrary scratchpad windows through the running Niri's IPC socket."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time


# Size observed with the original patch on this desktop; not a maximized tile.
DEFAULT_SLOT_SIZE = (954, 1028)


class Niri:
    def __init__(self, path):
        self.path = path

    def request(self, request):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(self.path)
            connection.sendall(json.dumps(request).encode() + b"\n")
            with connection.makefile("r") as stream:
                reply = json.loads(stream.readline())
        if "Err" in reply:
            raise RuntimeError(reply["Err"])
        return reply["Ok"]

    def query(self, name):
        return self.request(name)[name]

    def action(self, action_name, **arguments):
        self.request({"Action": {action_name: arguments}})


def manage(niri, state, command, workspace_name="scratch", all_workspaces=False):
    workspaces = niri.query("Workspaces")
    stash = next((w for w in workspaces if w["name"] == workspace_name), None)
    current = next((w for w in workspaces if w["is_focused"]), None)
    if stash is None:
        raise RuntimeError(f"缺少工作区 {workspace_name!r}，请先应用 scratchpad 配置")
    if current is None:
        return
    def ordinary_workspace(available):
        ordinary = [w for w in available if w["id"] != stash["id"]]
        return next((w for w in ordinary if w["id"] == state.get("last_workspace")),
                    next((w for w in ordinary if w.get("output") == current.get("output")),
                         next(iter(ordinary), None)))

    recovering = current["id"] == stash["id"]
    if recovering:
        if command not in ("toggle", "next"):
            return
        destination = ordinary_workspace(workspaces)
        if destination is None:
            raise RuntimeError("没有可用的普通工作区")
        niri.action("FocusWorkspace", reference={"Id": destination["id"]})
        current = destination
    state["last_workspace"] = current["id"]

    windows = {w["id"]: w for w in niri.query("Windows")}
    focused = next((w for w in windows.values() if w["is_focused"]), None)
    # Recover windows from the workspace if runtime state was lost, and discard
    # closed windows. Window IDs and state are scoped to this compositor session.
    members = [wid for wid in state.get("order", []) if wid in windows]
    members.extend(wid for wid, w in windows.items()
                 if w["workspace_id"] == stash["id"] and wid not in members)
    state["order"] = members
    origins = state.setdefault("return_workspaces", {})
    pins = state.setdefault("workspace_pins", {})
    for key in list(origins):
        if key not in {str(wid) for wid in windows}:
            del origins[key]

    def cleanup_pins():
        available = {w["id"]: w for w in niri.query("Workspaces")}
        for key, name in list(pins.items()):
            if any(origin["id"] == int(key) for origin in origins.values()):
                continue
            workspace = available.get(int(key))
            # Never undo a name the user changed while the window was borrowed.
            if workspace and workspace["name"] == name:
                niri.action("UnsetWorkspaceName", reference={"Id": int(key)})
            del pins[key]

    cleanup_pins()
    order = members
    if all_workspaces and command in ("toggle", "next"):
        # Only other workspaces and already borrowed members are candidates.
        # Keep this separate from explicit scratchpad membership, so ordinary
        # windows never become permanently stashed merely by being discovered.
        candidates = set(members)
        candidates.update(wid for wid, w in windows.items()
                          if w["workspace_id"] is not None
                          and (w["workspace_id"] != current["id"] or str(wid) in origins))
        order = [wid for wid in state.get("auto_order", []) if wid in candidates]
        order.extend(wid for wid in sorted(candidates) if wid not in order)
        state["auto_order"] = order
        visible = state.get("visible_window")
        if (command == "toggle" and visible in order
                and windows[visible]["workspace_id"] == current["id"]):
            order.remove(visible)
            order.append(visible)
    # Reset legacy arbitrary slot bounds once; retain membership and backups.
    if state.get("slot_policy") != "original-window-954x1028-v1":
        state.pop("slot", None)
        state["slot_policy"] = "original-window-954x1028-v1"
        state["center_slot"] = True
    modes = state.setdefault("restore_modes", {})
    geometries = state.setdefault("restore_geometry", {})
    for records in (modes, geometries):
        for key in list(records):
            if key not in {str(wid) for wid in windows}:
                del records[key]

    def geometry(window):
        layout = window.get("layout", {})
        position, size = layout.get("tile_pos_in_workspace_view"), layout.get("window_size")
        if not window.get("is_floating") or not position or not size:
            return None
        return {"x": position[0], "y": position[1], "width": size[0], "height": size[1]}

    def place(wid, bounds):
        niri.action("SetWindowWidth", id=wid, change={"SetFixed": bounds["width"]})
        niri.action("SetWindowHeight", id=wid, change={"SetFixed": bounds["height"]})
        # IPC absolute coordinates are relative to the working area, whereas
        # layout coordinates include panel offsets. Deltas share one origin.
        window = next((w for w in niri.query("Windows") if w["id"] == wid), None)
        current_bounds = geometry(window) if window else None
        if current_bounds:
            niri.action("MoveFloatingWindow", id=wid,
                        x={"AdjustFixed": bounds["x"] - current_bounds["x"]},
                        y={"AdjustFixed": bounds["y"] - current_bounds["y"]})

    def center_slot(wid):
        # Use the original observed window size, not a maximized app's size.
        width, height = DEFAULT_SLOT_SIZE
        niri.action("SetWindowWidth", id=wid, change={"SetFixed": width})
        niri.action("SetWindowHeight", id=wid, change={"SetFixed": height})
        # Wayland clients acknowledge resize asynchronously. Do not save the
        # old full-width geometry as the shared slot before they respond.
        deadline = time.monotonic() + 0.5
        while True:
            window = next((w for w in niri.query("Windows") if w["id"] == wid), None)
            if (window is None or not window.get("layout")
                    or window.get("layout", {}).get("window_size") == [width, height]
                    or time.monotonic() >= deadline):
                break
            time.sleep(0.02)
        niri.action("CenterWindow", id=wid)
        window = next((w for w in niri.query("Windows") if w["id"] == wid), None)
        bounds = geometry(window) if window else None
        if bounds:
            state["slot"] = {"output": current.get("output"), "bounds": bounds}
        state.pop("center_slot", None)

    def move(wid, workspace_id):
        # IDs are global across outputs; indices are local to each monitor.
        niri.action("MoveWindowToWorkspace", window_id=wid,
                    reference={"Id": workspace_id}, focus=False)

    def hide(window, keep_in_stash=False):
        nonlocal current
        key = str(window["id"])
        bounds = geometry(window)
        if key in modes and bounds and not state.get("center_slot"):
            state["slot"] = {"output": current.get("output"), "bounds": bounds}
        origin = origins.get(key)
        destination = stash["id"]
        if origin and not keep_in_stash:
            available = niri.query("Workspaces")
            if any(w["id"] == origin["id"] for w in available):
                destination = origin["id"]
            else:
                # Output removal or an explicit workspace deletion may still
                # invalidate a pinned workspace. Keep the window recoverable.
                if window["id"] not in members:
                    members.append(window["id"])
                print("niri-scratchpad: 原工作区已不存在，窗口已收回 scratch", file=sys.stderr)
        move(window["id"], destination)
        # Restore geometry in the hidden floating layout, even if the user
        # manually toggled the shown member back to tiling before hiding it.
        is_floating = window.get("is_floating", False)
        if key in geometries:
            if not is_floating:
                niri.action("MoveWindowToFloating", id=window["id"])
                is_floating = True
            place(window["id"], geometries[key])
            del geometries[key]
        if state.get("visible_window") == window["id"]:
            state.pop("visible_window", None)
        # Restore the pre-summon mode only after leaving the visible workspace,
        # so restoring tiling cannot reflow its other windows.
        key = str(window["id"])
        if key in modes:
            original_floating = modes[key]
            if original_floating != is_floating:
                niri.action("MoveWindowToFloating" if original_floating else "MoveWindowToTiling",
                            id=window["id"])
            del modes[key]
        if origin and not origin.get("floating", False) and origin.get("size"):
            # Restore the original tiling dimensions after restoring its mode.
            width, height = origin["size"]
            niri.action("SetWindowWidth", id=window["id"], change={"SetFixed": width})
            niri.action("SetWindowHeight", id=window["id"], change={"SetFixed": height})
        origins.pop(key, None)
        cleanup_pins()
        # Niri can remove an emptied unnamed workspace and focus scratch.
        # Keep the user on an ordinary workspace, including before cycling.
        available = niri.query("Workspaces")
        active = next((w for w in available if w["is_focused"]), None)
        if active and active["id"] == stash["id"]:
            active = ordinary_workspace(available)
            if active:
                niri.action("FocusWorkspace", reference={"Id": active["id"]})
        if active and active["id"] != stash["id"]:
            current = active
            state["last_workspace"] = current["id"]
        previous = windows.get(state.get("return_window"))
        if (window["is_focused"] and previous is not None
                and previous["id"] != window["id"]
                and previous["workspace_id"] == current["id"]):
            # Recheck after moving: never follow a closed/moved return window
            # onto another workspace just to restore keyboard focus.
            live = {w["id"]: w for w in niri.query("Windows")}
            previous = live.get(previous["id"])
            if previous and previous["workspace_id"] == current["id"]:
                niri.action("FocusWindow", id=previous["id"])
        state.pop("return_window", None)

    if command == "stash":
        if focused is not None:
            hide(focused, keep_in_stash=True)
            if focused["id"] in order:
                order.remove(focused["id"])
            order.append(focused["id"])
        return

    if command == "release":
        if focused and (focused["id"] in order or str(focused["id"]) in origins):
            if focused["id"] in order:
                order.remove(focused["id"])
            modes.pop(str(focused["id"]), None)
            geometries.pop(str(focused["id"]), None)
            origins.pop(str(focused["id"]), None)
            cleanup_pins()
            if state.get("visible_window") == focused["id"]:
                state.pop("visible_window", None)
            state.pop("return_window", None)
        return

    if not order:
        return

    # Cycling replaces the visible member even if keyboard focus was lost.
    shown = focused if focused and focused["id"] in order else None
    if command == "next" and shown is None:
        shown = windows.get(state.get("visible_window"))
        if not shown or shown["workspace_id"] != current["id"]:
            shown = next((w for w in windows.values() if w["id"] in order
                          and str(w["id"]) in modes and w["workspace_id"] == current["id"]), None)
    if shown and not recovering:
        order.remove(shown["id"])
        order.append(shown["id"])
        hide(shown)
        if command == "toggle":
            return
        order.insert(0, order.pop())
    elif command == "next":
        order.insert(0, order.pop())

    target = order[-1]
    live = niri.query("Windows")
    target_window = next((w for w in live if w["id"] == target), None)
    if target_window is None:
        return
    if all_workspaces and target not in members and str(target) not in origins:
        origin = next((w for w in niri.query("Workspaces")
                       if w["id"] == target_window["workspace_id"]), None)
        if origin is None or origin["id"] == current["id"]:
            return
        origins[str(target)] = {"id": origin["id"],
                                "floating": target_window.get("is_floating", False),
                                "size": target_window.get("layout", {}).get("window_size")}
        # Empty unnamed workspaces disappear in Niri. Temporarily name only
        # the borrowed window's source, then unname it once it is returned.
        if origin["name"] is None:
            name = f"scratchpad-return-{origin['id']}-{target}"
            names = {w["name"] for w in niri.query("Workspaces")}
            while name in names:
                name += "-"
            pins[str(origin["id"])] = name
            niri.action("SetWorkspaceName", name=name, workspace={"Id": origin["id"]})
    # Floating is temporary: remember the mode before the first summon, and
    # convert while still hidden so the destination's tiling stays untouched.
    modes.setdefault(str(target), target_window.get("is_floating", False))
    if not target_window.get("is_floating", False):
        niri.action("MoveWindowToFloating", id=target)
        target_window = next((w for w in niri.query("Windows") if w["id"] == target), target_window)
    if str(target) not in geometries:
        bounds = geometry(target_window)
        if bounds:
            geometries[str(target)] = bounds
    # Refocusing a visible scratchpad must not reinsert its column or change
    # its floating position. Preserve the original return focus as well.
    if target_window["workspace_id"] == current["id"]:
        if state.get("center_slot"):
            if str(target) in geometries:
                place(target, geometries[str(target)])
            center_slot(target)
        state["visible_window"] = target
        niri.action("FocusWindow", id=target)
        return
    # The fresh query above also captures the actual return focus after cycling.
    previous = next((w for w in live if w["is_focused"]), None)
    if previous and previous["id"] != target:
        state["return_window"] = previous["id"]
    move(target, current["id"])
    slot = state.get("slot", {})
    if slot.get("output") == current.get("output") and slot.get("bounds"):
        place(target, slot["bounds"])
    else:
        center_slot(target)
    state["visible_window"] = target
    niri.action("FocusWindow", id=target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stash", "toggle", "next", "release"))
    parser.add_argument("--all-workspaces", action="store_true",
                        help="自动呼出并循环其他工作区的窗口，收起时送回原工作区")
    args = parser.parse_args()
    path = os.environ.get("NIRI_SOCKET")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not path or not runtime:
        parser.error("需要 Niri 会话的 NIRI_SOCKET 和 XDG_RUNTIME_DIR")
    key = hashlib.sha256(path.encode()).hexdigest()[:16]
    state_path = Path(runtime) / f"niri-scratchpad-{key}.json"
    descriptor = os.open(state_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stream.seek(0)
        try:
            state = json.load(stream)
        except json.JSONDecodeError:
            state = {}
        try:
            manage(Niri(path), state, args.command, all_workspaces=args.all_workspaces)
        finally:
            # Keep membership after a partial IPC failure so another invocation
            # can recover; hidden windows also remain accessible in Overview.
            stream.seek(0)
            json.dump(state, stream)
            stream.truncate()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"niri-scratchpad: {error}", file=sys.stderr)
        sys.exit(1)
