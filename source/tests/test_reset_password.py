"""Resetting a password on the server computer."""
from pathlib import Path
import tempfile
import unittest

from template_manager.auth import Auth

OLD = 'old-password-' + 'x' * 8
NEW = 'new-password-' + 'y' * 8


class ResetTests(unittest.TestCase):
    def test_new_password_works_old_one_and_sessions_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Auth(Path(tmp) / 'accounts.sqlite3')
            auth.create('admin', OLD, 'admin')
            token = auth.login('admin', OLD, '127.0.0.1')
            auth.set_password('admin', NEW)
            self.assertIsNone(auth.user(token))
            with self.assertRaises(ValueError):auth.login('admin', OLD, '127.0.0.1')
            self.assertEqual(auth.user(auth.login('admin', NEW, '127.0.0.1'))['role'], 'admin')
            with self.assertRaises(ValueError):auth.set_password('admin', 'short')
            with self.assertRaises(ValueError):auth.set_password('nobody', NEW)


if __name__ == '__main__':
    unittest.main()
