"""Composition roots for embedded JSON-RPC and the stdio backend process."""

from contextlib import redirect_stdout
from dataclasses import dataclass
import os
import sys

from reuleauxcoder.app.commands.capabilities import UIProfile, UICapability
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.rpc.client import RuntimeClient
from reuleauxcoder.app.rpc.server import RuntimeServer
from reuleauxcoder.extensions.command.builtin import (
    create_builtin_command_panel_registry,
)
from reuleauxcoder.infrastructure.rpc.peer import RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import MemoryTransport, StreamTransport


def create_server(ctx, peer, profile):
    commands = CommandService(
        ctx.agent,
        ctx.config,
        ctx.ui_bus,
        profile,
        ctx.action_registry,
        sessions_dir=ctx.sessions_dir,
        session_exit_time=ctx.session_exit_time,
        skills_service=ctx.skills_service,
        panels=create_builtin_command_panel_registry(),
    )
    server = RuntimeServer(
        commands,
        peer,
        host_mode=ctx.config.remote_exec.enabled and ctx.config.remote_exec.host_mode,
    )
    ctx.ui_interactor = server.interactions
    return server


@dataclass
class LocalConnection:
    client: RuntimeClient
    server: RuntimeServer

    def close(self):
        try:
            if not self.client.peer.closed.is_set():
                self.client.shutdown()
        finally:
            self.client.close()
            self.server.peer.close()
            # The composition root also owns cleanup after connection loss.
            self.server.shutdown()


def connect_local(
    ctx, profile, ui_bus, interactor, *, foreground_interactions=False, activate=True
):
    left, right = MemoryTransport.pair()
    frontend, backend = RpcPeer(left), RpcPeer(right)
    client = RuntimeClient(
        frontend, ui_bus, interactor, foreground_interactions=foreground_interactions
    )
    server = create_server(ctx, backend, profile)
    backend.start()
    frontend.start()
    try:
        client.initialize(profile, activate=activate)
    except BaseException:
        client.close()
        backend.close()
        raise
    return LocalConnection(client, server)


def run_stdio(options):
    from reuleauxcoder.interfaces.entrypoint.runner import AppRunner

    reader = sys.stdin.buffer
    writer = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    with redirect_stdout(sys.stderr):
        runner = AppRunner(options)
        server = None
        peer = RpcPeer(StreamTransport(reader, writer))
        try:
            ctx = runner.initialize()
            server = create_server(
                ctx, peer, UIProfile("cli", "RPC", frozenset(UICapability))
            )
            peer.start()
            peer.closed.wait()
        finally:
            try:
                if server is not None:
                    server.shutdown()
            finally:
                peer.close()
                peer.wait_closed(timeout=10)
                runner.cleanup()
