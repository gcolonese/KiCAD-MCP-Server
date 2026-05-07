"""
Tests for the eeschema post-mutation reload bridge.

Three coverage axes:
  (a) Helper is invoked on each mutating tool's success path.
  (b) Helper swallows IPC errors and does not break the tool result.
  (c) Helper is a no-op when IPC is not connected.
"""

import shutil
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure the python/ subtree is importable regardless of CWD
sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

TEMPLATES_DIR = Path(__file__).parent.parent / "python" / "templates"
EMPTY_SCH = TEMPLATES_DIR / "empty.kicad_sch"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_iface(use_ipc: bool = False, kicad_obj: Any = None) -> Any:
    """Create a KiCADInterface with minimal mocking."""
    from kicad_interface import KiCADInterface

    iface = KiCADInterface()
    iface.use_ipc = use_ipc
    if use_ipc:
        iface.ipc_backend = MagicMock()
        iface.ipc_backend._kicad = kicad_obj if kicad_obj is not None else MagicMock()
    else:
        iface.ipc_backend = None
    return iface


# ---------------------------------------------------------------------------
# (c) No-op when IPC is not connected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReloadNoOpWhenNoIPC:
    """trigger_eeschema_reload must be a no-op when IPC is unavailable."""

    def test_no_ipc_backend_skips_reload(self) -> None:
        """When use_ipc is False the reload bridge must never call kipy."""
        from utils.eeschema_reloader import trigger_eeschema_reload

        mock_kicad = MagicMock()
        trigger_eeschema_reload(None)
        mock_kicad.get_open_documents.assert_not_called()
        mock_kicad._client.send.assert_not_called()

    def test_iface_no_ipc_skips_reload(self, tmp_path: Any) -> None:
        """KiCADInterface._trigger_eeschema_reload must no-op when use_ipc=False."""
        iface = _make_iface(use_ipc=False)
        # Patch trigger_eeschema_reload at the kicad_interface module level
        with patch("kicad_interface.trigger_eeschema_reload") as mock_reload:
            iface._trigger_eeschema_reload(schematic_path=str(tmp_path / "x.kicad_sch"))
            mock_reload.assert_not_called()


# ---------------------------------------------------------------------------
# (b) IPC errors are swallowed — tool result is unaffected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReloadSwallowsIpcErrors:
    """IPC failures must not propagate to the caller."""

    def test_get_open_documents_raises_still_returns(self) -> None:
        from utils.eeschema_reloader import trigger_eeschema_reload

        mock_kicad = MagicMock()
        mock_kicad.get_open_documents.side_effect = RuntimeError("socket closed")

        # Must not raise
        trigger_eeschema_reload(mock_kicad, schematic_path="/tmp/test.kicad_sch")

    def test_send_raises_on_revert_document_still_returns(self) -> None:
        from utils.eeschema_reloader import trigger_eeschema_reload

        mock_kicad = MagicMock()
        mock_kicad.get_open_documents.return_value = []  # no open docs
        mock_kicad.run_action.side_effect = RuntimeError("RPC error")
        mock_kicad._client.send.side_effect = RuntimeError("RPC error")

        # Must not raise even when all fallbacks fail
        trigger_eeschema_reload(mock_kicad, schematic_path="/tmp/test.kicad_sch")

    def test_iface_trigger_swallows_exception(self, tmp_path: Any) -> None:
        """KiCADInterface._trigger_eeschema_reload must swallow any exception."""
        iface = _make_iface(use_ipc=True)
        with patch("kicad_interface.trigger_eeschema_reload", side_effect=RuntimeError("boom")):
            # Must not raise
            iface._trigger_eeschema_reload(schematic_path=str(tmp_path / "x.kicad_sch"))


