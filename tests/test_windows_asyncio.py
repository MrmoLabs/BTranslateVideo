import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import windows_asyncio

transports = []


@pytest.fixture(autouse=True)
def release_test_transports():
    yield
    for transport in transports:
        transport._sock = None
    transports.clear()


def reset_error(number=10054):
    error = ConnectionResetError("peer reset")
    error.winerror = number
    return error


def closing_context(error=None):
    from asyncio.proactor_events import _ProactorBasePipeTransport

    transport = object.__new__(_ProactorBasePipeTransport)
    transport._sock = Mock()
    transport._server = None
    transport._called_connection_lost = False
    transports.append(transport)
    return {"exception": error or reset_error(), "handle": SimpleNamespace(_callback=transport._call_connection_lost)}


def test_only_known_closing_callback_is_recognized():
    assert windows_asyncio.is_closing_peer_reset(closing_context())
    assert not windows_asyncio.is_closing_peer_reset(closing_context(reset_error(10053)))
    assert not windows_asyncio.is_closing_peer_reset(closing_context(ValueError("task failed")))
    assert not windows_asyncio.is_closing_peer_reset({"exception": reset_error()})
    assert not windows_asyncio.is_closing_peer_reset({"exception": reset_error(), "handle": SimpleNamespace(_callback=lambda: None)})
    completed = closing_context()
    completed['handle']._callback.__self__._called_connection_lost = True
    assert not windows_asyncio.is_closing_peer_reset(completed)


@pytest.mark.parametrize("custom_handler", [False, True])
def test_unrelated_errors_are_delegated_and_previous_handler_restored(monkeypatch, custom_handler):
    async def check():
        loop = asyncio.get_running_loop()
        previous = Mock() if custom_handler else None
        default = Mock()
        loop.set_exception_handler(previous)
        monkeypatch.setattr(loop, "default_exception_handler", default)
        monkeypatch.setattr(windows_asyncio.sys, "platform", "win32")
        try:
            with windows_asyncio.handle_windows_disconnects():
                loop.call_exception_handler(closing_context())
                unexpected = {"exception": reset_error(), "message": "request processing failed"}
                loop.call_exception_handler(unexpected)
                ordinary = {"exception": RuntimeError("synthesis failed")}
                loop.call_exception_handler(ordinary)
                if custom_handler:
                    assert previous.call_args_list == [((loop, unexpected),), ((loop, ordinary),)]
                    default.assert_not_called()
                else:
                    assert default.call_args_list == [((unexpected,),), ((ordinary,),)]
            assert loop.get_exception_handler() is previous
        finally:
            loop.set_exception_handler(None)
    asyncio.run(check())


def test_restore_on_exception_and_preserve_later_handler(monkeypatch):
    async def check():
        monkeypatch.setattr(windows_asyncio.sys, "platform", "win32")
        loop = asyncio.get_running_loop()
        with pytest.raises(ValueError):
            with windows_asyncio.handle_windows_disconnects():
                raise ValueError("startup failure")
        assert loop.get_exception_handler() is None
        replacement = Mock()
        try:
            with windows_asyncio.handle_windows_disconnects():
                loop.set_exception_handler(replacement)
            assert loop.get_exception_handler() is replacement
        finally:
            loop.set_exception_handler(None)
    asyncio.run(check())


def test_other_platform_is_unchanged(monkeypatch):
    async def check():
        monkeypatch.setattr(windows_asyncio.sys, "platform", "linux")
        loop = asyncio.get_running_loop()
        with windows_asyncio.handle_windows_disconnects():
            assert loop.get_exception_handler() is None
    asyncio.run(check())


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows Proactor callback")
def test_real_event_loop_cleans_socket_after_peer_reset():
    from asyncio.proactor_events import _ProactorBasePipeTransport

    async def check():
        loop = asyncio.get_running_loop()
        previous = Mock()
        loop.set_exception_handler(previous)
        transport = object.__new__(_ProactorBasePipeTransport)
        sock = Mock()
        sock.shutdown.side_effect = reset_error()
        transport._sock = sock
        transport._protocol = Mock()
        transport._called_connection_lost = False
        transport._server = None
        try:
            with windows_asyncio.handle_windows_disconnects():
                loop.call_soon(transport._call_connection_lost, None)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                previous.assert_not_called()
                sock.close.assert_called_once()
                assert transport._sock is None
                assert transport._called_connection_lost
                transport._call_connection_lost(None)
                transport._protocol.connection_lost.assert_called_once_with(None)
            assert loop.get_exception_handler() is previous
        finally:
            transport._sock = None
            loop.set_exception_handler(None)
    asyncio.run(check())


def test_cleanup_failure_remains_visible(monkeypatch):
    async def check():
        monkeypatch.setattr(windows_asyncio.sys, "platform", "win32")
        loop = asyncio.get_running_loop()
        previous = Mock()
        loop.set_exception_handler(previous)
        context = closing_context()
        transport = context['handle']._callback.__self__
        transport._sock = Mock()
        error = OSError('close failed')
        transport._sock.close.side_effect = error
        try:
            with windows_asyncio.handle_windows_disconnects():
                loop.call_exception_handler(context)
            assert previous.call_count == 1
            reported = previous.call_args.args[1]
            assert reported['exception'] is error
            assert reported['original_exception'] is context['exception']
        finally:
            transport._sock = None
            loop.set_exception_handler(None)
    asyncio.run(check())
