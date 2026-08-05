"""Wiki authorization. Owned by the wiki service, not by the mesh.

Three rules the feature actually needs:

1. A weekly report from a division only updates pages in that division's
   namespace, and a reader sees their own namespace plus anything shared.
2. Some namespaces are clearance-gated. The executive roll-up lives in one, and
   ordinary users must not reach it through the same search tool they use for
   their own division.
3. **Aggregation does not launder clearance.** A report distilled from several
   divisions must not be published somewhere with a wider readership than its
   sources. This is the rule that scheduled cross-division reports make load
   bearing, and it is the one most likely to be missed, because every
   individual read and the write all pass on their own.

Nobody outside this module should know any of it — in particular the agent
should not, or every new division becomes a change in two services.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..protocol import Denied, Principal

log = logging.getLogger(__name__)

__all__ = ["WikiAuthorizer", "SHARED_NAMESPACE", "EXEC_NAMESPACE"]

SHARED_NAMESPACE = "shared"
EXEC_NAMESPACE = "exec"


def _default_clearance() -> dict[str, frozenset[str]]:
    # namespace -> roles required to read it. Absent means "no extra clearance".
    return {EXEC_NAMESPACE: frozenset({"wiki.exec"})}


@dataclass
class WikiAuthorizer:
    """Decides what a principal may read, write, and aggregate.

    In production ``verify`` would validate a signed token against the identity
    provider. Here it takes a pre-decoded principal and re-derives the grant
    from its claims, which is the part that matters architecturally: the
    *callee* computes the grant. A caller that passes
    ``{"namespaces": ["finance"]}`` is ignored.
    """

    public_namespaces: frozenset[str] = frozenset({SHARED_NAMESPACE})
    """Readable by every authenticated user."""

    clearance: dict[str, frozenset[str]] = field(default_factory=_default_clearance)
    """Namespaces gated behind a role, e.g. the executive roll-up."""

    # -- reads --------------------------------------------------------------

    def readable_namespaces(self, principal: Principal) -> frozenset[str]:
        if principal.has_role("wiki.admin"):
            return frozenset({"*"})
        allowed = {principal.division} | set(self.public_namespaces)
        # Cross-division read for service accounts running scheduled sweeps.
        if principal.has_role("wiki.reader.all"):
            allowed.add("*")
        for namespace, roles in self.clearance.items():
            if roles <= principal.roles:
                allowed.add(namespace)
        # A gated namespace is never granted just by being your division.
        return frozenset(
            ns for ns in allowed if ns == "*" or self._clearance_ok(principal, ns)
        )

    def _clearance_ok(self, principal: Principal, namespace: str) -> bool:
        required = self.clearance.get(namespace, frozenset())
        return required <= principal.roles

    def check_read(self, principal: Principal, namespace: str) -> None:
        allowed = self.readable_namespaces(principal)
        if "*" in allowed and self._clearance_ok(principal, namespace):
            return
        if namespace in allowed:
            return
        raise Denied(
            f"{principal.subject} may not read wiki namespace {namespace!r}",
            subject=principal.subject,
        )

    def filter_readable(self, principal: Principal, namespaces: list[str]) -> list[str]:
        allowed = self.readable_namespaces(principal)
        if "*" in allowed:
            return [ns for ns in namespaces if self._clearance_ok(principal, ns)]
        return [ns for ns in namespaces if ns in allowed]

    # -- writes -------------------------------------------------------------

    def writable_namespaces(self, principal: Principal) -> frozenset[str]:
        if principal.has_role("wiki.admin"):
            return frozenset({"*"})
        if not principal.has_role("wiki.writer"):
            return frozenset()
        allowed = {principal.division}
        # The scheduled report writer publishes into the executive namespace
        # without thereby gaining the right to write anywhere else.
        if principal.has_role("wiki.writer.exec"):
            allowed.add(EXEC_NAMESPACE)
        return frozenset(allowed)

    def check_write(self, principal: Principal, namespace: str) -> None:
        allowed = self.writable_namespaces(principal)
        if "*" in allowed or namespace in allowed:
            return
        if not principal.has_role("wiki.writer"):
            raise Denied(
                f"{principal.subject} has no wiki write role", subject=principal.subject
            )
        raise Denied(
            f"{principal.subject} may write to {sorted(allowed)}, not {namespace!r}",
            subject=principal.subject,
        )

    # -- aggregation --------------------------------------------------------

    def check_aggregation(self, target: str, sources: set[str]) -> None:
        """Refuse to publish into a namespace less protected than its sources.

        The failure this prevents: a scheduled sweep reads `supply`, `finance`
        and `platform` under a service account, distils them into one page, and
        writes it to a namespace anyone can read. Every individual step was
        authorized; the combination is a leak. Clearance has to be carried by
        the *derived* document, not just checked on the way in.
        """
        target_roles = self.clearance.get(target, frozenset())
        for source in sources:
            required = self.clearance.get(source, frozenset())
            if not required <= target_roles:
                raise Denied(
                    f"cannot publish into {target!r}: it derives from {source!r}, "
                    f"which requires {sorted(required)} to read, and {target!r} "
                    f"requires only {sorted(target_roles)}"
                )
        # Breadth is the other half. A page distilled from more than one
        # division is a cross-division artefact and belongs somewhere gated,
        # even when no single source was itself gated.
        divisions = {s for s in sources if s not in self.public_namespaces}
        if len(divisions) > 1 and not target_roles:
            raise Denied(
                f"cannot publish into {target!r}: it aggregates "
                f"{sorted(divisions)} and must go to a clearance-gated namespace "
                f"(e.g. {EXEC_NAMESPACE!r}), not one every user can read"
            )
