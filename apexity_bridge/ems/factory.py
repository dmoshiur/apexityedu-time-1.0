"""Build the right EMS client from configuration + live probing.

* ``iclock``/``rest`` — explicit admin choice.
* ``auto`` — probe once and use the protocol that answers; if nothing
  answers, select ``iclock`` when the path looks right… never. We stay
  honest: when no endpoint is detected the service runs in offline-queue
  mode (health checks keep failing until the admin configures the server
  side) instead of pretending uploads work.

Split mode (new)
----------------
Attendance and the people directory do not have to travel over the same
protocol. iClock has no roster download at all, and a bridge that cannot see
who a PIN belongs to cannot help anybody — so when iClock is in use the
factory also builds a REST client and hands it to the iClock client purely as
a *roster side-channel* (``GET /api/v1/bridge/people``). Set
``EMS_ROSTER_MODE=off`` to disable it, or ``EMS_ROSTER_MODE=rest`` to force it
even when REST probing never succeeded.
"""
from __future__ import annotations

import logging

from .base import EmsClient
from .iclock import IclockEmsClient
from .probe import probe_ems
from .rest import RestEmsClient

log = logging.getLogger("apexity_bridge.ems.factory")


def build_ems_client(config, secret_store=None, *, protocol: str | None = None,
                     probe_cache: dict | None = None) -> tuple[EmsClient, dict | None]:
    """Returns (client, probe_report_or_None)."""
    chosen = (protocol or config.ems_protocol or "auto").lower()
    report = None

    if chosen == "auto":
        report = probe_ems(config.ems_base_url, iclock_path=config.ems_iclock_path,
                           timeout=config.ems_timeout)
        verdict = report.to_dict()
        log.info("EMS probe: %s", verdict)
        recommended = report.recommended_protocol
        if recommended:
            chosen = recommended
        else:
            log.warning(
                "Neither an iClock endpoint nor the REST bridge API responded at %s; "
                "defaulting to iClock (sync will stay queued until the EMS side exists).",
                config.ems_base_url,
            )
            chosen = "iclock"

    token = secret_store.get_ems_token() if secret_store else None

    roster_client = None
    roster_mode = str(getattr(config, "ems_roster_mode", "auto") or "auto").lower()
    if roster_mode not in ("off", "none", "0", "false"):
        roster_client = RestEmsClient(
            config.ems_base_url, timeout=config.ems_timeout,
            token_provider=(secret_store.get_ems_token if secret_store else None),
        )

    if chosen == "iclock":
        client = IclockEmsClient(
            config.ems_base_url, path=config.ems_iclock_path,
            timeout=config.ems_timeout, auth_token=token,
            roster_client=roster_client,
        )
    elif chosen == "rest":
        client = RestEmsClient(
            config.ems_base_url, timeout=config.ems_timeout,
            token_provider=(secret_store.get_ems_token if secret_store else None),
        )
    else:  # pragma: no cover - guarded by Config.validate
        raise ValueError(f"unknown EMS protocol: {chosen}")

    return client, (report.to_dict() if report else None)
