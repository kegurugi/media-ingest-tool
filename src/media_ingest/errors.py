"""Exception types. Every error the tool raises on purpose is an IngestError."""


class IngestError(Exception):
    """Base class for expected, operator-facing errors."""


class ConfigError(IngestError):
    """project.json is missing, unreadable or invalid."""


class NamingError(IngestError):
    """A card name, date folder or reel number is invalid."""


class ReelMismatchError(IngestError):
    """Destinations disagree about which reels already exist."""


class DestinationExistsError(IngestError):
    """A card folder already exists on a destination. Never overwrite."""


class SourceError(IngestError):
    """The source card can't be read safely (unreadable file, symlink, changed during copy)."""


class InsufficientSpaceError(IngestError):
    """A destination doesn't have room for the card. Nothing was copied."""


class CopyError(IngestError):
    """Reading or writing failed during the copy."""


class IncompleteMismatchError(IngestError):
    """Destinations disagree about which cards are incomplete."""
