#!/usr/bin/env python3
"""Monitor local de capítulos de Caracol. Python 3.10+ y FFmpeg."""
import argparse
import errno
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import html
import fcntl
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit, parse_qs
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parent
LIST_URL = 'https://www.caracoltv.com/yo-me-llamo/capitulos'
UA = 'YoMeLlamoMonitor/1.0'
from library import checked_directory, inspect_library

class Page(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []; self.episode_urls = []; self.next_pages = []; self.objects = []; self.structured = False
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.append((attrs['href'], attrs.get('rel', ''), attrs.get('class', '')))
        if attrs.get('data-episode-url'):
            self.episode_urls.append(attrs['data-episode-url'])
        if tag == 'button' and attrs.get('data-url'):
            self.next_pages.append(attrs['data-url'])
        if tag == 'a' and ('next' in attrs.get('rel', '').split() or 'nextPage' in attrs.get('class', '')):
            self.next_pages.append(attrs['href'])
        if tag == 'script':
            self.structured = attrs.get('type') == 'application/ld+json'
    def handle_endtag(self, tag):
        if tag == 'script': self.structured = False
    def handle_data(self, data):
        if self.structured:
            try: self.objects.append(json.loads(data))
            except ValueError: pass

def objects(value):
    if isinstance(value, list):
        for child in value: yield from objects(child)
    elif isinstance(value, dict):
        yield value
        for child in value.values(): yield from objects(child)

def episode_from_html(source, url):
    page = Page(); page.feed(source)
    for item in objects(page.objects):
        if item.get('@type') == 'TVEpisode':
            video = item.get('video', {})
            stream = video.get('contentUrl', '')
            if not re.fullmatch(r'https://mdstrm\.com/video/[a-f0-9]{24}\.m3u8', stream):
                raise ValueError('El episodio no publica un HLS compatible.')
            duration = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', video.get('duration', ''))
            seconds = sum(int(v or 0) * scale for v, scale in zip(duration.groups(), [3600,60,1])) if duration else 0
            return dict(url=url, title=html.unescape(item['name']).strip(), number=int(item.get('episodeNumber') or 0),
                        season=str(item.get('partOfSeason', {}).get('seasonNumber', '')),
                        published=item.get('datePublished', ''), duration=seconds, hls=stream)
    raise ValueError('No se encontraron los metadatos del capítulo.')

def variants(source, base):
    result = []; pending = None
    if not source.lstrip().startswith('#EXTM3U'): raise ValueError('La respuesta no es una lista HLS.')
    for line in source.splitlines():
        line = line.strip()
        if line.startswith('#EXT-X-STREAM-INF:'):
            size = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            pending = (int(size[1]), int(size[2])) if size else (0,0)
        elif line and not line.startswith('#') and pending is not None:
            result.append({'width':pending[0], 'height':pending[1], 'url':urljoin(base,line)})
            pending = None
    return result

def ffmpeg_path():
    found = os.environ.get('FFMPEG_BINARY') or shutil.which('ffmpeg')
    if found: return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError('Falta FFmpeg. En Ubuntu: sudo apt install ffmpeg')

def publish_file(source, destination):
    """Publica de forma atómica, también entre dos discos distintos."""
    source = Path(source); destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV: raise
    staging = destination.with_name('.' + destination.name + '.copying')
    try:
        with source.open('rb') as src, staging.open('wb') as dst:
            shutil.copyfileobj(src, dst, length=1024*1024)
            dst.flush(); os.fsync(dst.fileno())
        if staging.stat().st_size != source.stat().st_size:
            raise RuntimeError('La copia al disco de destino está incompleta.')
        staging.replace(destination)
        source.unlink()
    finally:
        staging.unlink(missing_ok=True)

class Monitor:
    def __init__(self, config):
        self.config = dict(config)
        self.library_root=Path(config.get('library_root',config['output_dir'])).expanduser().resolve()
        self.library={'count':0,'last_episode':0,'episodes':[],'unrecognized_count':0,'unrecognized':[]}
        self.library_error=None
        self.resume_after=0
        self.settings_configured=not config.get('require_setup',False)
        self.start_after=0
        self.data = Path(config['data_dir']).expanduser().resolve(); self.data.mkdir(parents=True, exist_ok=True)
        self.dest = Path(config['output_dir']).expanduser().resolve(); self.dest.mkdir(parents=True, exist_ok=True)
        self.temp = Path(config.get('temp_dir', str(self.data / 'tmp'))).expanduser().resolve()
        self.temp.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'catalog.sqlite3'
        self.proxy = os.environ.get('YML_PROXY', '')
        self.fallback_proxy = os.environ.get('YML_PROXY_FALLBACK','')
        if any(value and urlsplit(value).scheme not in ('http','https') for value in (self.proxy,self.fallback_proxy)):
            raise ValueError('YML_PROXY debe ser un proxy HTTP o HTTPS.')
        self.opener = build_opener(ProxyHandler({'http':self.proxy, 'https':self.proxy} if self.proxy else {}))
        self.fallback_opener = build_opener(ProxyHandler({'http':self.fallback_proxy,'https':self.fallback_proxy})) if self.fallback_proxy else None
        self.scan_lock = threading.Lock(); self.pending = set(); self.pending_lock = threading.Lock()
        self.work = queue.Queue(); self.last_error = None; self.last_scan = None
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS episodes (
                url TEXT PRIMARY KEY, title TEXT, number INTEGER, season TEXT, published TEXT,
                duration INTEGER, hls TEXT, status TEXT DEFAULT 'available', progress REAL DEFAULT 0,
                path TEXT, error TEXT, height INTEGER, attempts INTEGER DEFAULT 0, next_retry REAL DEFAULT 0)''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(episodes)')}
            for name, definition in [('attempts','INTEGER DEFAULT 0'),('next_retry','REAL DEFAULT 0')]:
                if name not in columns: db.execute(f'ALTER TABLE episodes ADD COLUMN {name} {definition}')
            db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
            state = dict(db.execute('SELECT key,value FROM state').fetchall())
            self.last_scan = state.get('last_scan')
            self.last_full_scan = float(state.get('last_full_scan', 0))
            db.execute('CREATE TABLE IF NOT EXISTS local_files (number INTEGER,season TEXT,path TEXT,PRIMARY KEY(number,season))')
            saved=json.loads(state.get('settings','{}'))
            if saved:
                candidate=Path(saved['output_dir']).resolve()
                if candidate.is_relative_to(self.library_root):
                    self.dest=candidate;self.config.update(saved)
                    self.settings_configured=True;self.start_after=int(saved.get('start_after',0))
                else:self.library_error='La carpeta guardada queda fuera del volumen disponible. Revísala en Ajustes.'
            db.execute("UPDATE episodes SET status='available', error='La tarea se interrumpió; puedes reintentar.' WHERE status IN ('queued','downloading','moving')")
        if self.settings_configured:self.sync_library()
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=20); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()
    def fetch(self, url, limit=8_000_000, opener=None):
        routes=[opener] if opener is not None else [self.opener]+([self.fallback_opener] if self.fallback_opener and not self.proxy else [])
        for index, route in enumerate(routes):
            try:
                with route.open(Request(url, headers={'User-Agent':UA,'Referer':LIST_URL}), timeout=40) as response:
                    data=response.read(limit+1)
                    if len(data)>limit:raise ValueError('Respuesta demasiado grande.')
                    return data.decode('utf-8',errors='replace'),response.url
            except Exception:
                if index==len(routes)-1:raise
    def scan(self):
        if not self.scan_lock.acquire(blocking=False): return
        errors = []; pending = []; scheduled = set(); fetched = set()
        budget = int(self.config.get('max_episode_pages', 150))
        pagination_done = False
        full_scan = time.time()-self.last_full_scan >= self.config.get('full_scan_interval_hours',24)*3600
        with self.connect() as db:
            known_urls = {r['url'] for r in db.execute('SELECT url FROM episodes')}
        def episode_url(value, base):
            u = urlsplit(urljoin(base, value))
            if u.scheme == 'https' and u.hostname == 'www.caracoltv.com' and u.path.startswith('/yo-me-llamo/capitulos/capitulo-'):
                return u._replace(query='', fragment='').geturl()
        def collect(page, base, include_known=False):
            for value in [h for h, _, _ in page.links] + page.episode_urls:
                link = episode_url(value, base)
                if link and link not in scheduled and (full_scan or include_known or link not in known_urls):
                    scheduled.add(link); pending.append(link)
        def parse(source):
            page = Page(); page.feed(source); return page
        def read_episode(link):
            try:
                source, actual = self.fetch(link)
                page = parse(source)
                try: item = episode_from_html(source, link)
                except ValueError: return link, None, None, None
                return link, item, page, None
            except Exception: return link, None, None, 'No se pudo consultar un capítulo; se volverá a intentar.'
        try:
            source, actual = self.fetch(LIST_URL)
            page = parse(source); collect(page, actual, include_known=True)
            # Se vuelven a leer los capítulos conocidos: sus listas pueden incluir novedades.
            newest = None
            with ThreadPoolExecutor(max_workers=4) as pool:
                while pending and len(fetched) < budget:
                    batch = pending[:min(4, budget-len(fetched))]; del pending[:len(batch)]
                    fetched.update(batch)
                    for link, item, page, error in pool.map(read_episode, batch):
                        if error: errors.append(error)
                        if not item or item['season'] != str(self.config['season']): continue
                        with self.connect() as db:
                            db.execute('''INSERT INTO episodes (url,title,number,season,published,duration,hls)
                                VALUES (:url,:title,:number,:season,:published,:duration,:hls)
                                ON CONFLICT(url) DO UPDATE SET title=excluded.title,number=excluded.number,
                                season=excluded.season,published=excluded.published,duration=excluded.duration,hls=excluded.hls''', item)
                        collect(page, link)
                        # Una sola cadena de «CARGAR MÁS», desde el episodio más reciente.
                        if newest is None or item['number'] > newest[0]:
                            newest = (item['number'], link, page)
                    if newest is not None:
                        _, base, first_page = newest
                        newest = None
                        # Solo la primera vez: las demás playlists se recorren como grafo.
                        if not pagination_done:
                            pagination_done = True
                            more_page = first_page; seen_more = {base}
                            for _ in range(self.config['max_pages'] if full_scan else 2):
                                more = next((urljoin(base, v) for v in more_page.next_pages), None)
                                if not more or more in seen_more: break
                                u = urlsplit(more)
                                if u.scheme != 'https' or u.hostname != 'www.caracoltv.com' or u.path != urlsplit(base).path: break
                                seen_more.add(more)
                                source, _ = self.fetch(more)
                                more_page = parse(source); collect(more_page, base)
                            else:
                                if full_scan and more_page.next_pages: errors.append('Se alcanzó el límite de páginas de Cargar más; aumenta max_pages para ampliar el catálogo.')
            if pending: errors.append('Se alcanzó el límite de capítulos consultados; aumenta max_episode_pages.')
            self.last_scan = dt.datetime.now(dt.timezone.utc).isoformat()
            self.last_error = ' '.join(dict.fromkeys(errors)) or None
            print(f'Revisión terminada: {len(fetched)} páginas de capítulos consultadas.' + (' Con avisos.' if errors else ''),flush=True)
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO state VALUES ('last_scan',?)", (self.last_scan,))
                if full_scan and not errors:
                    self.last_full_scan = time.time()
                    db.execute("INSERT OR REPLACE INTO state VALUES ('last_full_scan',?)", (str(self.last_full_scan),))
                rows = db.execute('SELECT url FROM episodes ORDER BY number').fetchall()
            if self.settings_configured:
                self.sync_library()
            for row in rows: self.queue_automatic(row['url'])
        except Exception:
            self.last_error = 'No se pudo completar la consulta de Caracol. Revisa la conexión o el proxy y vuelve a intentar.'
        finally: self.scan_lock.release()
    def queue_automatic(self, url):
        if not self.config.get('auto_download',False) or not self.settings_configured or self.library_error: return
        with self.connect() as db:
            row = db.execute('SELECT * FROM episodes WHERE url=?',(url,)).fetchone()
        floor=max(self.start_after,self.library['last_episode']) if self.library['count'] or self.start_after else self.config.get('min_episode',75)-1
        self.resume_after=floor
        if not row or row['number']<=floor: return
        if row['status']=='available' or (row['status']=='error' and row['attempts']<self.config.get('max_attempts',3) and row['next_retry']<=time.time()):
            self.enqueue(url, automatic=True)
    def inspect_destination(self, value):
        path=checked_directory(value,self.library_root)
        with self.connect() as db:
            catalog=[dict(r) for r in db.execute('SELECT number,title FROM episodes WHERE season=?',(str(self.config['season']),))]
        result=inspect_library(path,self.config['season'],catalog)
        result['output_dir']=str(path)
        return result
    def sync_library(self):
        try:
            result=self.inspect_destination(str(self.dest))
            self.library_error=None
            with self.connect() as db:
                db.execute('DELETE FROM local_files')
                for item in result['episodes']:
                    db.execute('INSERT INTO local_files VALUES (?,?,?)',(item['number'],str(self.config['season']),item['path']))
                db.execute("UPDATE episodes SET status='available',progress=0,path=NULL,error=NULL WHERE status='downloaded' AND season=? AND number NOT IN (SELECT number FROM local_files WHERE season=?)",(str(self.config['season']),str(self.config['season'])))
                for item in result['episodes']:
                    db.execute("UPDATE episodes SET status='downloaded',progress=100,path=?,error=NULL,attempts=0,next_retry=0 WHERE number=? AND season=?",(item['path'],item['number'],str(self.config['season'])))
            self.library=result
            self.resume_after=max(self.start_after,result['last_episode']) if result['count'] or self.start_after else self.config.get('min_episode',75)-1
            return result
        except (ValueError,OSError) as error:
            self.library_error=str(error)
            return None
    def directories(self, value=None):
        current=checked_directory(value or self.library_root,self.library_root)
        try:
            folders=[{'name':p.name,'path':str(p.resolve())} for p in current.iterdir()
                     if p.is_dir() and not p.name.startswith('.') and p.resolve().is_relative_to(self.library_root)]
        except OSError as error:raise ValueError('No se puede leer esta carpeta.') from error
        return {'path':str(current),'root':str(self.library_root),
                'parent':str(current.parent) if current!=self.library_root else None,
                'folders':sorted(folders,key=lambda f:f['name'].lower())}
    def settings_snapshot(self):
        return {'output_dir':str(self.dest),'library_root':str(self.library_root),
                'configured':self.settings_configured,'auto_download':self.config.get('auto_download',True),
                'check_interval_minutes':self.config['check_interval_minutes'],
                'min_episode':self.config.get('min_episode',75),'library':self.library,
                'resume_after':self.resume_after,'error':self.library_error}
    def save_settings(self, values):
        if not self.scan_lock.acquire(blocking=False):raise ValueError('Espera a que termine la revisión de capítulos.')
        try:
            with self.pending_lock:
                if self.pending:raise ValueError('Espera a que terminen las descargas antes de cambiar la carpeta.')
                path=checked_directory(values['output_dir'],self.library_root)
                if not os.access(path,os.W_OK):raise ValueError('No hay permisos de escritura en la carpeta elegida.')
                interval=int(values.get('check_interval_minutes',15))
                first=int(values.get('start_episode',1))
                if not 1<=interval<=1440 or not 1<=first<=10000:raise ValueError('Intervalo o capítulo inicial fuera de rango.')
                automatic=values.get('auto_download',True)
                if not isinstance(automatic,bool):raise ValueError('La descarga automática debe ser true o false.')
                preview=self.inspect_destination(path)
                saved={'output_dir':str(path),'auto_download':automatic,
                       'check_interval_minutes':interval,'min_episode':first,
                       'start_after':max(preview['last_episode'],first-1)}
                with self.connect() as db:
                    db.execute("INSERT OR REPLACE INTO state VALUES ('settings',?)",(json.dumps(saved),))
                self.dest=path;self.config.update(saved);self.start_after=saved['start_after']
                self.settings_configured=True
                self.sync_library()
                return self.settings_snapshot()
        finally:self.scan_lock.release()
    def enqueue(self, url, automatic=False):
        if not self.settings_configured:raise ValueError('Selecciona y guarda la carpeta de destino en Ajustes.')
        with self.pending_lock:
            with self.connect() as db:
                row = db.execute('SELECT * FROM episodes WHERE url=?',(url,)).fetchone()
                if not row: raise ValueError('Capítulo desconocido.')
                if url in self.pending or row['status'] == 'downloaded': return
                if automatic:
                    db.execute("UPDATE episodes SET status='queued',error=NULL,progress=0 WHERE url=?",(url,))
                else:
                    db.execute("UPDATE episodes SET status='queued',error=NULL,progress=0,attempts=0,next_retry=0 WHERE url=?",(url,))
            self.pending.add(url); self.work.put(url)
            print(f"Capítulo {row['number']}: añadido a la cola.",flush=True)
    def update(self, url, **values):
        with self.connect() as db:
            db.execute('UPDATE episodes SET '+','.join(f'{k}=?' for k in values)+' WHERE url=?', (*values.values(),url))
    def download(self, url, sample_seconds=None):
        with self.connect() as db:
            row=db.execute('SELECT attempts FROM episodes WHERE url=?',(url,)).fetchone()
        self.update(url,attempts=row['attempts']+1)
        routes=[self.proxy]
        if not self.proxy and self.fallback_proxy:routes.append(self.fallback_proxy)
        for index, proxy in enumerate(routes):
            try:return self._download(url,sample_seconds,proxy)
            except Exception:
                if index==len(routes)-1:raise
                print('Conexión directa fallida; probando el proxy de respaldo.',flush=True)
    def _download(self, url, sample_seconds=None, proxy=''):
        if not self.settings_configured:raise ValueError('Configura la carpeta de destino en Ajustes.')
        checked_directory(self.dest,self.library_root)
        with self.connect() as db: row = dict(db.execute('SELECT * FROM episodes WHERE url=?',(url,)).fetchone())
        binary = ffmpeg_path()
        route=build_opener(ProxyHandler({'http':proxy,'https':proxy} if proxy else {}))
        master, actual = self.fetch(row['hls'],opener=route); choices = variants(master,actual)
        choices = [v for v in choices if 0 < v['height'] <= self.config['max_height']]
        if not choices: raise RuntimeError('No hay una resolución compatible en el manifiesto.')
        selected = max(choices, key=lambda v:v['height'])
        media, _ = self.fetch(selected['url'],opener=route)
        if not media.lstrip().startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in media:
            raise RuntimeError('El vídeo no es un capítulo HLS completo.')
        if re.search(r'#EXT-X-KEY:(?![^\n]*METHOD=NONE)', media):
            raise RuntimeError('El vídeo está cifrado; esta aplicación no procesa contenido protegido.')
        season_folder = f"Temporada {row['season']}"
        directory = self.dest if self.dest.name.casefold()==season_folder.casefold() else self.dest / season_folder
        directory.mkdir(parents=True,exist_ok=True)
        name = f"Yo me llamo - {row['season']} - E{row['number']:03d}" + (' - muestra' if sample_seconds else '')
        final = directory / (name+'.mp4'); partial = self.temp / (name+'.partial.mp4')
        if final.exists():
            if not sample_seconds: self.update(url,status='downloaded',progress=100,path=str(final),height=selected['height'],error=None,attempts=0,next_retry=0)
            return final
        cmd = [binary,'-hide_banner','-loglevel','error','-nostdin','-y']
        if proxy: cmd += ['-http_proxy',proxy]
        cmd += ['-user_agent',UA,'-referer',LIST_URL,'-rw_timeout','30000000','-protocol_whitelist','http,https,tcp,tls,crypto','-i',selected['url']]
        if sample_seconds: cmd += ['-t',str(sample_seconds)]
        cmd += ['-map','0:v:0','-map','0:a:0?','-c','copy','-movflags','+faststart','-progress','pipe:1','-nostats',str(partial)]
        self.update(url,status='downloading',error=None,height=selected['height'])
        completed_seconds = 0
        try:
            # stderr puede contener URLs firmadas: no se almacena ni se muestra en la interfaz.
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as proc:
                for line in proc.stdout:
                    if line.startswith('out_time_us='):
                        try: completed_seconds = max(completed_seconds,int(line.split('=',1)[1])/1_000_000)
                        except ValueError: continue
                        total = sample_seconds or row['duration']
                        self.update(url,progress=min(99,round(completed_seconds/max(total,1)*100,1)))
                code = proc.wait()
            expected = sample_seconds or row['duration']
            if code or not partial.exists() or partial.stat().st_size < 10000 or (expected and completed_seconds < max(0,expected-2)):
                raise RuntimeError('Descarga incompleta. Revisa la conexión y vuelve a intentar.')
            self.update(url,status='moving',progress=99)
            publish_file(partial,final)
            print(f"Capítulo {row['number']}: archivo completo publicado.",flush=True)
            if not sample_seconds:
                self.update(url,status='downloaded',progress=100,path=str(final),error=None,attempts=0,next_retry=0)
                self.sync_library()
            else: self.update(url,status='available',progress=0)
            return final
        except Exception:
            self.update(url,status='error',next_retry=time.time()+self.config.get('retry_minutes',15)*60,error='Descarga fallida. Se volverá a intentar automáticamente si quedan intentos.')
            raise
    def worker(self):
        while True:
            url = self.work.get()
            try: self.download(url)
            except Exception:
                self.update(url,status='error',next_retry=time.time()+self.config.get('retry_minutes',15)*60,error='Descarga fallida. Se volverá a intentar automáticamente si quedan intentos.')
            finally:
                with self.pending_lock: self.pending.discard(url)
                self.work.task_done()
    def scheduler(self):
        while True:
            self.scan()
            interval=self.config['check_interval_minutes']*60
            for _ in range(interval):
                time.sleep(1)
                if self.config['check_interval_minutes']*60!=interval:break
    def snapshot(self):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT url,title,number,season,published,duration,status,progress,path,error,height FROM episodes ORDER BY published DESC')]
        return dict(episodes=rows,scanning=self.scan_lock.locked(),last_scan=self.last_scan,error=self.last_error,
                    temp_dir=str(self.temp),output_dir=str(self.dest),proxy_enabled=bool(self.proxy or self.fallback_proxy),auto_download=self.config['auto_download'],
                    settings=self.settings_snapshot(),library=self.library,library_error=self.library_error,season=self.config['season'],max_height=self.config['max_height'],check_interval_minutes=self.config['check_interval_minutes'])

