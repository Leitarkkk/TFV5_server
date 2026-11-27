import base64
import json
import os
import smtplib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from threading import Lock
from unittest.mock import Mock, patch

from argon2 import PasswordHasher
from captcha.image import ImageCaptcha

import crypto
import register_tool
import web
from db.user import UserDb
from json_store import read_json, write_json


class AuthHttpRegressionTests(unittest.TestCase):
    port = 7001
    email = "alice@example.com"
    password = "old-password"

    @classmethod
    def setUpClass(cls):
        cls.private, cls.public, _, cls.public_pem, _ = crypto.generate_rsa_keys()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        original_directory = os.getcwd()
        os.chdir(directory.name)
        self.addCleanup(os.chdir, original_directory)
        self.config_path = Path(f"res/{self.port}/config.json")
        self.activation_path = self.config_path.with_name("activate.json")
        self.captcha_path = self.config_path.parent / "captcha/captcha.json"
        self.config = {"server_name": "Test instance", "legacy_auth_enabled": False}
        write_json(self.config_path, self.config)
        write_json(self.activation_path, {})
        write_json(self.captcha_path, {})
        self.users = UserDb(
            PasswordHasher(time_cost=1, memory_cost=32, parallelism=1),
            str(self.config_path.with_name("users.db")), self.port, 21474,
        )
        self.users.create_user_table()
        self.users.create_friend_table()
        self.addCleanup(self.users.conn.close)
        self.contact = Mock()
        app = web.main(
            self.port, 21474, self.public_pem, self.private, ImageCaptcha(),
            self.users, None, None, Mock(), None, SimpleNamespace(), self.contact,
            jwt_secret=b"x" * 32,
        )
        self.client = app.test_client()

    def post(self, path, **body):
        key = crypto.generate_aes_key()
        iv, ciphertext = crypto.aes_encrypt(json.dumps(body), key)

        def encode(value):
            return base64.b64encode(value).decode()

        response = self.client.post(path, json={
            "key": encode(crypto.encrypt(self.public, key)),
            "iv": encode(iv),
            "content": encode(ciphertext),
        })
        envelope = response.get_json()
        self.assertIsInstance(envelope, dict, response.get_data(as_text=True))
        return crypto.aes_decrypt(
            base64.b64decode(envelope["iv"]),
            base64.b64decode(envelope["content"]),
            key,
        ).decode()

    def test_history_reads_each_attachment_owner_only_once_per_request(self):
        files, messages, groups = Mock(), Mock(), Mock()
        files.get_metadata.side_effect = lambda key, owner_uid: {"file_name": str(owner_uid), "size": 1}
        messages.serialize_rows.side_effect = lambda rows: [
            {"file_hash": "same-hash", "sender_uid": uid, "file_name": f"name-{i}",
             "quote_preview": {"file_hash": "same-hash", "sender_uid": 5}}
            for i, uid in enumerate([3] * 40 + [5] * 10)
        ]
        app = web.main(self.port, 21474, self.public_pem, self.private, ImageCaptcha(),
                       self.users, None, files, Mock(), messages, groups, self.contact,
                       jwt_secret=b"x" * 32)
        self.client = app.test_client()
        self.assertTrue(self.users.user_create("alice", self.password, 1))
        uid = self.users.username_query("alice")[0][0]
        token = json.loads(self.post("/auth/login", uid=uid, password=self.password, jwt=True))["token"]
        with patch.object(web.file, "ensure_media") as ensure_media:
            records = json.loads(self.post("/message/history", group_id=7, token=token))
        self.assertEqual(len(records), 50)
        self.assertEqual(records[0]["file"]["file_name"], "name-0")
        self.assertEqual(records[0]["quote_preview"]["file"]["file_name"], "5")
        self.assertEqual(files.get_metadata.call_count, 2)
        self.assertEqual(ensure_media.call_count, 2)

    def test_password_login_and_change_on_jwt_only_server(self):
        self.assertTrue(self.users.user_create("alice", self.password, 1, self.email))
        uid = self.users.username_query("alice")[0][0]
        first_session = json.loads(self.post("/auth/login", uid=uid, password=self.password, jwt=True))
        self.assertEqual(self.users.count_active_sessions(uid), 1)
        self.assertTrue(self.post(
            "/auth/change_pwd", token=first_session["token"], new_pwd="new-password",
        ).endswith("True"))
        self.assertEqual(self.users.get_auth_version(uid), 1)
        self.assertEqual(self.users.count_active_sessions(uid), 0)
        self.contact.disconnect_user.assert_called_once_with(uid)
        second_session = json.loads(self.post("/auth/login", uid=uid, password="new-password", jwt=True))
        self.assertNotEqual(first_session["token"], second_session["token"])

    def test_refresh_can_be_retried_after_signing_failure(self):
        self.assertTrue(self.users.user_create("alice", self.password, 1, self.email))
        uid = self.users.username_query("alice")[0][0]
        session = json.loads(self.post("/auth/login", uid=uid, password=self.password, jwt=True))
        with patch.object(web.jwt_tool, "issue_token", side_effect=RuntimeError("temporarily unavailable")):
            failed = json.loads(self.post(
                "/auth/refresh", refresh_token=session["refresh_token"], detail_error=True,
            ))
        self.assertEqual(failed["error"], "AUTH_FAILED")
        refreshed = json.loads(self.post("/auth/refresh", refresh_token=session["refresh_token"]))
        self.assertNotEqual(refreshed["refresh_token"], session["refresh_token"])
        self.assertTrue(json.loads(self.post("/auth/validate", token=refreshed["token"]))["valid"])

    def test_pending_registration_resend_and_activation(self):
        self.config["email_activate"] = "sender@example.com"
        write_json(self.config_path, self.config)

        def enqueue(sender, port, recipient, password, config_lock, activate_lock, *, uid, purpose):
            self.assertEqual(self.users.uid_query(uid)[0][4], "pending")
            register_tool.issue_email_code(port, recipient, activate_lock, uid=uid, purpose=purpose)
            return True

        with patch.object(register_tool, "email_code_async", side_effect=enqueue) as mail:
            self.assertTrue(self.post(
                "/auth/register", username="alice", password=self.password, email=self.email,
            ).endswith("True"))
            uid = self.users.username_query("alice")[0][0]
            entry = read_json(self.activation_path)[self.email][register_tool.ACTIVATION]
            self.assertTrue(self.post(
                "/auth/resend_activation", uid=uid, password=self.password,
            ).endswith("True"))
            self.assertEqual(read_json(self.activation_path)[self.email][register_tool.ACTIVATION], entry)
            self.assertEqual(mail.call_count, 2)

        self.assertTrue(self.post("/auth/activate", uid=uid, activate_code=entry["code"]).endswith("True"))
        self.assertEqual(self.users.uid_query(uid)[0][4], "user")
        self.assertEqual(read_json(self.activation_path), {})

    def test_activation_and_password_reset_can_be_completed_independently(self):
        self.assertTrue(self.users.user_create("alice", self.password, 1, self.email, stat="pending"))
        uid = self.users.username_query("alice")[0][0]
        lock = Lock()
        activation_code = register_tool.issue_email_code(
            self.port, self.email, lock, uid=uid, purpose=register_tool.ACTIVATION,
        )
        password_code = register_tool.issue_email_code(
            self.port, self.email, lock, uid=uid, purpose=register_tool.PASSWORD_RESET,
        )
        self.contact.disconnect_user.side_effect = RuntimeError("listener stopped")
        self.assertTrue(self.post(
            "/auth/reset_password", email=self.email, activate_code=password_code, new_pwd="new-password",
        ).endswith("True"))
        self.contact.disconnect_user.assert_called_once_with(uid)
        self.assertEqual(self.users.uid_query(uid)[0][4], "pending")
        self.assertTrue(self.users.verify_user(uid, "new-password"))
        self.assertTrue(self.post("/auth/activate", uid=uid, activate_code=activation_code).endswith("True"))
        self.assertEqual(self.users.uid_query(uid)[0][4], "user")

    def test_legacy_stored_activation_code_remains_usable(self):
        self.assertTrue(self.users.user_create("alice", self.password, 1, self.email, stat="pending"))
        uid = self.users.username_query("alice")[0][0]
        write_json(self.activation_path, {self.email: 123456})
        self.assertTrue(self.post("/auth/activate", uid=uid, activate_code=123456).endswith("True"))
        self.assertEqual(self.users.uid_query(uid)[0][4], "user")

    def test_legacy_password_login_signals_pending_account(self):
        self.config["legacy_auth_enabled"] = True
        write_json(self.config_path, self.config)
        self.assertTrue(self.users.user_create("alice", self.password, 1, self.email, stat="pending"))
        uid = self.users.username_query("alice")[0][0]
        detail = json.loads(self.post("/auth/login", uid=uid, password=self.password, detail_error=True))
        self.assertEqual(detail["error"], "pending_activation")
        self.assertTrue(self.post("/auth/login", uid=uid, password=self.password).endswith("False"))
        self.assertTrue(self.users.activate_pending_user(uid, self.email))
        self.assertTrue(self.post("/auth/login", uid=uid, password=self.password).endswith("True"))

    def test_mail_connection_closes_even_when_quit_fails(self):
        smtp = Mock()
        smtp.quit.side_effect = smtplib.SMTPServerDisconnected("connection already closing")
        with patch.object(register_tool, "login_email", return_value=smtp):
            self.assertTrue(register_tool.send_email_code(
                "sender@example.com", self.port, self.email, "test", Lock(), 123456,
            ))
        smtp.sendmail.assert_called_once()
        smtp.close.assert_called_once_with()

    def test_two_image_captchas_issued_at_same_time_are_independent(self):
        self.config["captcha"] = True
        write_json(self.config_path, self.config)
        with patch.object(register_tool, "time", return_value=1_000):
            first_response = self.client.get("/auth/captcha")
            second_response = self.client.get("/auth/captcha")
            self.assertEqual(first_response.headers["Cache-Control"], "no-store")
            first = first_response.get_json()
            second = second_response.get_json()
            self.assertNotEqual(first["stamp"], second["stamp"])
            challenges = read_json(self.captcha_path)
            for challenge, name in ((first, "alice"), (second, "bobby")):
                self.assertTrue(base64.b64decode(challenge["pic"]).startswith(b"\x89PNG"))
                self.assertTrue(self.post(
                    "/auth/register", username=name, password=self.password,
                    captcha_stamp=str(challenge["stamp"]),
                    captcha_code=challenges[str(challenge["stamp"])]["answer"].lower(),
                ).endswith("True"))
        self.assertEqual(read_json(self.captcha_path), {})

    def test_new_image_captcha_prunes_expired_answers(self):
        self.config["captcha"] = True
        write_json(self.config_path, self.config)
        with patch.object(register_tool, "time", return_value=1_000):
            first = self.client.get("/auth/captcha").get_json()
        with patch.object(register_tool, "time", return_value=1_000 + register_tool.MAX_DELAY):
            second = self.client.get("/auth/captcha").get_json()
        challenges = read_json(self.captcha_path)
        self.assertNotIn(str(first["stamp"]), challenges)
        self.assertIn(str(second["stamp"]), challenges)

    def test_rate_limit_reloads_from_config_and_keeps_last_valid_policy(self):
        self.assertEqual(self.client.get("/info").status_code, 200)
        self.config["rate_limits"] = {"/info": {"requests": 1, "range": 60}}
        write_json(self.config_path, self.config)
        self.assertEqual(self.client.get("/info").status_code, 200)
        self.assertEqual(self.client.get("/info").status_code, 429)
        self.config["rate_limits"]["/info"]["range"] = "invalid"
        write_json(self.config_path, self.config)
        with self.assertLogs("rate_limiter", level="WARNING"):
            self.assertEqual(self.client.get("/info").status_code, 429)
        del self.config["rate_limits"]
        write_json(self.config_path, self.config)
        self.assertEqual(self.client.get("/info").status_code, 200)
