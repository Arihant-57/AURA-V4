"""AURA Configuration Module

Provides configuration management and workspace resolution for AURA.
"""
from .workspace import WorkspaceConfig, resolve_workspace, validate_workspace_path, workspace_from_argv
from .settings import Settings, get_settings

__all__ = [
    "WorkspaceConfig",
    "resolve_workspace",
    "validate_workspace_path",
    "workspace_from_argv",
    "Settings",
    "get_settings",
]