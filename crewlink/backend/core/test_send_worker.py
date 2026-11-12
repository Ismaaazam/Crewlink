"""
Rule 2 tests. Real PostgreSQL, real threads, real transactions (TransactionTestCase),
because the guarantees under test are row locks and constraints.
"""
import threading
from datetime import timedelta

from django.db import IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import worker
from .models import (
    Account, Announcement, AnnouncementRecipient, Local, Member, PushLog,
)


def run_concurrently(fn, n):
    """Run fn(i) in n threads released at the same instant; return results in order."""
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def target(i):
        try:
            barrier.wait(timeout=15)
            results[i] = fn(i)
        except Exception as exc:  # surfaced below so a thread failure fails the test
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return results


class Base(TransactionTestCase):
    def setUp(self):
        self.local = Local.objects.create(name="Local A")
        self.other_local = Local.objects.create(name="Local B")
        self.leader = self.make_account(self.local, "leader.a", "leader", "Business Manager")
        self.member_acct = self.make_account(self.local, "member.a", "member", "Journeyman")
        self.other_leader = self.make_account(self.other_local, "leader.b", "leader", "Business Manager")
        # eligible in Local A (the two logins above are active members too):
        # 28 Journeyman + 10 Apprentice + 2 logins (Business Manager, Journeyman)
        self.bulk_members(self.local, "J", 28, "Journeyman")
        self.bulk_members(self.local, "A", 10, "Apprentice")
        # NOT eligible: inactive members, and everyone in another local
        self.bulk_members(self.local, "R", 5, "Journeyman", status="retired")
        self.bulk_members(self.local, "S", 3, "Journeyman", status="suspended")
        self.bulk_members(self.other_local, "B", 20, "Journeyman")

    def make_account(self, local, username, role, classification):
        member = Member.objects.create(
            local=local, full_name=username, email=f"{username}@x.test", classification=classification
        )
        return Account.objects.create_user(username, "pw-not-used", member, role)

    def bulk_members(self, local, prefix, n, classification, status="active"):
        Member.objects.bulk_create([
            Member(local=local, full_name=f"{prefix}{i}", email=f"{prefix}{i}@{local.name[-1]}.test",
                   classification=classification, status=status)
            for i in range(n)
        ])

    def announcement(self, local=None, audience=(), status="approved"):
        local = local or self.local
        now = timezone.now()
        return Announcement.objects.create(
            local=local, title="Emergency meeting", body="Hall at 17:00", status=status,
            approved_at=None if status == "draft" else now,
            audience_classifications=list(audience),
            created_by=self.leader if local == self.local else self.other_leader,
        )

    def api_client(self, account):
        c = APIClient()
        c.force_authenticate(user=account)
        return c

    def send(self, account, ann):
        return self.api_client(account).post(f"/api/announcements/{ann.id}/send/")

    def expected_eligible(self):
        return Member.objects.filter(local=self.local, status="active").count()  # 40


