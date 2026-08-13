"""The two ways a report run gets its authority.

Scheduled runs and user-triggered runs are the same agent with different
principals, and that difference is the whole security model:

* **Scheduled** — a service account with cross-division read and the right to
  publish into the executive namespace. The output is a roll-up nobody below
  that clearance should see.
* **On demand** — the requesting user's own principal. They see what they can
  see, and the report they get back is bounded by that.

The important consequence is that the *same prompt* produces different reports
for different callers, and that is correct rather than a bug to paper over. A
user asking "what's our supply exposure" should get an answer built from their
division's pages; they should not get the executive roll-up because it happens
to be a better answer.
"""

from __future__ import annotations

from skr_agent.protocol import Principal
from skr_agent.wiki.authz import EXEC_NAMESPACE

__all__ = ["service_principal", "user_principal", "EXEC_NAMESPACE"]


def service_principal(
    *,
    name: str = "scheduler",
    division: str = EXEC_NAMESPACE,
    token: str | None = None,
) -> Principal:
    """Authority for the weekly scheduled sweep.

    Reads across divisions, writes only into the executive namespace. It
    deliberately does *not* carry ``wiki.admin`` — write access stays narrow
    even though read access is broad, so a prompt injection in a weekly report
    cannot turn the scheduler into an arbitrary-write credential.
    """
    return Principal(
        subject=f"svc:{name}",
        division=division,
        roles=frozenset(
            {
                "wiki.reader",
                "wiki.reader.all",   # cross-division read
                "wiki.exec",         # clearance for the exec namespace
                "wiki.writer",
                "wiki.writer.exec",  # may publish the roll-up
            }
        ),
        attributes={"actor_type": "service"},
        token=token,
    )


def user_principal(
    subject: str,
    division: str,
    *,
    roles: frozenset[str] | set[str] | None = None,
    token: str | None = None,
) -> Principal:
    """Authority for an on-demand run triggered from the dashboard.

    Defaults to read-only. A user who may publish carries ``wiki.writer``, and
    then writes land in their own division namespace — never in the executive
    one, which needs ``wiki.writer.exec``.
    """
    return Principal(
        subject=subject,
        division=division,
        roles=frozenset(roles) if roles else frozenset({"wiki.reader"}),
        attributes={"actor_type": "user"},
        token=token,
    )
