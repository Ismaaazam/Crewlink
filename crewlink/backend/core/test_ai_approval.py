"""Phase 6 tests: AI drafting, 120-character limit, approval gate, edit-after-approval."""
import io
import json
import os
import socket
import urllib.error
from unittest import mock

from django.db import DataError, IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import ai
from . import test_send_worker as sw
from .models import Account, Announcement, AnnouncementRecipient, Local, Member, PushLog


class Fx:
    """Shared fixtures; works with TestCase and TransactionTestCase."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if k.startswith("AI_")]:
            os.environ.pop(key)  # tests never depend on the developer's real AI settings
        self.local = Local.objects.create(name="Local A")
        self.other_local = Local.objects.create(name="Local B")
        self.leader = self.acct(self.local, "leader.a", "leader")
        self.member = self.acct(self.local, "member.a", "member")
        self.other_leader = self.acct(self.other_local, "leader.b", "leader")
        Member.objects.bulk_create(
            [Member(local=self.local, full_name=f"M{i}", email=f"m{i}@a.test", classification="Journeyman")
             for i in range(5)]
        )

    def acct(self, local, username, role):
        m = Member.objects.create(local=local, full_name=username, email=f"{username}@x.test",
                                  classification="Business Manager" if role == "leader" else "Journeyman")
        return Account.objects.create_user(username, "pw-not-used", m, role)

    def api(self, account):
        c = APIClient()
        c.force_authenticate(user=account)
        return c

    def draft_row(self, local=None, **kw):
        fields = dict(local=local or self.local, title="Meeting", body="Hall at 17:00",
                      push_preview="Meeting at 17:00", status="draft")
        fields.update(kw)
        return Announcement.objects.create(**fields)

    def approve(self, ann, account=None):
        return self.api(account or self.leader).post(f"/api/announcements/{ann.id}/approve/")

    def send(self, ann, account=None):
        return self.api(account or self.leader).post(f"/api/announcements/{ann.id}/send/")

    def patch(self, ann, body, account=None):
        return self.api(account or self.leader).patch(f"/api/announcements/{ann.id}/", body, format="json")

    def ai_draft(self, body=None, account=None):
        return self.api(account or self.leader).post(
            "/api/announcements/draft/",
            body if body is not None else {"prompt": "Emergency meeting tonight 17:00 at the hall"}, format="json"
        )


class StubProvider:
    name = "stub"

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def draft(self, prompt, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


GOOD = {"title": "Meeting tonight", "body": "Hall at 17:00.", "push_preview": "Meeting tonight 17:00"}


def with_provider(provider):
    return mock.patch("core.ai.get_provider", return_value=provider)


# ------------------------------------------------------------------- AI drafting
class DraftEndpointTests(Fx, TestCase):
    def test_fake_provider_draft_is_stored_as_unapproved_draft(self):
        r = self.ai_draft()
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.data["source"], r.data["provider"]), ("ai", "fake"))
        a = Announcement.objects.get(pk=r.data["announcement"]["id"])
        self.assertEqual(a.status, "draft")
        self.assertIsNone(a.approved_at)
        self.assertIsNone(a.sent_at)
        self.assertEqual(a.local_id, self.local.id)
        self.assertEqual(a.created_by_id, self.leader.id)
        self.assertTrue(0 < len(a.push_preview) <= 120)
        self.assertTrue(a.title and a.body)

    def test_ai_draft_alone_sends_nothing(self):
        self.ai_draft()
        self.assertFalse(AnnouncementRecipient.objects.exists())
        self.assertFalse(PushLog.objects.exists())

    def test_ai_draft_cannot_be_sent_until_a_human_approves(self):
        a = Announcement.objects.get(pk=self.ai_draft().data["announcement"]["id"])
        self.assertEqual(self.send(a).status_code, 409)
        self.assertEqual(self.approve(a).status_code, 200)
        self.assertEqual(self.send(a).status_code, 202)

    def test_caller_cannot_choose_local_status_or_approval(self):
        r = self.ai_draft({"prompt": "Hello members", "status": "approved", "approved_at": "2020-01-01T00:00:00Z",
                           "local_id": str(self.other_local.id), "sent_at": "2020-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 201)
        a = Announcement.objects.get(pk=r.data["announcement"]["id"])
        self.assertEqual((a.status, a.local_id, a.approved_at, a.sent_at), ("draft", self.local.id, None, None))

    def test_optional_fields_are_applied(self):
        r = self.ai_draft({"prompt": "Safety stand-down", "needs_ack": True, "audience_classifications": ["Apprentice"]})
        a = Announcement.objects.get(pk=r.data["announcement"]["id"])
        self.assertEqual((a.needs_ack, a.audience_classifications), (True, ["Apprentice"]))

    def test_prompt_is_required_and_bounded(self):
        self.assertEqual(self.ai_draft({}).status_code, 400)
        self.assertEqual(self.ai_draft({"prompt": "   "}).status_code, 400)
        self.assertEqual(self.ai_draft({"prompt": "x" * 1001}).status_code, 400)
        self.assertFalse(Announcement.objects.exists())

    def test_members_and_anonymous_are_refused(self):
        self.assertEqual(self.ai_draft(account=self.member).status_code, 403)
        self.assertEqual(APIClient().post("/api/announcements/draft/", {"prompt": "x"}, format="json").status_code, 401)

    def test_api_key_never_appears_in_responses(self):
        os.environ["AI_API_KEY"] = "sk-super-secret-value"
        os.environ["AI_PROVIDER"] = "fake"
        r = self.ai_draft()
        self.assertNotIn("sk-super-secret", json.dumps(r.data, default=str))
        os.environ["AI_FAKE_MODE"] = "fail"
        r = self.ai_draft()
        self.assertNotIn("sk-super-secret", json.dumps(r.data, default=str))


class ProviderFailureTests(Fx, TestCase):
    def assert_manual_fallback(self, r):
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data["fallback"], "manual")
        self.assertEqual(r.data["manual_endpoint"], "/api/announcements/")
        self.assertFalse(Announcement.objects.exists())  # nothing half-created

    def test_one_failure_is_retried_and_then_succeeds(self):
        stub = StubProvider(ai.ProviderError("timeout"), GOOD)
        with with_provider(stub):
            r = self.ai_draft()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(stub.calls, 2)

    def test_two_failures_give_manual_fallback_after_exactly_one_retry(self):
        stub = StubProvider(ai.ProviderError("timeout"), ai.ProviderError("timeout"), GOOD)
        with with_provider(stub):
            r = self.ai_draft()
        self.assert_manual_fallback(r)
        self.assertEqual(stub.calls, 2)

    def test_retry_count_is_configurable(self):
        os.environ["AI_MAX_RETRIES"] = "0"
        stub = StubProvider(ai.ProviderError("down"), GOOD)
        with with_provider(stub):
            self.assert_manual_fallback(self.ai_draft())
        self.assertEqual(stub.calls, 1)

    def test_fake_provider_outage_and_timeout_modes(self):
        for mode in ("fail", "timeout"):
            os.environ["AI_FAKE_MODE"] = mode
            self.assert_manual_fallback(self.ai_draft())

    def test_overlong_provider_output_is_rejected_never_truncated(self):
        os.environ["AI_FAKE_MODE"] = "invalid"  # push_preview of 121 characters
        self.assert_manual_fallback(self.ai_draft())
        stub = StubProvider({**GOOD, "push_preview": "y" * 121}, {**GOOD, "push_preview": "y" * 121})
        with with_provider(stub):
            self.assert_manual_fallback(self.ai_draft())

    def test_malformed_or_empty_provider_output_is_rejected(self):
        for bad in ("not a dict", {"title": "t"}, {**GOOD, "body": "  "}, {**GOOD, "title": 5}):
            with with_provider(StubProvider(bad, bad)):
                self.assert_manual_fallback(self.ai_draft())

    def test_provider_output_of_exactly_120_characters_is_accepted(self):
        stub = StubProvider({**GOOD, "push_preview": "z" * 120})
        with with_provider(stub):
            r = self.ai_draft()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(r.data["announcement"]["push_preview"]), 120)

    def test_misconfiguration_falls_back_without_retrying(self):
        os.environ["AI_PROVIDER"] = "anthropic"  # key missing
        self.assert_manual_fallback(self.ai_draft())
        os.environ["AI_PROVIDER"] = "no-such-provider"
        self.assert_manual_fallback(self.ai_draft())

    def test_manual_entry_still_works_while_the_provider_is_down(self):
        os.environ["AI_FAKE_MODE"] = "fail"
        self.assert_manual_fallback(self.ai_draft())
        r = self.api(self.leader).post(
            "/api/announcements/",
            {"title": "Meeting", "body": "Hall at 17:00", "push_preview": "Meeting at 17:00"}, format="json",
        )
        self.assertEqual(r.status_code, 201)
        a = Announcement.objects.get(pk=r.data["id"])
        self.assertEqual(a.status, "draft")
        self.assertEqual(self.approve(a).status_code, 200)
        self.assertEqual(self.send(a).status_code, 202)  # whole flow works with AI down


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class AnthropicProviderTests(TestCase):
    """The real provider, with the network mocked. NOT a test against the live API."""

    def provider(self):
        return ai.AnthropicProvider("sk-test-key", model="some-model", url="https://example.invalid/v1/messages")

    def test_parses_reply_sends_key_in_header_and_passes_timeout(self):
        reply = {"content": [{"type": "text", "text": "```json\n" + json.dumps(GOOD) + "\n```"}]}
        with mock.patch("core.ai.urllib.request.urlopen", return_value=_Resp(reply)) as urlopen:
            out = self.provider().draft("notes", timeout=8)
        self.assertEqual(out, GOOD)
        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 8)
        self.assertEqual(request.get_header("X-api-key"), "sk-test-key")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "some-model")
        self.assertIn("120", body["system"])
        self.assertNotIn("sk-test-key", request.data.decode())  # key only in the header

    def test_timeouts_http_errors_and_garbage_become_provider_errors(self):
        cases = [
            socket.timeout("slow"),
            urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"")),
            urllib.error.HTTPError("u", 401, "bad key sk-test-key", {}, io.BytesIO(b"")),
            urllib.error.URLError(ConnectionRefusedError()),
        ]
        for exc in cases:
            with mock.patch("core.ai.urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(ai.ProviderError) as cm:
                    self.provider().draft("notes", timeout=8)
            self.assertNotIn("sk-test-key", str(cm.exception))
        for payload in ({"content": [{"type": "text", "text": "no json here"}]}, {"unexpected": 1}):
            with mock.patch("core.ai.urllib.request.urlopen", return_value=_Resp(payload)):
                with self.assertRaises(ai.ProviderError):
                    self.provider().draft("notes", timeout=8)

    def test_get_provider_reads_environment(self):
        with mock.patch.dict(os.environ, {"AI_PROVIDER": "anthropic", "AI_API_KEY": "k", "AI_MODEL": "m"}):
            p = ai.get_provider()
        self.assertEqual((p.name, p.api_key, p.model), ("anthropic", "k", "m"))


# --------------------------------------------------------------- 120-char limit
class CharacterLimitTests(Fx, TestCase):
    def create(self, preview):
        return self.api(self.leader).post(
            "/api/announcements/", {"title": "T", "body": "B", "push_preview": preview}, format="json"
        )

    def test_create_enforces_120_characters(self):
        self.assertEqual(self.create("a" * 120).status_code, 201)
        r = self.create("a" * 121)
        self.assertEqual(r.status_code, 400)
        self.assertIn("push_preview", r.data)
        self.assertEqual(Announcement.objects.count(), 1)

    def test_limit_counts_characters_not_bytes(self):
        self.assertEqual(self.create("\u00e9" * 120).status_code, 201)  # 240 bytes
        self.assertEqual(self.create("\U0001F600" * 120).status_code, 201)
        self.assertEqual(self.create("\u00e9" * 121).status_code, 400)

    def test_patch_enforces_120_and_leaves_row_unchanged(self):
        a = self.draft_row()
        self.assertEqual(self.patch(a, {"push_preview": "b" * 121}).status_code, 400)
        a.refresh_from_db()
        self.assertEqual(a.push_preview, "Meeting at 17:00")
        self.assertEqual(self.patch(a, {"push_preview": "b" * 120}).status_code, 200)

    def test_database_enforces_it_too(self):
        with self.assertRaises(DataError), transaction.atomic():
            Announcement.objects.create(local=self.local, title="T", body="B", push_preview="c" * 121)


# ------------------------------------------------------------ approval and send
class ApprovalTests(Fx, TestCase):
    def test_send_refused_without_approval_and_nothing_is_created(self):
        a = self.draft_row()
        r = self.send(a)
        self.assertEqual(r.status_code, 409)
        a.refresh_from_db()
        self.assertEqual((a.status, a.sent_at), ("draft", None))
        self.assertFalse(AnnouncementRecipient.objects.exists())

    def test_explicit_approval_sets_status_and_timestamp_then_send_works(self):
        a = self.draft_row()
        r = self.approve(a)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["already_approved"])
        a.refresh_from_db()
        self.assertEqual(a.status, "approved")
        self.assertIsNotNone(a.approved_at)
        self.assertEqual(self.send(a).status_code, 202)

    def test_approve_is_idempotent_and_keeps_first_timestamp(self):
        a = self.draft_row()
        self.approve(a)
        a.refresh_from_db()
        first = a.approved_at
        r = self.approve(a)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["already_approved"])
        a.refresh_from_db()
        self.assertEqual(a.approved_at, first)

    def test_approve_needs_notification_text(self):
        a = self.draft_row(push_preview="")
        r = self.approve(a)
        self.assertEqual(r.status_code, 400)
        a.refresh_from_db()
        self.assertEqual((a.status, a.approved_at), ("draft", None))

    def test_approving_a_sent_announcement_is_409(self):
        a = self.draft_row()
        self.approve(a)
        self.send(a)
        self.assertEqual(self.approve(a).status_code, 409)

    def test_status_cannot_be_set_through_patch_or_create(self):
        a = self.draft_row()
        r = self.patch(a, {"status": "approved", "approved_at": timezone.now().isoformat(), "title": "New"})
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertEqual((a.status, a.approved_at, a.title), ("draft", None, "New"))
        r = self.api(self.leader).post(
            "/api/announcements/",
            {"title": "T", "body": "B", "status": "approved", "approved_at": timezone.now().isoformat()}, format="json",
        )
        self.assertEqual(Announcement.objects.get(pk=r.data["id"]).status, "draft")
        self.assertEqual(self.send(Announcement.objects.get(pk=r.data["id"])).status_code, 409)

    def test_database_refuses_approved_without_timestamp(self):
        a = self.draft_row()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Announcement.objects.filter(pk=a.pk).update(status="approved")

    def test_approve_is_leader_only_and_local_scoped(self):
        a = self.draft_row()
        self.assertEqual(self.approve(a, self.member).status_code, 403)
        self.assertEqual(self.approve(a, self.other_leader).status_code, 404)
        self.assertEqual(APIClient().post(f"/api/announcements/{a.id}/approve/").status_code, 401)
        a.refresh_from_db()
        self.assertEqual((a.status, a.approved_at), ("draft", None))


class EditAfterApprovalTests(Fx, TestCase):
    def approved(self):
        a = self.draft_row()
        self.assertEqual(self.approve(a).status_code, 200)
        return a

    def test_edit_after_approval_clears_approval_and_returns_to_draft(self):
        a = self.approved()
        r = self.patch(a, {"body": "Hall at 18:00 instead"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["status"], r.data["approved_at"]), ("draft", None))
        a.refresh_from_db()
        self.assertEqual((a.status, a.approved_at, a.body), ("draft", None, "Hall at 18:00 instead"))

    def test_edited_announcement_cannot_be_sent_until_reapproved(self):
        a = self.approved()
        self.patch(a, {"title": "Changed"})
        self.assertEqual(self.send(a).status_code, 409)
        self.assertFalse(AnnouncementRecipient.objects.exists())
        self.assertEqual(self.approve(a).status_code, 200)
        self.assertEqual(self.send(a).status_code, 202)

    def test_every_editable_field_withdraws_approval(self):
        for body in ({"title": "x"}, {"body": "x"}, {"push_preview": "x"}, {"needs_ack": True},
                     {"audience_classifications": ["Apprentice"]}):
            a = self.approved()
            self.assertEqual(self.patch(a, body).status_code, 200)
            a.refresh_from_db()
            self.assertEqual((a.status, a.approved_at), ("draft", None), body)

    def test_sent_announcements_cannot_be_edited(self):
        a = self.approved()
        self.send(a)
        before = Announcement.objects.get(pk=a.pk)
        self.assertEqual(self.patch(a, {"title": "Too late"}).status_code, 409)
        after = Announcement.objects.get(pk=a.pk)
        self.assertEqual((after.title, after.status, after.sent_at), (before.title, "sent", before.sent_at))

    def test_invalid_edit_does_not_withdraw_approval(self):
        a = self.approved()
        self.assertEqual(self.patch(a, {"push_preview": "p" * 121}).status_code, 400)
        a.refresh_from_db()
        self.assertEqual(a.status, "approved")  # nothing changed, so the approval stands
        self.assertIsNotNone(a.approved_at)

    def test_edit_is_local_scoped(self):
        a = self.approved()
        self.assertEqual(self.patch(a, {"title": "x"}, self.other_leader).status_code, 404)
        self.assertEqual(self.patch(a, {"title": "x"}, self.member).status_code, 403)
        a.refresh_from_db()
        self.assertEqual((a.status, a.title), ("approved", "Meeting"))


class EditVersusSendRaceTests(Fx, TransactionTestCase):
    """An edit and a send racing for the same approved announcement: exactly one wins,
    and the final state is never 'edited text that was sent without approval'."""

    def test_edit_and_send_never_both_succeed(self):
        for _ in range(6):
            a = self.draft_row()
            self.approve(a)

            def act(i, a=a):
                return self.patch(a, {"title": "Edited"}) if i == 0 else self.send(a)

            patch_resp, send_resp = sw.run_concurrently(act, 2)
            pair = (patch_resp.status_code, send_resp.status_code)
            self.assertIn(pair, [(409, 202), (200, 409)], pair)
            a.refresh_from_db()
            recipients = AnnouncementRecipient.objects.filter(announcement=a).count()
            if send_resp.status_code == 202:  # send committed first: unedited and sent
                self.assertEqual((a.status, a.title), ("sent", "Meeting"))
                self.assertGreater(recipients, 0)
            else:  # edit committed first: back to draft, nothing sent
                self.assertEqual((a.status, a.approved_at, a.sent_at, a.title), ("draft", None, None, "Edited"))
                self.assertEqual(recipients, 0)
