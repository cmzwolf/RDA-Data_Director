"""The exception hierarchy.

Failure modes are distinguished by type rather than by message text, because
the workflow engine decides whether to pause, halt or fail on the basis of what
went wrong, and matching on strings is not a decision procedure.
"""

from __future__ import annotations


class DataDirectorError(Exception):
    """Root of the hierarchy. Every subclass names what to do next in its
    message, per Blueprint C7's graceful-failure clause."""


class ConfigurationError(DataDirectorError):
    """An invalid or incomplete configuration. Raised only at startup.

    Deliberately fatal: a configuration defect surfacing three steps into a
    workflow is the failure mode the loader exists to prevent.
    """


class ChainIntegrityError(DataDirectorError):
    """The event log has been altered, reordered or truncated.

    Never recoverable and never to be caught broadly. It means the audit record
    is untrustworthy, which is a condition an operator must be told about
    rather than one the software should work around.
    """


class AuthorityError(DataDirectorError):
    """An unconfirmed assertion was acted on, or a human act lacked a human."""


class PluginError(DataDirectorError):
    """A plugin failed to load or manifest validation, or does not satisfy the
    protocol it claims."""


class CredentialError(DataDirectorError):
    """A required credential is absent or unusable.

    Messages name the environment variable, never its value.
    """


class ExternalServiceError(DataDirectorError):
    """A remote dependency failed or was unavailable.

    Always recoverable: principle P13 requires the workflow to pause and resume
    rather than fail, so the engine treats this as a halt with a resume path.
    """


class ExtractionError(DataDirectorError):
    """An archive was refused: path escape, symlink, or a limit exceeded.

    Not recoverable by retry. The container is rejected and the human told why.
    """
