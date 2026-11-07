"""Reconocimiento de capítulos existentes y selección de carpetas del servidor."""
import os
from pathlib import Path
import re
import unicodedata

VIDEO_EXTENSIONS = {'.mp4','.mkv','.avi','.ts','.m4v','.mov','.webm'}

def normalized(value):
    text=unicodedata.normalize('NFKD',value.lower())
    text=''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+',' ',text).strip()

def episode_number(name):
    text=normalized(Path(name).stem)
    if re.search(r'\b(?:partial|part|copying|muestra|sample)\b',text): return None
    for pattern in [r'\bcap(?:itulo)?\s*(\d{1,4})\b',r'\bs\d{1,4}\s*e\s*(\d{1,4})\b',r'\be\s*(\d{1,4})\b',
                    r'\byo me llamo\s+(\d{1,3})\b',r'^(\d{1,3})\b']:
        match=re.search(pattern,text)
        if match and int(match[1])>0:return int(match[1])
    return None

def checked_directory(value, root):
    path=Path(value).expanduser().resolve();root=Path(root).resolve()
    if not path.is_relative_to(root):raise ValueError('Elige una carpeta dentro de '+str(root))
    if not path.is_dir():raise ValueError('La carpeta no existe o no está montada.')
    if not os.access(path,os.R_OK|os.X_OK):raise ValueError('No hay permisos para leer la carpeta.')
    return path

def inspect_library(directory, season, catalog=()):
    directory=Path(directory)
    if not directory.is_dir():raise ValueError('La carpeta de destino no existe o no está montada.')
    titles={}
    for row in catalog:
        title=row['title'] or ''
        titles[normalized(title)]=row['number']
        short=re.sub(r'^Cap[ií]tulo\s+\d+\s+Yo\s+Me\s+Llamo\s*:\s*','',title,flags=re.I)
        titles[normalized(short)]=row['number']
    found={};unrecognized=[];total=0
    def failed(error):raise ValueError('No se puede leer una subcarpeta de la biblioteca.') from error
    for folder, dirs, files in os.walk(directory,followlinks=False,onerror=failed):
        dirs[:]=sorted(d for d in dirs if not d.startswith('.') and not (Path(folder)/d).is_symlink())
        for name in sorted(files):
            path=Path(folder)/name
            if path.suffix.lower() not in VIDEO_EXTENSIONS or name.startswith('.') or path.is_symlink():continue
            total+=1
            if total>20000:raise ValueError('Hay demasiados vídeos. Elige la carpeta concreta de Yo me llamo.')
            if path.stat().st_size==0:continue
            text=normalized(name)
            if re.search(r'\b(?:partial|part|copying|muestra|sample)\b',text):continue
            relative=path.relative_to(directory)
            season_tag=re.search(r'(?:temporada|season)\s*(20\d{2})\b',normalized(str(relative.parent)))
            canonical_year=re.search(r'\byo me llamo (20\d{2}) e\d+\b',normalized(path.stem))
            if any(tag and tag[1]!=str(season) for tag in (season_tag,canonical_year)):continue
            number=episode_number(name)
            if number is None:
                alias=re.sub(r'\b(?:720p|1080p|480p|mp4|mkv)\b','',normalized(path.stem)).strip()
                number=titles.get(alias)
            if number:
                found.setdefault(number,{'number':number,'path':str(path),'name':str(relative)})
            else:unrecognized.append(str(relative))
    episodes=sorted(found.values(),key=lambda item:item['number'])
    return {'count':len(episodes),'last_episode':max(found,default=0),'episodes':episodes,
            'unrecognized_count':len(unrecognized),'unrecognized':unrecognized[:20]}
