import tempfile
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import app

class ParsingTests(unittest.TestCase):
    def test_main_video_not_recommendation(self):
        source='''<script type="application/ld+json">{"@type":"TVEpisode","name":"Capítulo 75","episodeNumber":75,"partOfSeason":{"seasonNumber":"2026"},"video":{"contentUrl":"https://mdstrm.com/video/6aee76186f7d48c6e52a5957.m3u8","duration":"PT1H6M2S"}}</script><div data-video-id="aaaaaaaaaaaaaaaaaaaaaaaa"></div>'''
        row=app.episode_from_html(source,'https://www.caracoltv.com/episode')
        self.assertEqual(row['duration'],3962)
        self.assertEqual(row['season'],'2026')
        self.assertIn('6aee7618',row['hls'])
    def test_relative_variants(self):
        rows=app.variants('#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=640x360\n360/index.m3u8\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\n720/index.m3u8','https://cdn.example/master.m3u8')
        self.assertEqual(rows[1],{'width':1280,'height':720,'url':'https://cdn.example/720/index.m3u8'})
    def test_non_hls_response(self):
        with self.assertRaises(ValueError):app.variants('<html>blocked</html>','https://example.com')
    def test_restart_and_queue_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'data_dir':directory,'output_dir':directory,'season':2026}
            monitor=app.Monitor(config)
            with monitor.connect() as db:
                db.execute("INSERT INTO episodes(url,status) VALUES ('test','available')")
            monitor.enqueue('test');monitor.enqueue('test')
            self.assertEqual(monitor.work.qsize(),1)
            restarted=app.Monitor(config)
            with restarted.connect() as db:
                self.assertEqual(db.execute('SELECT status FROM episodes').fetchone()[0],'queued')
    def test_cannot_queue_unknown_url(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=app.Monitor({'data_dir':directory,'output_dir':directory,'season':2026})
            with self.assertRaises(ValueError): monitor.enqueue('https://untrusted.example/video')

class DiscoveryTests(unittest.TestCase):
    def test_known_episode_playlist_and_load_more_are_traversed(self):
        base='https://www.caracoltv.com/yo-me-llamo/capitulos/'
        url=lambda n:base+f'capitulo-{n}-yo-me-llamo-test'
        def source(n, extra=''):
            item={'@type':'TVEpisode','name':f'Capítulo {n}','episodeNumber':n,
                  'partOfSeason':{'seasonNumber':'2026'},'video':{
                  'contentUrl':'https://mdstrm.com/video/6aee76186f7d48c6e52a5957.m3u8','duration':'PT1H'}}
            return '<script type="application/ld+json">'+json.dumps(item)+'</script>'+extra
        pages={app.LIST_URL:f'<a href="{url(75)}">75</a>',
               url(75):source(75,f'<div data-episode-url="{url(74)}"></div><button data-url="?p=list.2">CARGAR MÁS</button>'),
               url(75)+'?p=list.2':f'<a href="{url(68)}">68</a><button data-url="?p=list.3">CARGAR MÁS</button>',
               url(75)+'?p=list.3':f'<a href="{url(67)}">67</a>'}
        for n in range(69,75): pages[url(n)]=source(n,f'<div data-episode-url="{url(n-1)}"></div>')
        for n in [67,68]: pages[url(n)]=source(n)
        with tempfile.TemporaryDirectory() as directory:
            config={'data_dir':directory,'output_dir':directory,'max_pages':5,'max_episode_pages':30,
                    'season':2026,'auto_download':False}
            monitor=app.Monitor(config)
            saved=Path(directory)/'Yo me llamo - 2026 - E075.mp4';saved.write_bytes(b'video')
            # El 75 ya estaba guardado: su estado y ruta deben conservarse.
            with monitor.connect() as db:
                db.execute("INSERT INTO episodes(url,number,season,status,path) VALUES (?,75,'2026','downloaded','saved.mp4')",(url(75),))
            def fetch(link):return pages[link],link
            with patch.object(monitor,'fetch',side_effect=fetch) as request:
                monitor.scan()
            self.assertIsNone(monitor.last_error)
            with monitor.connect() as db:
                self.assertEqual([r[0] for r in db.execute('SELECT number FROM episodes ORDER BY number')],list(range(67,76)))
                row=db.execute('SELECT status,path FROM episodes WHERE number=75').fetchone()
                self.assertEqual(tuple(row),('downloaded',str(saved.resolve())))
            calls=[c.args[0] for c in request.call_args_list]
            self.assertIn(url(75),calls)
            self.assertEqual(calls.count(url(74)),1)
            self.assertIn(url(75)+'?p=list.3',calls)


class StorageTests(unittest.TestCase):
    def test_publish_between_disks(self):
        import errno
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'work'/'episode.partial.mp4'
            final=Path(directory)/'media'/'episode.mp4'
            source.parent.mkdir();source.write_bytes(b'complete video'*3000)
            expected=source.read_bytes();original=Path.replace
            def replace(path,target):
                if path==source:raise OSError(errno.EXDEV,'different devices')
                return original(path,target)
            with patch.object(Path,'replace',new=replace): app.publish_file(source,final)
            self.assertEqual(final.read_bytes(),expected)
            self.assertFalse(source.exists())
            self.assertFalse(final.with_name('.episode.mp4.copying').exists())
    def test_failed_copy_keeps_source_and_hides_incomplete_output(self):
        import errno
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'episode.partial.mp4';source.write_bytes(b'video')
            final=Path(directory)/'media'/'episode.mp4'
            with patch.object(Path,'replace',side_effect=OSError(errno.EXDEV,'different devices')):
                with patch.object(app.shutil,'copyfileobj',side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):app.publish_file(source,final)
            self.assertTrue(source.exists());self.assertFalse(final.exists())
            self.assertFalse(final.with_name('.episode.mp4.copying').exists())

class AutomaticTests(unittest.TestCase):
    def test_new_episode_is_queued_while_existing_saved_episode_is_preserved(self):
        base='https://www.caracoltv.com/yo-me-llamo/capitulos/'
        url=lambda n:base+f'capitulo-{n}-test'
        def source(n,extra=''):
            data={'@type':'TVEpisode','name':str(n),'episodeNumber':n,'partOfSeason':{'seasonNumber':'2026'},
                  'video':{'contentUrl':'https://mdstrm.com/video/6aee76186f7d48c6e52a5957.m3u8','duration':'PT1H'}}
            return '<script type="application/ld+json">'+json.dumps(data)+'</script>'+extra
        pages={app.LIST_URL:f'<a href="{url(75)}">75</a>',
               url(75):source(75,f'<div data-episode-url="{url(76)}"></div>'),url(76):source(76)}
        with tempfile.TemporaryDirectory() as directory:
            config={'data_dir':directory,'output_dir':directory,'max_pages':5,'season':2026,
                    'auto_download':True,'min_episode':75,'season':2026}
            saved=Path(directory)/'Yo me llamo - 2026 - E075.mp4';saved.write_bytes(b'video')
            monitor=app.Monitor(config);monitor.last_full_scan=app.time.time()
            with monitor.connect() as db:
                db.execute("INSERT INTO episodes(url,number,status) VALUES (?,75,'downloaded')",(url(75),))
            with patch.object(monitor,'fetch',side_effect=lambda u:(pages[u],u)):monitor.scan()
            self.assertIsNone(monitor.last_error)
            self.assertEqual(monitor.work.qsize(),1)
            self.assertEqual(monitor.work.get_nowait(),url(76))
            with monitor.connect() as db:
                self.assertEqual(db.execute('SELECT status FROM episodes WHERE number=75').fetchone()[0],'downloaded')
                self.assertEqual(db.execute('SELECT status FROM episodes WHERE number=76').fetchone()[0],'queued')
    def test_retry_limit_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=app.Monitor({'data_dir':directory,'output_dir':directory,'auto_download':True,'min_episode':75,'season':2026})
            with monitor.connect() as db:
                db.execute("INSERT INTO episodes(url,number,status,attempts,next_retry) VALUES ('test',76,'error',3,0)")
            monitor.queue_automatic('test');self.assertEqual(monitor.work.qsize(),0)
            monitor.update('test',attempts=1,next_retry=app.time.time()+3600)
            monitor.queue_automatic('test');self.assertEqual(monitor.work.qsize(),0)
            monitor.update('test',next_retry=0)
            monitor.queue_automatic('test');self.assertEqual(monitor.work.qsize(),1)

if __name__=='__main__':unittest.main()
