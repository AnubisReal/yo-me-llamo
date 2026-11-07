import tempfile
from unittest import TestCase
from unittest.mock import patch
import app

class ProxyFallbackTests(TestCase):
    def monitor(self,directory):
        monitor=app.Monitor({'data_dir':directory,'output_dir':directory,'season':2026})
        with monitor.connect() as db:db.execute("INSERT INTO episodes(url,number) VALUES ('test',75)")
        return monitor
    def test_download_restarts_on_fallback_route(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(app.os.environ,{'YML_PROXY':'','YML_PROXY_FALLBACK':'http://proxy.example:3128'}):monitor=self.monitor(directory)
            with patch.object(monitor,'_download',side_effect=[OSError('direct denied'),'saved.mp4']) as download:
                self.assertEqual(monitor.download('test'),'saved.mp4')
            self.assertEqual([c.args[2] for c in download.call_args_list],['','http://proxy.example:3128'])
            with monitor.connect() as db:self.assertEqual(db.execute('SELECT attempts FROM episodes').fetchone()[0],1)
    def test_explicit_proxy_does_not_switch_exit_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(app.os.environ,{'YML_PROXY':'http://primary.example:3128','YML_PROXY_FALLBACK':'http://backup.example:3128'}):monitor=self.monitor(directory)
            with patch.object(monitor,'_download',side_effect=OSError('unavailable')) as download:
                with self.assertRaises(OSError):monitor.download('test')
            self.assertEqual(download.call_count,1)
            self.assertEqual(download.call_args.args[2],'http://primary.example:3128')
