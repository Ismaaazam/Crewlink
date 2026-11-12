"""
Idempotent send (Rule 2, part 1).

The winner is decided by ONE conditional UPDATE. Under READ COMMITTED a second
concurrent UPDATE on the same row blocks until the first transaction commits, then
re-evaluates its WHERE clause, finds status != 'approved', and matches 0 rows.
So exactly one request ever gets row count 1, on any backend instance, and only that
request inserts recipients. A loser also returns after the winner has committed, so
it always sees the complete recipient set.
"""
from django.db import connection, transaction
from django.utils import timezone

from .models import Announcement

# Audience is read from the announcement row itself, inside the same statement.
# Empty audience_classifications means the whole local. Only active members qualify.
# local_id comes from the announcement, so a recipient can never point at another local.
RECIPIENTS_SQL = """
INSERT INTO announcement_recipients (announcement_id, member_id, local_id, attempts)
SELECT a.id, m.id, a.local_id, 0
FROM announcements a
JOIN members m ON m.local_id = a.local_id
WHERE a.id = %s
  AND m.status = 'active'
  AND (cardinality(a.audience_classifications) = 0
       OR m.classification = ANY (a.audience_classifications))
ON CONFLICT (announcement_id, member_id) DO NOTHING
"""


def send_announcement(announcement):
    """Returns (won, recipients_created). Only the winner creates recipients."""
    with transaction.atomic():
        won = Announcement.objects.filter(
            pk=announcement.pk,
            local_id=announcement.local_id,
            status=Announcement.Status.APPROVED,
            sent_at__isnull=True,
        ).update(status=Announcement.Status.SENT, sent_at=timezone.now())
        if won != 1:
            return False, 0
        with connection.cursor() as cur:
            cur.execute(RECIPIENTS_SQL, [announcement.pk])
            created = cur.rowcount
    return True, created
