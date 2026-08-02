"""Wiki authorization. Owned by the wiki service, not by the mesh.

The rule the feature actually needs: a weekly report from a division only
updates pages in that division's namespace, and a reader sees their own
namespace plus anything published as shared. Nobody outside this module should
know that rule, and in particular the copilot should not — otherwise every new
division becomes a change in two services.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..protocol import Denied, Principal

log = logging.getLogger(__name__)

__all__ = ["WikiAuthorizer", "SHARED_NAMESPACE"]

SHARED_NAMESPACE = "shared"


@dataclass
class WikiAuthorizer:
    """Decides what a principal may read and write in the wiki.

    In production ``verify`` would validate a signed token against the identity
    provider. Here it accepts a pre-decoded principal and re-derives the
    namespace grant from the division claim, which is the part that matters
    architecturally: the *callee* computes the grant. A caller that hands us
    ``{"namespaces": ["finance"]}`` gets ignored.
    """

    #: Divisions whose pages every authenticated user may read.
    public_namespaces: frozenset[str] = frozenset({SHARED_NAMESPACE})

    def readable_namespaces(self, principal: Principal) -> frozenset[str]:
        if principal.has_role("wiki.admin"):
            return frozenset({"*"})
        return frozenset({principal.division}) | self.public_namespaces

    def writable_namespaces(self, principal: Principal) -> frozenset[str]:
        if principal.has_role("wiki.admin"):
            return frozenset({"*"})
        if not principal.has_role("wiki.writer"):
            return frozenset()
        # Note the asymmetry: a writer writes only into their own namespace.
        # They can read `shared` but they cannot publish into it.
        return frozenset({principal.division})

    def check_read(self, principal: Principal, namespace: str) -> None:
        allowed = self.readable_namespaces(principal)
        if "*" in allowed or namespace in allowed:
            return
        raise Denied(
            f"{principal.subject} may not read wiki namespace {namespace!r}",
            subject=principal.subject,
        )

    def check_write(self, principal: Principal, namespace: str) -> None:
        allowed = self.writable_namespaces(principal)
        if "*" in allowed or namespace in allowed:
            return
        if not principal.has_role("wiki.writer"):
            raise Denied(
                f"{principal.subject} has no wiki write role", subject=principal.subject
            )
        raise Denied(
            f"{principal.subject} may only write to namespace {principal.division!r}, "
            f"not {namespace!r}",
            subject=principal.subject,
        )

    def filter_readable(self, principal: Principal, namespaces: list[str]) -> list[str]:
        allowed = self.readable_namespaces(principal)
        if "*" in allowed:
            return namespaces
        return [ns for ns in namespaces if ns in allowed]
