"""Test suite for Auto-Deploy, Secrets Vault, and Custom Domains using standard library unittest."""
import unittest
from fastapi.testclient import TestClient

from app.main import app


class TestNewFeatures(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_auto_deploy_crud_and_test_push(self):
        # 1. Create auto-deploy config
        res = self.client.post(
            "/auto-deploy/configs",
            json={"repo_url": "https://github.com/my-org/my-project", "branch": "main", "auto_rollback": True},
        )
        self.assertEqual(res.status_code, 201)
        cfg = res.json()
        self.assertEqual(cfg["repo_url"], "https://github.com/my-org/my-project")
        self.assertEqual(cfg["branch"], "main")
        self.assertIn("webhook_secret", cfg)
        config_id = cfg["id"]

        # 2. List configs
        res = self.client.get("/auto-deploy/configs")
        self.assertEqual(res.status_code, 200)
        configs = res.json()
        self.assertTrue(any(c["id"] == config_id for c in configs))

        # 3. Trigger test push
        res = self.client.post(
            "/auto-deploy/test-push",
            json={"config_id": config_id, "commit_message": "test commit for ci/cd"},
        )
        self.assertEqual(res.status_code, 200)
        evt = res.json()
        self.assertEqual(evt["status"], "triggered")
        self.assertEqual(evt["commit_message"], "test commit for ci/cd")

        # 4. List events
        res = self.client.get("/auto-deploy/events")
        self.assertEqual(res.status_code, 200)
        events = res.json()
        self.assertGreater(len(events), 0)

    def test_secrets_vault_lifecycle(self):
        # 1. Create encrypted secret
        res = self.client.post(
            "/secrets",
            json={
                "key": "TEST_DB_PASSWORD",
                "value": "super_secret_p@ssw0rd_123!",
                "environment": "production",
                "rotation_interval_days": 30,
            },
        )
        self.assertEqual(res.status_code, 201)
        secret = res.json()
        self.assertEqual(secret["key"], "TEST_DB_PASSWORD")
        self.assertEqual(secret["environment"], "production")
        self.assertEqual(secret["version"], 1)
        self.assertNotIn("super_secret", secret["masked_value"])
        secret_id = secret["id"]

        # 2. Reveal secret (audit logged)
        res = self.client.post(f"/secrets/{secret_id}/reveal")
        self.assertEqual(res.status_code, 200)
        revealed = res.json()
        self.assertEqual(revealed["plain_value"], "super_secret_p@ssw0rd_123!")

        # 3. Rotate secret (auto generate)
        res = self.client.post(f"/secrets/{secret_id}/rotate", json={"auto_generate": True})
        self.assertEqual(res.status_code, 200)
        rotated = res.json()
        self.assertEqual(rotated["version"], 2)
        self.assertEqual(rotated["status"], "active")

        # 4. Verify audit log
        res = self.client.get("/secrets/audit-logs")
        self.assertEqual(res.status_code, 200)
        logs = res.json()
        actions = [l["action"] for l in logs if l["secret_key"] == "TEST_DB_PASSWORD"]
        self.assertIn("create", actions)
        self.assertIn("reveal", actions)
        self.assertIn("rotate", actions)

    def test_custom_domains_lifecycle(self):
        # 1. Register custom domain
        res = self.client.post(
            "/domains",
            json={"domain": "api.production-test.io", "target_url": "https://service.onrender.com"},
        )
        self.assertEqual(res.status_code, 201)
        dom = res.json()
        self.assertEqual(dom["domain"], "api.production-test.io")
        self.assertEqual(dom["status"], "pending_dns")
        self.assertEqual(dom["dns_record"]["type"], "CNAME")
        self.assertEqual(dom["dns_record"]["host"], "api")
        domain_id = dom["id"]

        # 2. Verify DNS
        res = self.client.post(f"/domains/{domain_id}/verify-dns")
        self.assertEqual(res.status_code, 200)
        verified = res.json()
        self.assertTrue(verified["dns_verified"])
        self.assertEqual(verified["status"], "active")
        self.assertEqual(verified["ssl_status"], "issued")

        # 3. Health check probe
        res = self.client.post(f"/domains/{domain_id}/check-health")
        self.assertEqual(res.status_code, 200)
        health = res.json()
        self.assertEqual(health["health_status"], "healthy")
        self.assertGreater(health["latency_ms"], 0)


if __name__ == "__main__":
    unittest.main()
