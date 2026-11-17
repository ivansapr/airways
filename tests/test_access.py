import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from access import CredentialVault
from bot import APIError, Bot
from tracking import WatchStore


class PromoTests(unittest.TestCase):
    def setUp(self):
        self.store = WatchStore()
        self.addCleanup(self.store.db.close)

    def test_usage_limit_case_insensitivity_and_replay(self):
        promo = self.store.create_promo("Friends", max_uses=1)
        self.assertTrue(self.store.redeem(promo["code"].lower(), 123, 456))
        grant = self.store.get_access(123)
        self.assertEqual(grant["mode"], "promo")
        self.assertFalse(self.store.redeem(promo["code"], 123, 456))
        self.assertEqual(self.store.get_access(123)["expires_at"], grant["expires_at"])
        self.assertFalse(self.store.redeem(promo["code"], 124, 456))
        self.assertIsNone(self.store.get_access(124))

    def test_custom_code_is_normalized_redeemable_and_stored_only_as_hash(self):
        promo = self.store.create_promo('Family', code=' family_2026 ')
        self.assertEqual(promo['code'], 'FAMILY_2026')
        self.assertTrue(self.store.redeem('family_2026', 123, 123))
        self.assertNotIn('FAMILY_2026', str(tuple(self.store.db.execute('SELECT * FROM promos').fetchone())))
        self.assertNotIn('FAMILY_2026', json.dumps(self.store.dashboard()))

    def test_custom_code_cannot_duplicate_revoked_or_differently_cased_code(self):
        promo = self.store.create_promo('First', code='Family2026')
        self.store.revoke_promo(promo['id'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.store.create_promo('Second', code='family2026')
        self.assertEqual(len(self.store.dashboard()['promos']), 1)

    def test_invalid_custom_codes_are_rejected(self):
        for code in ('short', 'a' * 65, 'with space', '<script>', 'café2026', [], None):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'Custom code'):
                self.store.create_promo('Invalid', code=code)

    def test_expired_code_and_expired_grant_are_separate(self):
        promo = self.store.create_promo("Short", redeem_days=1, grant_days=30)
        self.assertTrue(self.store.redeem(promo["code"], 123, 456))
        with patch("tracking.time.time", return_value=time.time() + 2 * 86400):
            self.assertIsNotNone(self.store.get_access(123))
            self.assertFalse(self.store.redeem(promo["code"], 124, 456))
        with patch("tracking.time.time", return_value=time.time() + 31 * 86400):
            self.assertIsNone(self.store.get_access(123))

    def test_revoking_promo_disables_all_its_grants(self):
        promo = self.store.create_promo("Friends", max_uses=2)
        for chat_id in (123, 124):
            self.store.redeem(promo["code"], chat_id, 456)
        self.store.revoke_promo(promo["id"])
        for chat_id in (123, 124):
            self.assertIsNone(self.store.get_access(chat_id))

    def test_only_hashes_are_stored_and_codes_are_not_in_dashboard(self):
        promo = self.store.create_promo("Friends")
        row = self.store.db.execute("SELECT * FROM promos").fetchone()
        self.assertNotIn(promo["code"], str(tuple(row)))
        data = json.dumps(self.store.dashboard())
        self.assertNotIn(promo["code"], data)
        self.assertNotIn("code_hash", data)

    def test_concurrent_last_use_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "access.sqlite3")
            store = WatchStore(path)
            promo = store.create_promo("One use")
            store.db.close()
            def redeem(chat_id):
                other = WatchStore(path)
                try:
                    return other.redeem(promo["code"], chat_id, 456)
                finally:
                    other.db.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(redeem, (123, 124)))
            self.assertEqual(sum(results), 1)

    def test_customer_credentials_persist_encrypted_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "credentials.sqlite3")
            key = Fernet.generate_key()
            store = WatchStore(path)
            store.set_credentials(123, CredentialVault(key).seal("client-id", "client-secret"), 123)
            store.db.close()
            restored = WatchStore(path)
            try:
                grant = restored.get_access(123)
                self.assertEqual(CredentialVault(key).open(grant["credentials"]), ("client-id", "client-secret"))
            finally:
                restored.db.close()


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.store = WatchStore()
        self.addCleanup(self.store.db.close)
        self.owner = Mock()
        self.owner.nearby.return_value = ([], time.time())
        self.vault = CredentialVault(Fernet.generate_key())
        self.bot = Bot("token", self.owner, store=self.store, vault=self.vault)
        self.bot.send = Mock()
        self.bot.call = Mock(return_value={"status": "administrator"})
        self.chat = {"id": 123, "type": "private"}

    def message(self, text="", **extra):
        return {"message": {"chat": self.chat, "from": {"id": 123}, "message_id": 42, "text": text, **extra}}

    def test_free_location_uses_owner_without_creating_watch(self):
        self.bot.handle(self.message(location={"latitude": 38.7, "longitude": -9.1}))
        self.owner.nearby.assert_called_once_with(38.7, -9.1, 50)
        self.assertIsNone(self.store.get(123))
        self.assertIn("No watch started", self.bot.send.call_args.args[1])

    def test_free_cannot_start_watch_by_reply(self):
        self.bot.handle(self.message('/watch', reply_to_message={'location': {'latitude': 38.7, 'longitude': -9.1}}))
        self.owner.nearby.assert_not_called()
        self.assertIsNone(self.store.get(123))
        self.assertIn('require a promo code', self.bot.send.call_args.args[1])

    def test_free_live_edits_do_not_trigger_checks_even_for_paused_watch(self):
        self.store.set_location(123, 38.7, -9.1, 50, location_message_id=42)
        self.bot.handle({'edited_message': self.message(location={'latitude': 39, 'longitude': -9})['message']})
        self.owner.nearby.assert_not_called()
        self.assertEqual(self.store.get(123)['lat'], 38.7)

    def test_free_without_owner_credentials_explains_setup(self):
        self.bot.provider = None
        self.bot.handle(self.message(location={'latitude': 38.7, 'longitude': -9.1}))
        self.assertIsNone(self.store.get(123))
        self.assertIn('owner must configure OpenSky', self.bot.send.call_args.args[1])

    def test_credentials_without_arguments_shows_registration_without_deletion(self):
        self.bot.handle(self.message('/credentials'))
        self.bot.call.assert_not_called()
        text = self.bot.send.call_args.args[1]
        self.assertIn('https://opensky-network.org/my-opensky/account', text)
        self.assertIn('Register or sign in', text)
        self.assertIn('create an API client', text)
        self.assertIn('/credentials CLIENT_ID CLIENT_SECRET', text)

    def test_register_shows_provider_website_and_group_setup_template(self):
        self.chat = {'id': -123, 'type': 'group'}
        self.bot.handle(self.message('/register'))
        self.bot.call.assert_not_called()
        self.assertIn('Register or sign in at OpenSky', self.bot.send.call_args.args[1])
        self.assertIn('/credentials -123 CLIENT_ID CLIENT_SECRET', self.bot.send.call_args.args[1])

    def test_existing_unactivated_watch_never_uses_owner(self):
        self.store.set_location(123, 38.7, -9.1, 50)
        self.bot.check_watches()
        self.owner.nearby.assert_not_called()

    def test_promo_activates_only_redeeming_chat(self):
        promo = self.store.create_promo("Friend")
        self.bot.handle(self.message("/promo " + promo["code"]))
        self.assertIs(self.bot.provider_for(123), self.owner)
        self.assertIsNone(self.bot.provider_for(124))
        self.bot.handle(self.message(location={"latitude": 38.7, "longitude": -9.1}))
        self.owner.nearby.assert_called_once()
        self.store.revoke_promo(promo["id"])
        watch = self.store.get(123); watch["next_check"] = 0; self.store.save(watch)
        self.bot.check_watches()
        self.assertEqual(self.owner.nearby.call_count, 1)

    def test_group_non_admin_cannot_redeem(self):
        self.chat = {"id": -123, "type": "group"}
        self.bot.call.return_value = {"status": "member"}
        promo = self.store.create_promo("Friend")
        self.bot.handle(self.message("/promo " + promo["code"]))
        self.assertIsNone(self.store.get_access(-123))

    def test_no_owner_credentials_means_no_promo_redemption(self):
        self.bot.provider = None
        promo = self.store.create_promo("Friend")
        self.bot.handle(self.message("/promo " + promo["code"]))
        self.assertIsNone(self.store.get_access(123))
        self.assertEqual(self.store.dashboard()["promos"][0]["uses"], 0)

    def test_group_credentials_can_be_connected_privately_by_admin(self):
        with patch("bot.OpenSky") as factory:
            self.bot.handle(self.message("/credentials -123 customer-id customer-secret"))
            factory.return_value.headers.assert_called_once()
        self.assertEqual(self.store.get_access(-123)["configured_by"], 123)
        self.assertIsNone(self.store.get_access(123))

    def test_access_expires_across_restart_and_can_use_new_code(self):
        promo = self.store.create_promo("Short", grant_days=1)
        self.store.redeem(promo["code"], 123, 123)
        with patch("tracking.time.time", return_value=time.time() + 2 * 86400):
            restored_bot = Bot("token", self.owner, store=self.store, vault=self.vault)
            self.assertIsNone(restored_bot.provider_for(123))
        self.assertFalse(self.store.redeem(promo["code"], 123, 123))

    def test_credentials_are_encrypted_and_isolated_from_owner(self):
        customer = Mock()
        with patch("bot.OpenSky", return_value=customer):
            self.bot.handle(self.message("/credentials customer-id customer-secret"))
        grant = self.store.get_access(123)
        self.assertEqual(self.vault.open(grant["credentials"]), ("customer-id", "customer-secret"))
        self.assertNotIn("customer-secret", grant["credentials"])
        with patch("bot.OpenSky", return_value=customer) as factory:
            self.assertIs(self.bot.provider_for(123), customer)
            factory.assert_called_once_with("customer-id", "customer-secret", usage_scope="customer")
        self.assertNotIn("customer-secret", json.dumps(self.store.dashboard()))
        self.assertNotIn(grant["credentials"], json.dumps(self.store.dashboard()))
        customer.nearby.side_effect = APIError(429)
        self.bot.handle(self.message(location={"latitude": 38.7, "longitude": -9.1}))
        self.owner.nearby.assert_not_called()

    def test_invalid_credentials_do_not_replace_access(self):
        with patch("bot.OpenSky") as factory:
            factory.return_value.headers.side_effect = APIError(401)
            self.bot.handle(self.message("/credentials bad-id bad-secret"))
        self.assertIsNone(self.store.get_access(123))

    def test_group_credentials_are_rejected_and_command_deleted(self):
        self.chat = {"id": -123, "type": "group"}
        self.bot.handle(self.message("/credentials secret-id secret-password"))
        self.assertIsNone(self.store.get_access(-123))
        self.bot.call.assert_called_once_with("deleteMessage", chat_id=-123, message_id=42)
        self.assertNotIn("secret-password", str(self.bot.send.call_args))

    def test_private_group_credentials_require_group_admin(self):
        self.bot.call.return_value = {"status": "member"}
        self.bot.handle(self.message("/credentials -123 customer-id customer-secret"))
        self.assertIsNone(self.store.get_access(-123))

    def test_wrong_encryption_key_never_falls_back_to_owner(self):
        other_vault = CredentialVault(Fernet.generate_key())
        self.store.set_credentials(123, other_vault.seal("id", "secret"), 123)
        self.assertIsNone(self.bot.provider_for(123))

    def test_disconnect_deletes_credentials_and_watch(self):
        self.store.set_credentials(123, self.vault.seal("id", "secret"), 123)
        self.store.set_location(123, 38.7, -9.1, 50)
        self.bot.handle(self.message("/disconnect"))
        self.assertIsNone(self.store.get_access(123))
        self.assertIsNone(self.store.get(123))

    def test_activation_attempts_are_throttled(self):
        for _ in range(6):
            self.bot.handle(self.message("/promo INVALID"))
        self.assertIn("Too many activation attempts", self.bot.send.call_args.args[1])
