#!/usr/bin/env python3
"""Shared CLI introspection for the documentation/contract gate scripts.

Every gate that compares docs or API contracts against the real command surface
needs the set of registered subcommands. argparse only exposes them through
private attributes whose shape varies across Python versions (``_subparsers``
may be ``None``, or resolve to an ``_ArgumentGroup`` rather than the
``_SubParsersAction``), so the access lives here once instead of being copied
into each script — three copy-pasted variants had already drifted apart.
"""
from __future__ import annotations

import argparse


def registered_subcommands(parser: argparse.ArgumentParser) -> set[str]:
    """Top-level subcommand names registered on *parser*."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()
