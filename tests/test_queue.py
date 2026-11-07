import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import app


def wait_for(predicate,timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return True
        time.sleep(.01)
    return False

class QueueTests(TestCase):
    def monitor(self,directory,workers=2,automatic=True):
        return app.Monitor({'data_dir':directory,'output_dir':directory,'season':2026,'min_episode':73,
                            'check_interval_minutes':15,'auto_download':automatic,'download_workers':workers,'retry_seconds':20})
    def insert(self,monitor,number,status='available'):
        with monitor.connect() as db:
            db.execute('INSERT INTO episodes(url,number,season,status) VALUES (?,?,?,?)',(str(number),number,'2026',status))
    def status(self,monitor,number):
        with monitor.connect() as db:return db.execute('SELECT status FROM episodes WHERE url=?',(str(number),)).fetchone()[0]
    def test_parallel_limit_can_change_without_stopping_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory);release=threading.Event();lock=threading.Lock();running=[0];peak=[0]
            for number in range(73,77):self.insert(monitor,number);monitor.enqueue(str(number))
            def download(url):
                with lock:running[0]+=1;peak[0]=max(peak[0],running[0])
                try:
                    if not release.wait(4):raise RuntimeError('test timeout')
                    monitor.update(url,status='downloaded',progress=100)
                finally:
                    with lock:running[0]-=1
            with patch.object(monitor,'download',side_effect=download):
                try:
                    monitor.start_workers(slots=4)
                    self.assertTrue(wait_for(lambda:len(monitor.active)==2))
                    self.assertEqual(peak[0],2)
                    monitor.set_concurrency(3)
                    self.assertTrue(wait_for(lambda:len(monitor.active)==3))
                    self.assertEqual(peak[0],3)
                    release.set()
                    self.assertTrue(wait_for(lambda:monitor.work.unfinished_tasks==0))
                    self.assertTrue(all(self.status(monitor,n)=='downloaded' for n in range(73,77)))
                finally:
                    release.set();monitor.stop_workers()
                    for thread in monitor.worker_threads:thread.join(1)
    def test_failed_first_item_retries_after_later_episode_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory,workers=1,automatic=False);calls={}
            for number in (73,74):self.insert(monitor,number);monitor.enqueue(str(number))
            def download(url,sample,proxy):
                calls[url]=calls.get(url,0)+1
                if url=='73' and calls[url]==1:raise app.DownloadError('HTTP 403 temporal')
                monitor.update(url,status='downloaded',progress=100)
                return 'saved.mp4'
            with patch.object(monitor,'_download',side_effect=download):
                try:
                    monitor.start_workers(slots=2)
                    self.assertTrue(wait_for(lambda:monitor.work.unfinished_tasks==0))
                    self.assertEqual(self.status(monitor,73),'error')
                    self.assertEqual(self.status(monitor,74),'downloaded')
                    monitor.library['last_episode']=74
                    monitor.update('73',next_retry=time.time()-1)
                    monitor.retry_due()
                    self.assertTrue(wait_for(lambda:self.status(monitor,73)=='downloaded'))
                    self.assertEqual(calls['73'],2)
                    self.assertTrue(any(t.is_alive() for t in monitor.worker_threads))
                finally:
                    monitor.stop_workers()
                    for thread in monitor.worker_threads:thread.join(1)
    def test_completed_later_episode_does_not_skip_earlier_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory);monitor.start_after=72;monitor.library.update(count=1,last_episode=75)
            self.insert(monitor,73,'error');monitor.update('73',attempts=1,next_retry=time.time()-1)
            monitor.queue_automatic('73')
            self.assertEqual(monitor.work.get_nowait(),'73')
    def test_queued_manual_item_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory,automatic=False);self.insert(monitor,70);monitor.enqueue('70')
            restarted=self.monitor(directory,automatic=False)
            with patch.object(restarted,'download',side_effect=lambda url:restarted.update(url,status='downloaded')):
                try:
                    restarted.start_workers(slots=1)
                    self.assertTrue(wait_for(lambda:self.status(restarted,70)=='downloaded'))
                finally:
                    restarted.stop_workers()
                    for thread in restarted.worker_threads:thread.join(1)
    def test_completed_temporary_is_moved_without_downloading_again(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory);self.insert(monitor,73)
            monitor.update('73',duration=3600,height=720)
            name='Yo me llamo - 2026 - E073'
            partial=monitor.temp/(name+'.partial.mp4');partial.write_bytes(b'complete-video'*1000)
            (monitor.temp/(name+'.ready')).write_text(str(partial.stat().st_size))
            with patch.object(monitor,'fetch',side_effect=AssertionError('must not request the network')):
                final=monitor.download('73')
            self.assertTrue(final.exists());self.assertFalse(partial.exists())
            self.assertEqual(self.status(monitor,73),'downloaded')
    def test_error_messages_do_not_publish_signed_urls(self):
        message=app.download_error(RuntimeError('Failed https://cdn.example/video?access_token=secret'))
        self.assertNotIn('secret',message)
        self.assertFalse(app.retryable(PermissionError('denied')))

    def test_orphaned_queued_item_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory);self.insert(monitor,73,'queued')
            monitor.retry_due()
            self.assertEqual(monitor.work.get_nowait(),'73')
    def test_geoblock_has_actionable_error(self):
        import io
        from urllib.error import HTTPError
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory)
            route=Mock();route.open.side_effect=HTTPError('https://example.com',403,'Forbidden',None,io.BytesIO(b'{"data":"GEO"}'))
            with self.assertRaisesRegex(app.DownloadError,'Colombia'):monitor.fetch('https://example.com',opener=route)
    def test_ffmpeg_proxy_transport_is_allowed(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            monitor=self.monitor(directory);monitor.config['max_height']=720;self.insert(monitor,73)
            monitor.update('73',hls='https://example.com/master.m3u8',duration=8)
            master='#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\nhttps://example.com/720.m3u8'
            media='#EXTM3U\n#EXTINF:8,\nsegment.ts\n#EXT-X-ENDLIST'
            def start(command,**options):
                self.assertIn('httpproxy',command[command.index('-protocol_whitelist')+1].split(','))
                self.assertEqual(command[command.index('-http_proxy')+1],'http://proxy.example:3128')
                Path(command[-1]).write_bytes(b'x'*12000)
                process=MagicMock();process.__enter__.return_value=process
                process.stdout=iter(['out_time_us=8000000\n']);process.wait.return_value=0;process.poll.return_value=0
                return process
            with patch.object(monitor,'fetch',side_effect=[(master,'https://example.com/master.m3u8'),(media,'https://example.com/720.m3u8')]),patch.object(app,'ffmpeg_path',return_value='ffmpeg'),patch.object(app.subprocess,'Popen',side_effect=start):
                final=monitor._download('73',8,'http://proxy.example:3128')
            self.assertTrue(final.exists())