class SendEndpointTests(Base):
    def test_eligible_count_fixture(self):
        self.assertEqual(self.expected_eligible(), 40)

    def test_first_send_wins_and_creates_one_recipient_per_eligible_member(self):
        ann = self.announcement()
        r = self.send(self.leader, ann)
        self.assertEqual(r.status_code, 202)
        self.assertFalse(r.data["already_sent"])
        self.assertEqual(r.data["recipients_created"], 40)
        ann.refresh_from_db()
        self.assertEqual(ann.status, "sent")
        self.assertIsNotNone(ann.sent_at)
        self.assertEqual(AnnouncementRecipient.objects.filter(announcement=ann).count(), 40)
        # every recipient belongs to the announcement's local
        self.assertFalse(AnnouncementRecipient.objects.filter(announcement=ann).exclude(local=self.local).exists())

    def test_send_twice_only_one_wins(self):
        ann = self.announcement()
        first, second = self.send(self.leader, ann), self.send(self.leader, ann)
        self.assertEqual((first.status_code, second.status_code), (202, 200))
        self.assertTrue(second.data["already_sent"])
        self.assertEqual(second.data["recipients_created"], 0)
        self.assertEqual(AnnouncementRecipient.objects.filter(announcement=ann).count(), 40)
        ann.refresh_from_db()
        sent_at = ann.sent_at
        self.send(self.leader, ann)
        ann.refresh_from_db()
        self.assertEqual(ann.sent_at, sent_at)  # a retry never rewrites sent_at

    def test_concurrent_sends_exactly_one_winner(self):
        ann = self.announcement()
        n = 8
        responses = run_concurrently(lambda i: self.send(self.leader, ann), n)
        codes = sorted(r.status_code for r in responses)
        self.assertEqual(codes, [200] * (n - 1) + [202])
        self.assertEqual(sum(r.data["recipients_created"] for r in responses), 40)
        # losers answer after the winner committed, so they already see all recipients
        self.assertTrue(all(r.data["counts"]["recipients"] == 40 for r in responses))
        self.assertEqual(AnnouncementRecipient.objects.filter(announcement=ann).count(), 40)

    def test_concurrent_sends_across_many_announcements_are_independent(self):
        anns = [self.announcement() for _ in range(3)]
        run_concurrently(lambda i: self.send(self.leader, anns[i % 3]), 9)
        for ann in anns:
            self.assertEqual(AnnouncementRecipient.objects.filter(announcement=ann).count(), 40)

    def test_unique_constraint_blocks_duplicate_recipient_rows(self):
        ann = self.announcement()
        self.send(self.leader, ann)
        row = AnnouncementRecipient.objects.filter(announcement=ann).first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnouncementRecipient.objects.create(
                announcement=ann, member=row.member, local=self.local
            )

    def test_audience_filter(self):
        ann = self.announcement(audience=["Apprentice"])
        r = self.send(self.leader, ann)
        self.assertEqual(r.data["recipients_created"], 10)
        classes = set(
            AnnouncementRecipient.objects.filter(announcement=ann)
            .values_list("member__classification", flat=True)
        )
        self.assertEqual(classes, {"Apprentice"})

    def test_draft_cannot_be_sent(self):
        ann = self.announcement(status="draft")
        r = self.send(self.leader, ann)
        self.assertEqual(r.status_code, 409)
        ann.refresh_from_db()
        self.assertEqual(ann.status, "draft")
        self.assertIsNone(ann.sent_at)
        self.assertFalse(AnnouncementRecipient.objects.filter(announcement=ann).exists())

    def test_other_local_leader_gets_404_and_nothing_happens(self):
        ann = self.announcement()
        r = self.send(self.other_leader, ann)
        self.assertEqual(r.status_code, 404)
        ann.refresh_from_db()
        self.assertEqual(ann.status, "approved")
        self.assertFalse(AnnouncementRecipient.objects.filter(announcement=ann).exists())

    def test_member_gets_403_and_anonymous_gets_401(self):
        ann = self.announcement()
        self.assertEqual(self.send(self.member_acct, ann).status_code, 403)
        self.assertEqual(APIClient().post(f"/api/announcements/{ann.id}/send/").status_code, 401)
        self.assertFalse(AnnouncementRecipient.objects.filter(announcement=ann).exists())


