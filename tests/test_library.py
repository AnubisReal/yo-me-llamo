import tempfile
from pathlib import Path
import unittest
import app
from library import episode_number, inspect_library

class FilenameTests(unittest.TestCase):
    def test_names_from_user_screenshot(self):
        self.assertEqual(episode_number('Capítulo 72 Yo Me Llamo, completo y gratis - 720p.mp4'),72)
        self.assertEqual(episode_number('Capítulo 71 Yo Me Llamo Elder Dayán - 720p.mp4'),71)
    def test_formats_and_quality_are_not_episode_numbers(self):
        for name in ['Yo me llamo - 2026 - E075.mp4','Yo.Me.Llamo.S01E075.720p.mkv','75 - episodio.mp4']:
            self.assertEqual(episode_number(name),75)
        for name in ['720p.mp4','Yo me llamo 720p.mp4','archivo 1080p.mp4','E075.partial.mp4','E075 - muestra.mp4']:
            self.assertIsNone(episode_number(name))
    def test_other_seasons_and_partials_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'Temporada 2025').mkdir()
            for path in [root/'Capítulo 72 Yo Me Llamo - 720p.mp4',root/'E073.partial.mp4',root/'Temporada 2025'/'E100.mp4']:
                path.write_bytes(b'video')
            result=inspect_library(root,2026)
            self.assertEqual(result['count'],1);self.assertEqual(result['last_episode'],72)
    def test_title_without_number_matches_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'Amparo y Aurelio estaban jugando y el lobo perdió la cola - 720p.mp4').write_bytes(b'video')
            result=inspect_library(directory,2026,[{'number':74,'title':'Capítulo 74 Yo Me Llamo : Amparo y Aurelio estaban jugando y el lobo perdió la cola'}])
            self.assertEqual(result['last_episode'],74)

class SettingsTests(unittest.TestCase):
    def config(self,root):
        return {'data_dir':str(root/'data'),'output_dir':str(root/'Series'/'Yo me llamo'),
                'library_root':str(root/'Series'),'season':2026,'min_episode':75,
                'check_interval_minutes':15,'max_height':720,'auto_download':True,'require_setup':True}
    def test_continue_from_existing_72_and_persist_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=self.config(root);monitor=app.Monitor(config)
            (monitor.dest/'E075.mp4').write_bytes(b'video')
            destination=root/'Series'/'Otra carpeta';destination.mkdir()
            for number in [71,72]:
                (destination/f'Capítulo {number} Yo Me Llamo, completo y gratis - 720p.mp4').write_bytes(b'video')
            with monitor.connect() as db:
                for number in range(71,76):
                    db.execute('INSERT INTO episodes(url,number,title,season,status) VALUES (?,?,?,?,?)',(str(number),number,str(number),'2026','downloaded' if number==75 else 'available'))
            monitor.queue_automatic('75');self.assertTrue(monitor.work.empty())
            preview=monitor.inspect_destination(destination)
            self.assertEqual(preview['last_episode'],72)
            monitor.save_settings({'output_dir':str(destination),'start_episode':73,'auto_download':True,'check_interval_minutes':10})
            for number in range(71,76):monitor.queue_automatic(str(number))
            self.assertEqual([monitor.work.get_nowait() for _ in range(3)],['73','74','75'])
            self.assertTrue(monitor.work.empty())
            with monitor.connect() as db:
                self.assertEqual([r[0] for r in db.execute("SELECT number FROM episodes WHERE status='downloaded' ORDER BY number")],[71,72])
            restarted=app.Monitor(config)
            self.assertEqual(restarted.dest,destination.resolve())
            self.assertTrue(restarted.settings_configured)
            self.assertEqual(restarted.config['check_interval_minutes'],10)
            restarted.sync_library();self.assertEqual(restarted.resume_after,72)
    def test_folder_traversal_and_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);monitor=app.Monitor(self.config(root))
            with self.assertRaises(ValueError):monitor.inspect_destination(root/'data')
            outside=root/'private';outside.mkdir()
            link=root/'Series'/'escape';link.symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):monitor.inspect_destination(link)
            self.assertNotIn('escape',[f['name'] for f in monitor.directories()['folders']])
    def test_destination_change_blocked_during_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);monitor=app.Monitor(self.config(root));monitor.settings_configured=True
            with monitor.connect() as db:db.execute("INSERT INTO episodes(url,number) VALUES ('75',75)")
            monitor.enqueue('75')
            with self.assertRaises(ValueError):monitor.save_settings({'output_dir':str(monitor.dest)})

if __name__=='__main__':unittest.main()
