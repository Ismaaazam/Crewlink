from django.test import TestCase


class HealthTests(TestCase):
    def test_health_is_public_and_reports_database(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "database": "ok"})
