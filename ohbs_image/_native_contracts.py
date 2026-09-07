"""Compatibility aliases for the native engine contracts."""
from .native.contracts import CommunicatorOps, ProviderOps

NativeProviderOps = ProviderOps
NativeCommunicatorOps = CommunicatorOps

__all__ = ["NativeCommunicatorOps", "NativeProviderOps"]
