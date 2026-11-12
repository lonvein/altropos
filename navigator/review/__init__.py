"""Local extraction review. Approval never indexes or publishes a document.

Only call record_decision after an explicit human decision. Files are integrity
checked, not cryptographically signed: the local runtime directory is trusted.
"""
from .workflow import (
    ReviewError, load_approved_package, prepare_document, record_decision,
    show_package, write_index,
)

__all__ = ["ReviewError", "prepare_document", "record_decision",
           "load_approved_package", "show_package", "write_index"]
