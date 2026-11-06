"""Connectors: any source to any target (docs/connectors.md). Importing this package registers the built-in kinds;
a new connector is one module calling `registry.register` and one line here."""
from . import dav, imap, pst, registry  # noqa: F401 - imported for their registration
from .base import ConnectorError  # noqa: F401
