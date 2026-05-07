"""
eeschema_reloader.py — Post-mutation reload bridge for eeschema.

After a schematic-mutating MCP tool writes changes to disk via the
SWIG / sexp path, eeschema continues showing its in-memory copy.
This module provides ``trigger_eeschema_reload``, which tries a
sequence of IPC mechanisms to make eeschema reload from disk:

    1. ``RevertDocument`` (preferred) — tells eeschema to discard its
       in-memory schematic and reload from the file it was opened from.
       Uses ``_client.send`` directly because ``kipy.KiCad`` has no
       public ``revert_document`` wrapper yet.

    2. ``RunAction("eeschema.EditorControl.revertSchematic")`` (fallback)
       — invokes the eeschema menu action by name.

    3. ``RefreshEditor(frame=FT_SCHEMATIC_EDITOR)`` (last resort) — only
       triggers a repaint, not a full reload, but is better than nothing.

All IPC failures are caught and logged at WARNING level.  The file
write MUST NOT fail because the reload bridge couldn't reach eeschema.
When IPC is not connected ``trigger_eeschema_reload`` returns
immediately as a no-op.
"""

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def trigger_eeschema_reload(
    kicad_instance: Any,
    schematic_path: Optional[str] = None,
) -> None:
    """Ask eeschema to reload from disk after a schematic file write.

    Args:
        kicad_instance: A live ``kipy.KiCad`` object (``self._kicad`` on
            ``IPCBackend``).  If ``None`` or not connected the function
            returns immediately.
        schematic_path: Absolute path to the ``.kicad_sch`` file that was
            just written.  When provided we try to match it against the
            open documents so we only revert the document we changed.
            If ``None`` we revert any open schematic document.
    """
    if kicad_instance is None:
        return

    _attempt_revert_document(kicad_instance, schematic_path)


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------


def _attempt_revert_document(kicad: Any, schematic_path: Optional[str]) -> None:
    """Try RevertDocument first, then run_action fallback, then RefreshEditor."""
    try:
        from kipy.proto.common.commands.editor_commands_pb2 import (  # type: ignore[import]
            RefreshEditor,
            RevertDocument,
        )
        from kipy.proto.common.types.base_types_pb2 import (  # type: ignore[import]
            DocumentType,
            FrameType,
        )
    except ImportError:
        # kipy not available; nothing to do
        return

    # ---- 1. RevertDocument -----------------------------------------------
    try:
        open_docs = kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning(f"eeschema reload: get_open_documents failed: {e}")
        open_docs = []

    # Build a list of (DocumentSpecifier, display_path) pairs to revert
    targets = _select_targets(open_docs, schematic_path)

    if targets:
        for doc_spec, display in targets:
            try:
                cmd = RevertDocument()
                cmd.document.CopyFrom(doc_spec)
                from kipy.proto.common.types.base_types_pb2 import (  # type: ignore[import]
                    CommandStatusResponse,
                )

                kicad._client.send(cmd, CommandStatusResponse)  # type: ignore[attr-defined]
                logger.info(f"eeschema reload: RevertDocument sent for '{display}'")
                return
            except Exception as e:
                logger.warning(f"eeschema reload: RevertDocument failed for '{display}': {e}")
    else:
        logger.debug("eeschema reload: no open schematic documents found via IPC")

    # ---- 2. run_action fallback ------------------------------------------
    _try_run_action(kicad)

    # ---- 3. RefreshEditor last resort ------------------------------------
    _try_refresh_editor(kicad, FrameType.FT_SCHEMATIC_EDITOR)  # type: ignore[attr-defined]


def _select_targets(
    open_docs: Any,
    schematic_path: Optional[str],
) -> list:
    """Return [(DocumentSpecifier, display_str)] for documents to revert.

    If *schematic_path* is given we prefer documents whose path matches.
    If none match (or *schematic_path* is None) we return all open
    schematic documents so at least something gets reverted.
    """
    all_specs = []
    try:
        for doc in open_docs:
            # doc is a kipy DocumentSpecifier proto
            display = _doc_display(doc)
            all_specs.append((doc, display))
    except Exception as e:
        logger.warning(f"eeschema reload: error iterating open documents: {e}")
        return []

    if not all_specs:
        return []

    if schematic_path is None:
        return all_specs

    # Normalise for comparison
    target = Path(schematic_path).resolve()
    matched = [(spec, disp) for spec, disp in all_specs if disp and Path(disp).resolve() == target]
    return matched if matched else all_specs


def _doc_display(doc: Any) -> str:
    """Extract a human-readable path string from a DocumentSpecifier proto."""
    try:
        # board_filename is the most common field populated for schematic docs
        if hasattr(doc, "board_filename") and doc.board_filename:
            return doc.board_filename
    except Exception:
        pass
    try:
        if hasattr(doc, "sheet_path") and doc.sheet_path:
            path_val = doc.sheet_path
            if hasattr(path_val, "path"):
                return path_val.path
    except Exception:
        pass
    return str(doc)


def _try_run_action(kicad: Any) -> None:
    """Attempt revert via the eeschema TOOL_ACTION name."""
    action = "eeschema.EditorControl.revertSchematic"
    try:
        kicad.run_action(action)
        logger.info(f"eeschema reload: run_action('{action}') sent")
    except Exception as e:
        logger.warning(f"eeschema reload: run_action('{action}') failed: {e}")


def _try_refresh_editor(kicad: Any, frame_type: int) -> None:
    """Last-resort: send RefreshEditor to force a canvas repaint."""
    try:
        from kipy.proto.common.commands.editor_commands_pb2 import (
            RefreshEditor,  # type: ignore[import]
        )
        from kipy.proto.common.types.base_types_pb2 import (
            CommandStatusResponse,  # type: ignore[import]
        )

        cmd = RefreshEditor()
        cmd.frame = frame_type
        kicad._client.send(cmd, CommandStatusResponse)  # type: ignore[attr-defined]
        logger.info("eeschema reload: RefreshEditor sent")
    except Exception as e:
        logger.warning(f"eeschema reload: RefreshEditor failed: {e}")
