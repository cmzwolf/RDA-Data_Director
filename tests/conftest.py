"""Shared test identities and job identifiers.

Test subjects are declared as fixtures rather than module constants so that
every test states its dependencies in its own signature: a test that needs a
human says so, and a test that does not cannot silently reach for one.

On the identifiers used here: any well-formed ORCID may belong to a real
person, so only identifiers with a documented fictitious status are used by
name. These fixtures are never transmitted anywhere; the tests are offline.
"""

import pytest

from datadirector_contracts import Orcid


@pytest.fixture
def researcher() -> Orcid:
    """Josiah S. Carberry, the fictitious professor ORCID publishes for use in
    examples and testing. Not a real person.

    https://orcid.org/0000-0002-1825-0097
    """
    return Orcid(value="0000-0002-1825-0097")


@pytest.fixture
def data_steward() -> Orcid:
    """A second identity, needed where a test must distinguish two people.

    Synthetic: constructed to satisfy the MOD 11-2 checksum, with no claim that
    it is unassigned. It exists only to be different from `researcher` and is
    never sent to the ORCID API or to any other service.
    """
    return Orcid(value="0000-0001-2345-6789")


@pytest.fixture
def job_id() -> str:
    """A well-formed job identifier (ULID form)."""
    return "job-01JBQ7X9ABCDEFGHJKMNPQRSTV"
