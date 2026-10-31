"""Settings, secrets storage, and setup state management (Brief v12)."""

from __future__ import annotations

from jobhunt.settings.secrets import (
    SecretMaskingFilter,
    get_gemini_key,
    get_secret_info,
    install_secret_masking,
    mask_secret_text,
    set_gemini_key,
)
from jobhunt.settings.setup_state import (
    check_and_autodone_existing_install,
    get_setup_state,
    set_setup_state,
)
from jobhunt.settings.storage import (
    load_merged_settings,
    read_settings_file,
    write_settings_file,
)

__all__ = [
    "SecretMaskingFilter",
    "check_and_autodone_existing_install",
    "get_gemini_key",
    "get_secret_info",
    "get_setup_state",
    "install_secret_masking",
    "load_merged_settings",
    "mask_secret_text",
    "read_settings_file",
    "set_gemini_key",
    "set_setup_state",
    "write_settings_file",
]
