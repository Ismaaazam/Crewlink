"""Phase 5 tests: member inbox, read, acknowledge. Real PostgreSQL."""
from django.utils import timezone

from . import test_send_worker as sw
from .models import Announcement, AnnouncementRecipient


class InboxBase(sw.Base):
    def setUp(self):
        super().setUp()
        self.apprentice = self.make_account(self.local, "apprentice.a", "member", "Apprentice")
        self.member_b = self.make_account(self.other_local, "member.b", "member", "Journeyman")

    def sent(self, audience=(), needs_ack=True, local=None, title="Emergency meeting"):
        ann = self.announcement(local=local, audience=audience)
        ann.needs_ack, ann.title = needs_ack, title
        ann.save()
        sender = self.leader if ann.local_id == self.local.id else self.other_leader
        self.assertEqual(self.send(sender, ann).status_code, 202)
        return ann

    def row(self, ann, account):
        return AnnouncementRecipient.objects.get(announcement=ann, member=account.member)

    def get_inbox(self, account):
        r = self.api_client(account).get("/api/me/inbox/")
        self.assertEqual(r.status_code, 200)
        return r.data["results"]

    def act(self, account, ann_id, action):
        return self.api_client(account).post(f"/api/me/inbox/{ann_id}/{action}/")


class InboxTests(InboxBase):
    def test_inbox_empty_before_any_send_and_ignores_drafts(self):
        self.announcement(status="draft")
        self.announcement(status="approved")  # approved but not sent
        self.assertEqual(self.get_inbox(self.member_acct), [])

    def test_inbox_lists_only_announcements_addressed_to_caller(self):
        everyone = self.sent(title="Everyone")
        apprentices = self.sent(audience=["Apprentice"], title="Apprentices only")
        journeyman = {i["title"] for i in self.get_inbox(self.member_acct)}
        apprentice = {i["title"] for i in self.get_inbox(self.apprentice)}
        self.assertEqual(journeyman, {"Everyone"})
        self.assertEqual(apprentice, {"Everyone", "Apprentices only"})

    def test_inbox_never_shows_another_locals_announcements(self):
        self.sent(title="Local A news")
        b_ann = self.sent(local=self.other_local, title="Local B news")
        self.assertEqual({i["title"] for i in self.get_inbox(self.member_acct)}, {"Local A news"})
        self.assertEqual({i["title"] for i in self.get_inbox(self.member_b)}, {"Local B news"})
        self.assertEqual([i["announcement_id"] for i in self.get_inbox(self.member_b)], [str(b_ann.id)])

    def test_inbox_available_even_if_push_never_went_out(self):
        ann = self.sent()  # worker has NOT run: recipient.sent_at is still NULL
        self.assertIsNone(self.row(ann, self.member_acct).sent_at)
        self.assertEqual(len(self.get_inbox(self.member_acct)), 1)

    def test_inbox_item_exposes_only_own_state_and_no_contact_details(self):
        self.sent()
        item = self.get_inbox(self.member_acct)[0]
        self.assertEqual(
            set(item),
            {"announcement_id", "title", "body", "push_preview", "needs_ack", "sent_at", "read_at", "acked_at"},
        )

    def test_inbox_reflects_own_read_state_only(self):
        ann = self.sent()
        self.act(self.member_acct, ann.id, "read")
        self.assertIsNotNone(self.get_inbox(self.member_acct)[0]["read_at"])
        self.assertIsNone(self.get_inbox(self.apprentice)[0]["read_at"])

    def test_leader_has_an_inbox_too_and_anonymous_is_refused(self):
        self.sent()
        self.assertEqual(len(self.get_inbox(self.leader)), 1)  # leaders are members
        self.assertEqual(sw.APIClient().get("/api/me/inbox/").status_code, 401)


class ReadTests(InboxBase):
    def test_read_sets_read_at_and_nothing_else(self):
        ann = self.sent()
        r = self.act(self.member_acct, ann.id, "read")
        self.assertEqual(r.status_code, 200)
        row = self.row(ann, self.member_acct)
        self.assertIsNotNone(row.read_at)
        self.assertIsNone(row.acked_at)
        self.assertIsNotNone(r.data["read_at"])

    def test_repeated_read_is_safe_and_keeps_first_timestamp(self):
        ann = self.sent()
        self.act(self.member_acct, ann.id, "read")
        first = self.row(ann, self.member_acct).read_at
        for _ in range(3):
            self.assertEqual(self.act(self.member_acct, ann.id, "read").status_code, 200)
        self.assertEqual(self.row(ann, self.member_acct).read_at, first)

    def test_read_works_on_announcements_that_need_no_ack(self):
        ann = self.sent(needs_ack=False)
        self.assertEqual(self.act(self.member_acct, ann.id, "read").status_code, 200)

    def test_get_on_action_endpoints_not_allowed(self):
        ann = self.sent()
        c = self.api_client(self.member_acct)
        self.assertEqual(c.get(f"/api/me/inbox/{ann.id}/read/").status_code, 405)
        self.assertEqual(c.get(f"/api/me/inbox/{ann.id}/ack/").status_code, 405)


