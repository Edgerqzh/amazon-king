"""更新.bat: pulls new commits, keeps local edits and ignored data safe."""
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import update


def run(cwd, *args):
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'init.defaultBranch=main'] + list(args),
                   cwd=cwd, check=True, capture_output=True)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp());self.addCleanup(lambda: subprocess.run(['cmd', '/c', 'rmdir', '/s', '/q', str(self.tmp)], capture_output=True))
        remote, self.author, self.user = self.tmp / 'remote.git', self.tmp / 'author', self.tmp / 'user'
        run(self.tmp, 'init', '--bare', str(remote))
        run(self.tmp, 'clone', str(remote), str(self.author))
        (self.author / '.gitignore').write_text('.data/\n');(self.author / 'app.py').write_text('v1\n')
        run(self.author, 'add', '.');run(self.author, 'commit', '-m', 'first');run(self.author, 'push', 'origin', 'HEAD:main')
        run(self.tmp, 'clone', '-b', 'main', str(remote), str(self.user))
        (self.user / '.data').mkdir();(self.user / '.data' / 'accounts.sqlite3').write_text('keep')

    def update(self):
        out = io.StringIO()
        with patch.object(update, 'HERE', self.user), contextlib.redirect_stdout(out):code = update.main()
        return code, out.getvalue()

    def test_pulls_new_version_and_keeps_data(self):
        (self.author / 'app.py').write_text('v2\n');run(self.author, 'commit', '-am', '新功能');run(self.author, 'push', 'origin', 'HEAD:main')
        code, text = self.update()
        self.assertEqual(code, 0);self.assertIn('新功能', text)
        self.assertEqual((self.user / 'app.py').read_text(), 'v2\n')
        self.assertEqual((self.user / '.data' / 'accounts.sqlite3').read_text(), 'keep')
        self.assertIn('已经是最新版', self.update()[1])

    def test_local_edits_block_update(self):
        (self.author / 'app.py').write_text('v2\n');run(self.author, 'commit', '-am', 'x');run(self.author, 'push', 'origin', 'HEAD:main')
        (self.user / 'app.py').write_text('mine\n')
        code, text = self.update()
        self.assertEqual(code, 1);self.assertIn('没有更新', text)
        self.assertEqual((self.user / 'app.py').read_text(), 'mine\n')


if __name__ == '__main__':
    unittest.main()