class WorkerTests(Base):
    def sent_announcement(self):
        ann = self.announcement()
        self.send(self.leader, ann)
        return ann

    def assert_one_push_each(self, ann, n=40):
        self.assertEqual(AnnouncementRecipient.objects.filter(announcement=ann).count(), n)
        self.assertEqual(PushLog.objects.filter(recipient__announcement=ann).count(), n)
        self.assertFalse(AnnouncementRecipient.objects.filter(announcement=ann, sent_at__isnull=True).exists())

    def test_worker_pushes_everyone_once_even_when_run_repeatedly(self):
        ann = self.sent_announcement()
        t1 = worker.process_available("w1", batch_size=7)
        t2 = worker.process_available("w1", batch_size=7)
        t3 = worker.process_available("w2", batch_size=7)
        self.assertEqual((t1["delivered"], t1["pushes"]), (40, 40))
        self.assertEqual((t2["claimed"], t3["claimed"]), (0, 0))
        self.assert_one_push_each(ann)
        self.assertFalse(AnnouncementRecipient.objects.filter(attempts__gt=1).exists())
        # claim cleared after delivery
        self.assertFalse(AnnouncementRecipient.objects.filter(claimed_by__isnull=False).exists())

    def test_worker_command_once_twice(self):
        from django.core.management import call_command
        ann = self.sent_announcement()
        call_command("run_worker", "--once", "--worker-id", "cmd-1", verbosity=0)
        call_command("run_worker", "--once", "--worker-id", "cmd-2", verbosity=0)
        self.assert_one_push_each(ann)

    def test_concurrent_workers_never_double_push(self):
        ann = self.sent_announcement()
        totals = run_concurrently(lambda i: worker.process_available(f"w{i}", batch_size=5), 4)
        self.assertEqual(sum(t["delivered"] for t in totals), 40)
        self.assertEqual(sum(t["pushes"] for t in totals), 40)
        self.assert_one_push_each(ann)
        self.assertFalse(AnnouncementRecipient.objects.filter(attempts__gt=1).exists())

    def test_live_lease_is_not_stolen(self):
        ann = self.sent_announcement()
        ids = worker.claim_batch("slow", batch_size=100, lease_seconds=60)
        self.assertEqual(len(ids), 40)
        self.assertEqual(worker.process_available("other")["claimed"], 0)  # lease still valid
        self.assertEqual(PushLog.objects.count(), 0)

    def test_expired_lease_is_recovered_after_worker_dies(self):
        ann = self.sent_announcement()
        dead_ids = worker.claim_batch("dead-worker", batch_size=100, lease_seconds=60)
        self.assertEqual(len(dead_ids), 40)  # claimed, then the worker "crashes"
        AnnouncementRecipient.objects.filter(announcement=ann).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        totals = worker.process_available("rescuer")
        self.assertEqual((totals["claimed"], totals["delivered"], totals["pushes"]), (40, 40, 40))
        self.assert_one_push_each(ann)
        # claimed twice (dead worker, rescuer), pushed once
        self.assertEqual(
            set(AnnouncementRecipient.objects.filter(announcement=ann).values_list("attempts", flat=True)), {2}
        )
        self.assertEqual(set(PushLog.objects.values_list("worker_id", flat=True)), {"rescuer"})

    def test_crash_during_delivery_rolls_back_and_recovers(self):
        ann = self.sent_announcement()
        original = worker.simulate_push

        def boom(ids, worker_id):
            original(ids, worker_id)  # push_log rows written inside the transaction...
            raise RuntimeError("crash before commit")  # ...then the worker dies

        worker.simulate_push = boom
        try:
            totals = worker.process_available("crashy", batch_size=100)
        finally:
            worker.simulate_push = original
        self.assertEqual(totals["failed_batches"], 1)
        self.assertEqual(PushLog.objects.count(), 0)  # rolled back, nothing half-recorded
        self.assertEqual(AnnouncementRecipient.objects.filter(announcement=ann, sent_at__isnull=True).count(), 40)
        AnnouncementRecipient.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        worker.process_available("rescuer", batch_size=100)
        self.assert_one_push_each(ann)

    def test_late_worker_cannot_overwrite_new_owner(self):
        ann = self.sent_announcement()
        slow_ids = worker.claim_batch("slow", batch_size=100)
        AnnouncementRecipient.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        fast_ids = worker.claim_batch("fast", batch_size=100)  # takes over, has not finished yet
        self.assertEqual(sorted(fast_ids), sorted(slow_ids))
        # the slow worker wakes up while "fast" still owns the rows: it must do nothing
        self.assertEqual(worker.process_batch(slow_ids, "slow"), (0, 0))
        self.assertEqual(PushLog.objects.count(), 0)
        self.assertEqual(worker.process_batch(fast_ids, "fast"), (40, 40))
        self.assert_one_push_each(ann)
        self.assertEqual(set(PushLog.objects.values_list("worker_id", flat=True)), {"fast"})

    def test_push_log_unique_constraint(self):
        self.sent_announcement()
        worker.process_available("w1")
        rec = AnnouncementRecipient.objects.first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            PushLog.objects.create(recipient=rec, worker_id="duplicate")
        # the raw insert path the worker uses is a silent no-op instead
        self.assertEqual(worker.simulate_push([rec.id], "dup"), 0)
        self.assertEqual(PushLog.objects.filter(recipient=rec).count(), 1)
