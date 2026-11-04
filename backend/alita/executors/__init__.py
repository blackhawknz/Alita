"""Executor implementations — pluggable backends for delegated work."""

from .base import Executor, ExecutorEvent
from .native import NativeExecutor

__all__ = ["Executor", "ExecutorEvent", "NativeExecutor"]
