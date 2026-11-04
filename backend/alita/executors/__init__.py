"""Executor implementations — pluggable backends for delegated work."""

from .base import Executor, ExecutorEvent

__all__ = ["Executor", "ExecutorEvent", "NativeExecutor", "OpenCodeExecutor"]


def __getattr__(name: str):
    # Imported lazily so ``alita.executors.report`` does not pull in the
    # sub-agent module (which itself imports the report parser).
    if name == "NativeExecutor":
        from .native import NativeExecutor

        return NativeExecutor
    if name == "OpenCodeExecutor":
        from .opencode import OpenCodeExecutor

        return OpenCodeExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
