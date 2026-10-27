"""The interface a crudkit model implements to become signable.

Required:

    def get_signature_pdf(self) -> bytes
    def get_signers(self) -> list[SignerSpec]

Optional:

    def get_signature_fields(self) -> list[FieldSpec]   # skip anchor detection
    def get_signature_title(self) -> str
    def get_signature_message(self) -> str
    def get_signature_expiry_days(self) -> int
    def on_signature_sent(self, request)
    def on_signature_viewed(self, request, signer)
    def on_signature_signed(self, request, signer)
    def on_signature_completed(self, request, signed_file)
    def on_signature_declined(self, request, signer, reason)
    def on_signature_voided(self, request, by_user, reason)
    def on_signature_expired(self, request)
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SignatureError(Exception):
    """A request could not be created or advanced. The message is safe to show to staff."""


# Field kinds. Signature and initials take the signer's image; date and name are
# filled by the server; text and checkbox are filled by the signer.
SIGNATURE = "Signature"
INITIALS = "Initials"
DATE = "Date"
NAME = "Name"
TEXT = "Text"
CHECKBOX = "Checkbox"
FIELD_KINDS = (SIGNATURE, INITIALS, DATE, NAME, TEXT, CHECKBOX)
IMAGE_KINDS = frozenset({SIGNATURE, INITIALS})
SERVER_FILLED_KINDS = frozenset({DATE, NAME})


@dataclass(frozen=True)
class SignerSpec:
    role: str
    name: str
    email: str
    order: int = 1
    user_id: int | None = None
    # Shown only to this signer, in their invitation email.
    note: str = ""


@dataclass(frozen=True)
class FieldSpec:
    role: str
    kind: str
    page: int
    x: float
    y: float
    width: float
    height: float
    required: bool = True
    name: str = ""
    anchor: str = ""
    # Checkboxes sharing a group are a pick-one choice; required means one must be picked.
    group: str = ""


@runtime_checkable
class Signable(Protocol):
    def get_signature_pdf(self) -> bytes: ...

    def get_signers(self) -> list[SignerSpec]: ...


def call_hook(obj, name, *args):
    hook = getattr(obj, name, None)
    if callable(hook):
        return hook(*args)
    return None
