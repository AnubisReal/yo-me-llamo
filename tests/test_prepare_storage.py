from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import tempfile
import prepare_storage

class PrepareStorageTests(TestCase):
    def test_scoped_to_data_and_tmp_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'project';root.mkdir()
            (root/'data').mkdir();(root/'data'/'.instance.lock').write_text('')
            (root/'other.txt').write_text('leave alone')
            outside=Path(directory)/'media';outside.mkdir();video=outside/'E072.mp4';video.write_bytes(b'video')
            (root/'data'/'shortcut').symlink_to(outside,target_is_directory=True)
            with patch.object(prepare_storage.os,'chown') as chown,patch.object(prepare_storage.os,'chmod'):
                prepare_storage.prepare(root,1000,1000)
            targets=[call.args[0] for call in chown.call_args_list]
            self.assertIn(root/'data'/'.instance.lock',targets)
            self.assertIn(root/'tmp',targets)
            self.assertNotIn(video,targets)
            self.assertNotIn(root/'data'/'shortcut',targets)
            self.assertNotIn(root/'other.txt',targets)
            self.assertEqual(video.read_bytes(),b'video')
    def test_rejects_symlink_as_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'project';root.mkdir();outside=Path(directory)/'outside';outside.mkdir()
            (root/'data').symlink_to(outside,target_is_directory=True)
            with patch.object(prepare_storage.os,'chown'),patch.object(prepare_storage.os,'chmod'):
                with self.assertRaises(ValueError):prepare_storage.prepare(root,1000,1000)
