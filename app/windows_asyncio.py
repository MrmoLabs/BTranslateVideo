"""Handle a Windows peer reset during an already-closing transport only."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def is_closing_peer_reset(context: dict) -> bool:
    error = context.get("exception")
    if not isinstance(error, ConnectionResetError) or getattr(error, "winerror", None) != 10054:
        return False
    # Match the actual stdlib callback, not its message or a general socket error.
    from asyncio.proactor_events import _ProactorBasePipeTransport

    callback = getattr(context.get("handle"), "_callback", None)
    transport = getattr(callback, "__self__", None)
    return (
        isinstance(transport, _ProactorBasePipeTransport)
        and getattr(callback, "__func__", None) is _ProactorBasePipeTransport._call_connection_lost
        and not transport._called_connection_lost
        and transport._sock is not None
    )


def finish_reset_close(context: dict) -> None:
    # In Python 3.11/3.12, shutdown() raising skips the rest of the closing
    # callback. Finish that cleanup without calling protocol.connection_lost twice.
    transport = context["handle"]._callback.__self__
    if transport._sock is not None:
        transport._sock.close()
        transport._sock = None
    if transport._server is not None:
        transport._server._detach()
        transport._server = None
    transport._called_connection_lost = True


@contextmanager
def handle_windows_disconnects():
    """Keep real errors visible and leave asyncio's subprocess support intact."""
    if sys.platform != "win32":
        yield
        return
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(current_loop, context):
        if is_closing_peer_reset(context):
            try:
                finish_reset_close(context)
            except Exception as error:
                context = {**context, "exception": error,
                           "message": "Failed to finish Windows disconnected transport cleanup",
                           "original_exception": context["exception"]}
            else:
                logger.debug("Peer reset while closing a Windows transport (WinError 10054)")
                return
        if previous is not None:
            previous(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        # Do not overwrite a handler installed by another component meanwhile.
        if loop.get_exception_handler() is handler:
            loop.set_exception_handler(previous)
