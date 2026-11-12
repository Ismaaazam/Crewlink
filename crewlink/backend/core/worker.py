"""
Background worker (Rule 2, part 2).

The recipient row is the queue. A row is claimable while sent_at IS NULL and its lease
is empty or expired.

  claim    SELECT ... FOR UPDATE SKIP LOCKED a batch, set claimed_by + lease_until,
           attempts += 1, commit. Concurrent workers skip each other's rows.
  deliver  in ONE transaction: lock our rows, write push_log (unique on recipient_id),
           mark sent_at, clear the claim.
  recover  a worker that dies keeps its lease until it expires; after that any worker
           can claim the rows again. No broker, no heartbeat.

Push is simulated by a push_log row. The unique constraint on push_log.recipient_id is
what enforces "one successful push per recipient" in this demo.

KNOWN LIMITATION (real provider): the database cannot make delivery to an EXTERNAL push
provider exactly-once. If the provider accepts the request and the worker crashes before
this transaction commits, the lease expires and the push is sent again. The design
decision is to resend (a missed emergency callout is worse than a duplicate) and to
collapse duplicates on the device by using announcement_id as the notification
collapse ID, with the inbox row unique on (announcement, member). Strict exactly-once
would need provider-side idempotency keys, which must be verified before relying on it.
"""
import logging
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import AnnouncementRecipient

logger = logging.getLogger("crewlink.worker")

PUSH_LOG_SQL = """
INSERT INTO push_log (recipient_id, worker_id, pushed_at)
SELECT r, %s, now() FROM unnest(%s::bigint[]) AS r
ON CONFLICT (recipient_id) DO NOTHING
"""


def claim_batch(worker_id, batch_size=200, lease_seconds=60, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        ids = list(
            AnnouncementRecipient.objects.select_for_update(skip_locked=True)
            .filter(sent_at__isnull=True)
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        if ids:
            AnnouncementRecipient.objects.filter(id__in=ids).update(
                claimed_by=worker_id,
                lease_until=now + timedelta(seconds=lease_seconds),
                attempts=F("attempts") + 1,
            )
    return ids


def simulate_push(recipient_ids, worker_id):
    """Stand-in for the push provider. Returns how many pushes were newly recorded;
    ids that already have a push_log row are skipped by the unique constraint."""
    with connection.cursor() as cur:
        cur.execute(PUSH_LOG_SQL, [worker_id, list(recipient_ids)])
        recorded = cur.rowcount
    logger.info("worker=%s simulated push: %d new, %d already pushed",
                worker_id, recorded, len(recipient_ids) - recorded)
    return recorded


def process_batch(ids, worker_id, now=None):
    """Push + mark sent, atomically. Only rows still leased to this worker are touched,
    so a worker whose lease expired and was taken over cannot overwrite the new owner."""
    now = now or timezone.now()
    with transaction.atomic():
        own = list(
            AnnouncementRecipient.objects.select_for_update()
            .filter(id__in=ids, claimed_by=worker_id, sent_at__isnull=True)
            .values_list("id", flat=True)
        )
        if not own:
            return 0, 0
        recorded = simulate_push(own, worker_id)
        AnnouncementRecipient.objects.filter(id__in=own).update(
            sent_at=now, claimed_by=None, lease_until=None
        )
    return len(own), recorded


def process_available(worker_id, batch_size=200, lease_seconds=60):
    """Drain everything currently claimable. A batch that fails is logged and left
    leased; it becomes claimable again when the lease expires."""
    totals = {"claimed": 0, "delivered": 0, "pushes": 0, "failed_batches": 0}
    while True:
        ids = claim_batch(worker_id, batch_size, lease_seconds)
        if not ids:
            return totals
        totals["claimed"] += len(ids)
        try:
            delivered, pushes = process_batch(ids, worker_id)
        except Exception:
            logger.exception("worker=%s batch of %d failed; lease will expire", worker_id, len(ids))
            totals["failed_batches"] += 1
            continue
        totals["delivered"] += delivered
        totals["pushes"] += pushes
