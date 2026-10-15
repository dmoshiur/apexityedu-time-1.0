"""Build the right EMS client from configuration + live probing.

* ``iclock``/``rest`` — explicit admin choice.
* ``auto`` — probe once and use the protocol that answers; if nothing
  answers, select ``iclock`` when the path looks right… never. We stay
  honest: when no endpoint is detected the service runs in offline-queue
  mode (health checks keep failing until the admin configures the server
  side) instead of pretending uploads work.
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

    if chosen == "iclock":
        client = IclockEmsClient(
            config.ems_base_url, path=config.ems_iclock_path,
            timeout=config.ems_timeout, auth_token=token,
        )
    elif chosen == "rest":
        client = RestEmsClient(
            config.ems_base_url, timeout=config.ems_timeout,
            token_provider=(secret_store.get_ems_token if secret_store else None),
        )
    else:  # pragma: no cover - guarded by Config.validate
        raise ValueError(f"unknown EMS protocol: {chosen}")

    return client, (report.to_dict() if report else None)
