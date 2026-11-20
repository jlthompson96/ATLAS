"""atlas-executor: runs approved write actions only (PLAN.md §2.9 rule 4, Phase 6).

* ``approval_record``: the signed approval record format (internal between atlas-core and the
  executor, version 1; see docs/adr/0001-privilege-model.md).
* ``verify``: every check the executor runs before acting (UW-3).
* ``service``: verify, claim the nonce, write-ahead audit, send, write-result audit.
* ``senders``: the only ``write``-tagged code in ATLAS. Phase 1 ships a fake sender only.
* ``server``: the ``/run/atlas/executor.sock`` handler (peer must be atlas-core).

Nothing in atlas-core imports ``senders``, ``service`` or ``server`` (UW-1, checked by tests).
"""