class AckTests(InboxBase):
    def test_ack_sets_acked_at_and_fills_read_at(self):
        ann = self.sent()
        r = self.act(self.member_acct, ann.id, "ack")
        self.assertEqual(r.status_code, 200)
        row = self.row(ann, self.member_acct)
        self.assertIsNotNone(row.acked_at)
        self.assertIsNotNone(row.read_at)  # acknowledging implies reading
        self.assertLessEqual(row.read_at, row.acked_at)

    def test_ack_after_read_keeps_original_read_at(self):
        ann = self.sent()
        self.act(self.member_acct, ann.id, "read")
        read_at = self.row(ann, self.member_acct).read_at
        self.act(self.member_acct, ann.id, "ack")
        row = self.row(ann, self.member_acct)
        self.assertEqual(row.read_at, read_at)
        self.assertIsNotNone(row.acked_at)

    def test_repeated_ack_is_safe_and_keeps_first_timestamp(self):
        ann = self.sent()
        self.act(self.member_acct, ann.id, "ack")
        first = self.row(ann, self.member_acct)
        for _ in range(3):
            self.assertEqual(self.act(self.member_acct, ann.id, "ack").status_code, 200)
        again = self.row(ann, self.member_acct)
        self.assertEqual((again.acked_at, again.read_at), (first.acked_at, first.read_at))

    def test_ack_refused_when_announcement_does_not_request_it(self):
        ann = self.sent(needs_ack=False)
        r = self.act(self.member_acct, ann.id, "ack")
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(self.row(ann, self.member_acct).acked_at)

    def test_concurrent_acks_and_reads_stay_consistent(self):
        ann = self.sent()
        responses = sw.run_concurrently(
            lambda i: self.act(self.member_acct, ann.id, "ack" if i % 2 else "read"), 8
        )
        self.assertTrue(all(r.status_code == 200 for r in responses))
        row = self.row(ann, self.member_acct)
        self.assertIsNotNone(row.read_at)
        self.assertIsNotNone(row.acked_at)
        self.assertLessEqual(row.read_at, row.acked_at)
        self.assertEqual(AnnouncementRecipient.objects.filter(announcement=ann).count(), 41)  # no extra rows


class IsolationTests(InboxBase):
    def snapshot(self, ann):
        return list(
            AnnouncementRecipient.objects.filter(announcement=ann)
            .order_by("id").values_list("id", "read_at", "acked_at", "attempts")
        )

    def test_cross_local_read_and_ack_are_404_and_change_nothing(self):
        ann = self.sent()  # Local A
        before = self.snapshot(ann)
        for action in ("read", "ack"):
            self.assertEqual(self.act(self.member_b, ann.id, action).status_code, 404)
        self.assertEqual(self.snapshot(ann), before)

    def test_announcement_not_addressed_to_caller_is_404_even_in_same_local(self):
        ann = self.sent(audience=["Apprentice"])
        self.assertEqual(self.act(self.member_acct, ann.id, "read").status_code, 404)  # Journeyman
        self.assertEqual(self.act(self.member_acct, ann.id, "ack").status_code, 404)
        self.assertIsNone(self.row(ann, self.apprentice).read_at)

    def test_nonexistent_id_looks_identical_to_forbidden_one(self):
        ann = self.sent()
        import uuid
        a = self.act(self.member_b, ann.id, "read")
        b = self.act(self.member_b, uuid.uuid4(), "read")
        self.assertEqual((a.status_code, a.data), (b.status_code, b.data))

    def test_cannot_modify_another_members_recipient_row(self):
        ann = self.sent()
        before = self.row(ann, self.apprentice)
        # the caller tries to smuggle in someone else's identity and timestamps
        r = self.api_client(self.member_acct).post(
            f"/api/me/inbox/{ann.id}/read/",
            {"member_id": str(self.apprentice.member_id), "member": str(self.apprentice.member_id),
             "read_at": "2000-01-01T00:00:00Z", "acked_at": "2000-01-01T00:00:00Z"},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        after = self.row(ann, self.apprentice)
        self.assertEqual((after.read_at, after.acked_at), (before.read_at, before.acked_at))
        mine = self.row(ann, self.member_acct)
        self.assertIsNotNone(mine.read_at)
        self.assertNotEqual(mine.read_at.year, 2000)  # client-supplied timestamps are ignored
        self.assertIsNone(mine.acked_at)

    def test_recipient_ids_are_not_addressable_at_all(self):
        ann = self.sent()
        rid = self.row(ann, self.apprentice).id
        r = self.api_client(self.member_acct).post(f"/api/me/inbox/{rid}/read/")
        self.assertEqual(r.status_code, 404)  # route only accepts a UUID announcement id

    def test_leader_endpoints_still_closed_to_members(self):
        ann = self.sent()
        self.assertEqual(self.api_client(self.member_acct).post(f"/api/announcements/{ann.id}/send/").status_code, 403)

    def test_unauthenticated_action_is_401(self):
        ann = self.sent()
        self.assertEqual(sw.APIClient().post(f"/api/me/inbox/{ann.id}/read/").status_code, 401)