def serve(monitor,host,port):
    token = os.environ.get('YML_TOKEN','')
    if host not in ('127.0.0.1','localhost','::1') and not token:
        raise ValueError('Para escuchar en la red configura YML_TOKEN y un proxy inverso HTTPS.')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,status,data,kind='application/json'):
            payload = json.dumps(data,ensure_ascii=False).encode() if kind=='application/json' else data
            self.send_response(status); self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(payload))); self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY')
            self.end_headers(); self.wfile.write(payload)
        def authorized(self):
            return not token or secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+token)
        def do_GET(self):
            parsed=urlsplit(self.path)
            if parsed.path == '/api/directories':
                if not self.authorized():return self.reply(401,{'error':'Introduce el token del servidor.'})
                try:return self.reply(200,monitor.directories(parse_qs(parsed.query).get('path',[None])[0]))
                except (ValueError,OSError) as error:return self.reply(400,{'error':str(error)})
            if self.path == '/health': return self.reply(200,{'ok':True})
            if self.path == '/api/status':
                if not self.authorized(): return self.reply(401,{'error':'Introduce el token del servidor.'})
                return self.reply(200,monitor.snapshot())
            files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if self.path not in files: return self.reply(404,{'error':'No encontrado.'})
            name=files[self.path]; kind={'html':'text/html','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
            self.reply(200,(ROOT/'static'/name).read_bytes(),kind)
        def do_POST(self):
            origin=self.headers.get('Origin')
            if origin and origin != 'http://'+self.headers.get('Host','') and origin != 'https://'+self.headers.get('Host',''):
                return self.reply(403,{'error':'Origen no permitido.'})
            if self.headers.get('Content-Type') != 'application/json': return self.reply(415,{'error':'Se requiere JSON.'})
            if not self.authorized(): return self.reply(401,{'error':'Token incorrecto.'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=4096: raise ValueError('Petición demasiado grande.')
                body=json.loads(self.rfile.read(length) or b'{}')
                if self.path=='/api/scan': threading.Thread(target=monitor.scan,daemon=True).start()
                elif self.path=='/api/download': monitor.enqueue(body['url'])
                elif self.path=='/api/settings/inspect':return self.reply(200,monitor.inspect_destination(body['output_dir']))
                elif self.path=='/api/settings':
                    result=monitor.save_settings(body)
                    threading.Thread(target=monitor.scan,daemon=True).start()
                    return self.reply(200,result)
                else: return self.reply(404,{'error':'No encontrado.'})
                self.reply(202,{'ok':True})
            except (ValueError,KeyError,TypeError,OSError) as error:self.reply(400,{'error':str(error) if isinstance(error,ValueError) else 'Solicitud no válida o carpeta no accesible.'})
    threading.Thread(target=monitor.worker,daemon=True).start()
    threading.Thread(target=monitor.scheduler,daemon=True).start()
    print(f'Interfaz: http://{host}:{port}',flush=True)
    server=ThreadingHTTPServer((host,port),Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

def apply_environment(config):
    fields = {'YML_HOST':('host',str),'YML_PORT':('port',int),
              'YML_DATA_DIR':('data_dir',str),'YML_TEMP_DIR':('temp_dir',str),
              'YML_LIBRARY_ROOT':('library_root',str),'YML_OUTPUT_DIR':('output_dir',str),'YML_SEASON':('season',int),
              'YML_INTERVAL_MINUTES':('check_interval_minutes',int),
              'YML_MIN_EPISODE':('min_episode',int),'YML_MAX_HEIGHT':('max_height',int)}
    for variable, (key, convert) in fields.items():
        if variable in os.environ: config[key] = convert(os.environ[variable])
    if 'YML_AUTO_DOWNLOAD' in os.environ:
        value=os.environ['YML_AUTO_DOWNLOAD'].lower()
        if value not in ('true','false','1','0'): raise ValueError('YML_AUTO_DOWNLOAD debe ser true o false.')
        config['auto_download']=value in ('true','1')

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--config',default=str(ROOT/'config.json'))
    parser.add_argument('--scan',action='store_true'); parser.add_argument('--sample',type=int,metavar='SECONDS')
    parser.add_argument('--download',type=int,metavar='EPISODE'); args=parser.parse_args()
    config=json.loads(Path(args.config).read_text())
    apply_environment(config)
    for key in ['max_pages','max_height','check_interval_minutes']:
        if int(config[key])<=0: raise ValueError(f'{key} debe ser positivo.')
    data_dir=Path(config['data_dir']).expanduser().resolve(); data_dir.mkdir(parents=True,exist_ok=True)
    instance_lock=(data_dir/'.instance.lock').open('a')
    try: fcntl.flock(instance_lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Ya hay una instancia utilizando esta base de datos.')
    monitor=Monitor(config)
    if args.scan or args.sample or args.download is not None:
        monitor.scan()
        if monitor.last_error: print(monitor.last_error)
        rows=monitor.snapshot()['episodes']
        if args.sample or args.download is not None:
            row=next((r for r in rows if args.download is None or r['number']==args.download),None)
            if not row: raise SystemExit('No se encontró el capítulo solicitado en el listado.')
            print(monitor.download(row['url'],args.sample))
        else: print(json.dumps(monitor.snapshot(),ensure_ascii=False,indent=2))
    else: serve(monitor,config.get('host','127.0.0.1'),int(config.get('port',8787)))

if __name__=='__main__': main()
