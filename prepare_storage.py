"""Prepara exclusivamente los datos y temporales del proyecto; no monta la biblioteca."""
import os
from pathlib import Path
import stat


def prepare(root, uid, gid):
    root=Path(root)
    if root.is_symlink():raise ValueError('La carpeta del proyecto no puede ser un enlace simbólico.')
    root.mkdir(parents=True,exist_ok=True)
    def fix(path):
        if path.is_symlink():return
        current=path.stat()
        os.chown(path,uid,gid,follow_symlinks=False)
        permissions=stat.S_IMODE(current.st_mode)
        os.chmod(path,permissions | (0o700 if path.is_dir() else 0o600))
    fix(root)
    for name in ('data','tmp'):
        directory=root/name
        if directory.is_symlink():raise ValueError(f'{name} no puede ser un enlace simbólico.')
        directory.mkdir(exist_ok=True)
        fix(directory)
        for folder, dirs, files in os.walk(directory,followlinks=False):
            dirs[:]=[name for name in dirs if not (Path(folder)/name).is_symlink()]
            for name in dirs+files:fix(Path(folder)/name)


if __name__=='__main__':
    try:
        uid=int(os.environ.get('PUID','1000'));gid=int(os.environ.get('PGID','1000'))
        if uid<0 or gid<0:raise ValueError('PUID y PGID deben ser enteros positivos o cero.')
        prepare('/project',uid,gid)
        print(f'Carpetas del proyecto preparadas para {uid}:{gid}.',flush=True)
    except (OSError,ValueError) as error:
        raise SystemExit(f'No se pudieron preparar los permisos del proyecto: {error}')