# ---------------------------------------------------------------------------
# (a) Reload is invoked on each mutating tool's SUCCESS path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReloadCalledOnMutatingToolSuccess:
    """_trigger_eeschema_reload must be called after each mutating schematic tool."""

    MUTATING_COMMANDS = [
        "add_schematic_component",
        "delete_schematic_component",
        "edit_schematic_component",
        "set_schematic_component_property",
        "remove_schematic_component_property",
        "move_schematic_component",
        "rotate_schematic_component",
        "add_schematic_wire",
        "delete_schematic_wire",
        "add_schematic_net_label",
        "delete_schematic_net_label",
        "move_schematic_net_label",
        "add_schematic_hierarchical_label",
        "add_sheet_pin",
        "add_no_connect",
        "add_schematic_text",
        "annotate_schematic",
        "snap_to_grid",
    ]

    def _run(self, command: str, tmp_path: Any) -> None:
        """Run *command* via handle_command with a success stub, assert reload called."""
        from kicad_interface import KiCADInterface

        iface = KiCADInterface()
        iface.use_ipc = True
        iface.ipc_backend = MagicMock()
        iface.ipc_backend._kicad = MagicMock()

        sch = str(tmp_path / "test.kicad_sch")
        params = {"schematicPath": sch, "dummy": True}

        # Stub the route entry directly so handle_command dispatches to our mock
        stub_result = {"success": True, "message": "stub ok"}

        with patch.object(iface, "_trigger_eeschema_reload") as mock_reload:
            # Replace the command_routes entry with a lambda that returns the stub
            iface.command_routes[command] = lambda _p: stub_result
            result = iface.handle_command(command, params)

        assert result.get("success") is True, f"stub should have returned success for {command}"
        mock_reload.assert_called_once_with(schematic_path=sch)

    @pytest.mark.parametrize("command", MUTATING_COMMANDS)
    def test_reload_called_on_success(self, command: str, tmp_path: Any) -> None:
        self._run(command, tmp_path)

    def test_reload_not_called_on_failure(self, tmp_path: Any) -> None:
        """When the handler returns success=False, reload must NOT be triggered."""
        from kicad_interface import KiCADInterface

        iface = KiCADInterface()
        iface.use_ipc = True
        iface.ipc_backend = MagicMock()
        iface.ipc_backend._kicad = MagicMock()

        sch = str(tmp_path / "test.kicad_sch")
        params = {"schematicPath": sch}

        stub_result = {"success": False, "message": "stub fail"}
        with patch.object(iface, "_trigger_eeschema_reload") as mock_reload:
            iface.command_routes["add_schematic_component"] = lambda _p: stub_result
            result = iface.handle_command("add_schematic_component", params)

        assert result.get("success") is False
        mock_reload.assert_not_called()

    def test_reload_not_called_for_read_only_command(self, tmp_path: Any) -> None:
        """Read-only commands must never trigger eeschema reload."""
        from kicad_interface import KiCADInterface

        iface = KiCADInterface()
        iface.use_ipc = True
        iface.ipc_backend = MagicMock()
        iface.ipc_backend._kicad = MagicMock()

        sch = str(tmp_path / "test.kicad_sch")
        params = {"schematicPath": sch}

        stub_result = {"success": True, "components": []}
        with patch.object(iface, "_trigger_eeschema_reload") as mock_reload:
            iface.command_routes["list_schematic_components"] = lambda _p: stub_result
            result = iface.handle_command("list_schematic_components", params)

        assert result.get("success") is True
        mock_reload.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests for trigger_eeschema_reload internals
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTriggerEeschemaReloadUnit:
    """Low-level unit tests for utils.eeschema_reloader."""

    def _make_mock_kicad(self, open_docs: list = None) -> MagicMock:
        mock = MagicMock()
        mock.get_open_documents.return_value = open_docs or []
        return mock

    def test_calls_get_open_documents_with_schematic_type(self) -> None:
        """get_open_documents must be called with DOCTYPE_SCHEMATIC."""
        from utils.eeschema_reloader import trigger_eeschema_reload

        mock_kicad = self._make_mock_kicad()
        # Stub kipy imports so the function can proceed without real kipy
        try:
            from kipy.proto.common.types.base_types_pb2 import DocumentType

            expected_type = DocumentType.DOCTYPE_SCHEMATIC
        except ImportError:
            pytest.skip("kipy not installed — skipping proto-dependent test")

        trigger_eeschema_reload(mock_kicad)
        mock_kicad.get_open_documents.assert_called_once_with(expected_type)

    def test_run_action_called_when_no_open_docs(self) -> None:
        """When no open docs found, run_action fallback must be attempted."""
        from utils.eeschema_reloader import trigger_eeschema_reload

        try:
            import kipy  # noqa: F401
        except ImportError:
            pytest.skip("kipy not installed")

        mock_kicad = self._make_mock_kicad(open_docs=[])
        trigger_eeschema_reload(mock_kicad)
        mock_kicad.run_action.assert_called()

    def test_schematic_path_passed_to_reloader(self) -> None:
        """schematic_path must be forwarded from KiCADInterface._trigger_eeschema_reload."""
        from kicad_interface import KiCADInterface

        iface = KiCADInterface()
        iface.use_ipc = True
        iface.ipc_backend = MagicMock()
        iface.ipc_backend._kicad = MagicMock()

        with patch("kicad_interface.trigger_eeschema_reload") as mock_fn:
            iface._trigger_eeschema_reload(schematic_path="/foo/bar.kicad_sch")

        mock_fn.assert_called_once_with(
            iface.ipc_backend._kicad,
            schematic_path="/foo/bar.kicad_sch",
        )
