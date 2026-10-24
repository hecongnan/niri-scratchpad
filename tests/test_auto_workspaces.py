"""Automatic borrowing must cycle remote windows without claiming local ones."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_scratchpad import Desktop, scratchpad


class WorkspaceDesktop(Desktop):
    def __init__(self):
        super().__init__()
        self.remove_empty = False

    def action(self, action_name, **arguments):
        if action_name in ("SetWorkspaceName", "UnsetWorkspaceName"):
            self.actions.append((action_name, arguments))
            reference = arguments.get("workspace", arguments.get("reference"))
            workspace = next(w for w in self.workspaces if w["id"] == reference["Id"])
            workspace["name"] = arguments.get("name")
        else:
            if action_name == "MoveWindowToWorkspace":
                assert any(w["id"] == arguments["reference"]["Id"] for w in self.workspaces)
            super().action(action_name, **arguments)
        if self.remove_empty:
            occupied = {w["workspace_id"] for w in self.windows.values()}
            self.workspaces = [w for w in self.workspaces
                               if w["name"] or w["is_focused"] or w["id"] in occupied]


class AutomaticWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.desktop = WorkspaceDesktop()
        self.state = {}
        self.add_remote(3)

    def add_remote(self, wid, floating=False, workspace=30):
        self.desktop.windows[wid] = {
            "id": wid, "workspace_id": workspace, "app_id": "remote",
            "is_focused": False, "is_floating": floating,
            "layout": {"window_size": [640 + wid, 480],
                       "tile_pos_in_workspace_view": [80.0, 120.0]},
        }

    def run_command(self, command):
        scratchpad.manage(self.desktop, self.state, command, all_workspaces=True)

    def test_cycles_all_other_workspace_windows_without_manual_stash(self):
        self.add_remote(4)
        self.add_remote(5)
        background = copy.deepcopy(self.desktop.windows[1])
        self.run_command("toggle")
        shown = [self.state["visible_window"]]
        for _ in range(3):
            self.run_command("next")
            shown.append(self.state["visible_window"])
        self.assertEqual(shown, [5, 4, 3, 5])
        self.assertEqual(self.state["order"], [])
        self.assertEqual(self.desktop.windows[1], background)
        self.assertEqual(self.desktop.windows[2]["workspace_id"], 10)
        self.assertFalse(self.desktop.windows[2]["is_floating"])
        for wid in (3, 4):
            self.assertEqual(self.desktop.windows[wid]["workspace_id"], 30)

    def test_single_manual_member_does_not_block_other_workspace_candidates(self):
        self.desktop.windows[6] = {
            "id": 6, "workspace_id": 20, "is_focused": False, "is_floating": False,
        }
        self.state["order"] = [6]
        self.run_command("toggle")
        first = self.state["visible_window"]
        self.run_command("next")
        second = self.state["visible_window"]
        self.assertEqual({first, second}, {3, 6})
        self.run_command("next")
        self.assertEqual(self.state["visible_window"], first)
        self.assertEqual(self.state["order"], [6])

    def test_hiding_restores_original_workspace_mode_and_size(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                self.setUp()
                self.desktop.windows[3]["is_floating"] = floating
                original = copy.deepcopy(self.desktop.windows[3])
                self.run_command("toggle")
                self.assertEqual(self.desktop.windows[3]["workspace_id"], 10)
                self.assertTrue(self.desktop.windows[3]["is_floating"])
                self.run_command("toggle")
                self.assertEqual(self.desktop.windows[3], original)
                self.assertTrue(self.desktop.windows[2]["is_focused"])
                self.assertEqual(self.state["return_workspaces"], {})
                self.assertEqual(self.state["workspace_pins"], {})
                self.assertIsNone(self.desktop.workspaces[2]["name"])

    def test_last_source_window_does_not_lose_its_workspace(self):
        self.desktop.remove_empty = True
        self.run_command("toggle")
        source = next(w for w in self.desktop.workspaces if w["id"] == 30)
        self.assertTrue(source["name"].startswith("scratchpad-return-"))
        self.run_command("toggle")
        source = next(w for w in self.desktop.workspaces if w["id"] == 30)
        self.assertIsNone(source["name"])
        self.assertEqual(self.desktop.windows[3]["workspace_id"], 30)

    def test_source_name_changed_by_user_is_preserved(self):
        self.run_command("toggle")
        self.desktop.workspaces[2]["name"] = "user-project"
        self.run_command("toggle")
        self.assertEqual(self.desktop.workspaces[2]["name"], "user-project")

    def test_existing_named_source_is_never_renamed(self):
        self.desktop.workspaces[2]["name"] = "project"
        self.run_command("toggle")
        self.run_command("toggle")
        self.assertEqual(self.desktop.workspaces[2]["name"], "project")
        self.assertFalse(any(name in ("SetWorkspaceName", "UnsetWorkspaceName")
                             for name, _ in self.desktop.actions))

    def test_new_windows_join_and_closed_windows_leave_on_next_invocation(self):
        self.run_command("toggle")
        self.add_remote(4)
        self.run_command("next")
        self.assertEqual(self.state["visible_window"], 4)
        del self.desktop.windows[3]
        self.run_command("next")
        self.assertEqual(self.state["visible_window"], 4)
        self.assertNotIn(3, self.state["auto_order"])
        self.assertNotIn("3", self.state["return_workspaces"])

    def test_refocus_after_lost_focus_keeps_current_borrowed_window(self):
        self.run_command("toggle")
        self.add_remote(4)
        self.desktop.focus(2)
        self.desktop.actions.clear()
        self.run_command("toggle")
        self.assertEqual(self.desktop.actions, [("FocusWindow", {"id": 3})])
        self.run_command("toggle")
        self.assertEqual(self.desktop.windows[3]["workspace_id"], 30)

    def test_explicit_stash_changes_borrowed_window_to_manual_member(self):
        self.run_command("toggle")
        self.run_command("stash")
        self.assertEqual(self.desktop.windows[3]["workspace_id"], 20)
        self.assertEqual(self.state["order"], [3])
        self.assertEqual(self.state["return_workspaces"], {})
        self.assertEqual(self.state["workspace_pins"], {})

    def test_release_keeps_borrowed_window_and_cleans_source_pin(self):
        self.run_command("toggle")
        self.run_command("release")
        self.assertEqual(self.desktop.windows[3]["workspace_id"], 10)
        self.assertTrue(self.desktop.windows[3]["is_floating"])
        self.assertEqual(self.state["return_workspaces"], {})
        self.assertEqual(self.state["workspace_pins"], {})
        self.run_command("next")
        self.assertNotIn(3, self.state["auto_order"])

    def test_closed_borrowed_window_cleans_up_empty_source_pin(self):
        self.run_command("toggle")
        del self.desktop.windows[3]
        self.desktop.focus(2)
        self.run_command("toggle")
        self.assertEqual(self.state["return_workspaces"], {})
        self.assertEqual(self.state["workspace_pins"], {})
        self.assertIsNone(self.desktop.workspaces[2]["name"])

    def test_missing_source_falls_back_to_recoverable_manual_stash(self):
        self.run_command("toggle")
        self.desktop.workspaces = [w for w in self.desktop.workspaces if w["id"] != 30]
        with patch("sys.stderr"):
            self.run_command("toggle")
        self.assertEqual(self.desktop.windows[3]["workspace_id"], 20)
        self.assertIn(3, self.state["order"])

    def test_no_remote_windows_does_not_move_current_workspace_windows(self):
        del self.desktop.windows[3]
        self.run_command("toggle")
        self.run_command("next")
        self.assertEqual(self.desktop.actions, [])

    def test_automatic_state_survives_real_cli_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(scratchpad.os.environ, {
                "NIRI_SOCKET": "/test/niri.sock", "XDG_RUNTIME_DIR": directory,
            }), patch.object(scratchpad, "Niri", return_value=self.desktop):
                for _ in range(2):
                    with patch.object(scratchpad.sys, "argv", [
                        "scratchpad.py", "toggle", "--all-workspaces",
                    ]):
                        scratchpad.main()
                saved = json.loads(next(Path(directory).glob("*.json")).read_text())
                self.assertEqual(self.desktop.windows[3]["workspace_id"], 30)
                self.assertEqual(saved["return_workspaces"], {})
                self.assertEqual(saved["workspace_pins"], {})


if __name__ == "__main__":
    unittest.main()
