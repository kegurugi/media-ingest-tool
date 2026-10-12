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
