# Yo me llamo

Monitor y descargador de capítulos con interfaz web. Python, SQLite y FFmpeg, sin navegador ni dependencias Python obligatorias.

- Revisa novedades cada 15 minutos. En Ajustes eliges la carpeta existente y continúa automáticamente después del último capítulo reconocido.
- Recorre las listas internas y «CARGAR MÁS». Revisión completa al iniciar por primera vez y cada 24 horas; el resto de revisiones consulta novedades.
- Descarga a `/instalaciones/yo-me-llamo/tmp` y publica el MP4 completo en `/extraible2/Series/yo me llamo/Temporada 2026`.
- Una descarga simultánea, historial persistente y hasta tres intentos automáticos. Funciona con discos distintos.

## 1. Subir a GitHub y generar la imagen

Sube el código a un repositorio, en la rama `main` o `master`. No incluyas `.venv`, `data`, `downloads` ni `.env`; están excluidos de Git y del contexto Docker.

El workflow **Imagen Docker** ejecuta las pruebas y publica en GitHub Container Registry:

```text
ghcr.io/anubisreal/yo-me-llamo:latest
```

Construye para `linux/amd64` y `linux/arm64`. Utiliza `GITHUB_TOKEN` de Actions; no necesitas crear un secreto para publicar. Espera a que Actions termine correctamente. En GitHub → Packages → yo-me-llamo → Package settings, haz público el paquete para descargarlo desde Portainer sin credenciales. Si prefieres mantenerlo privado, configura GHCR como registro autenticado en Portainer.

## 2. Preparar las carpetas del servidor

Comprueba que `/extraible2` es el disco de destino y está montado. Ejecuta en Ubuntu:

```sh
sudo install -d -o 1000 -g 1000 /instalaciones/yo-me-llamo/data
sudo install -d -o 1000 -g 1000 /instalaciones/yo-me-llamo/tmp
sudo install -d -o 1000 -g 1000 "/extraible2/Series/yo me llamo"
```

Si tu biblioteca usa otro usuario/grupo, sustituye `1000` por sus UID/GID y utiliza esos mismos valores en `PUID` y `PGID` del stack. No es necesario cambiar los propietarios de los vídeos existentes.

La instalación queda en la imagen Docker. Solo la base de datos y los temporales se guardan en `/instalaciones/yo-me-llamo`; los vídeos terminados van al disco de series.

## 3. Crear el stack en Portainer

En un entorno **Docker Standalone**, abre **Stacks → Add stack → Web editor**, pega [compose.yaml](compose.yaml) y añade dos variables:

| Variable | Valor |
| --- | --- |
| `IMAGE` | `ghcr.io/anubisreal/yo-me-llamo:latest` (usuario en minúsculas) |
| `YML_TOKEN` | Un token que elijas. Puedes generar uno con `openssl rand -hex 24`. |

Pulsa **Deploy the stack**. Abre `http://IP-DE-TU-SERVIDOR:8787` e introduce ese token.

En la primera apertura no empieza a descargar hasta configurar el destino:

1. Abre **Ajustes → Explorar** y elige la carpeta de Yo me llamo dentro de `/extraible2/Series`.
2. Pulsa **Buscar capítulos existentes**. Verás los archivos reconocidos y el último capítulo.
3. Comprueba **Continuar desde el capítulo** y pulsa **Guardar ajustes**.

Por ejemplo, si ya tienes hasta el 72, propone empezar por el 73. Reconoce nombres como `Capítulo 72 Yo Me Llamo, completo y gratis - 720p.mp4`, `S01E072` y `E072`; también compara títulos con el catálogo cuando falta la numeración. Si hay archivos sin reconocer los muestra para revisarlos. No renombra ni mueve los vídeos que ya tenías.

La carpeta, el punto de inicio y la automatización se guardan en la base de datos y sobreviven a actualizaciones. El servicio arranca con el servidor y sigue revisando aunque cierres la web. Puedes cambiar el destino después desde Ajustes cuando no haya descargas pendientes. El selector muestra las carpetas del servidor montadas dentro del contenedor, no las de tu ordenador. El stack monta `/extraible2/Series` completo para poder elegir sus subcarpetas.

También puedes elegir **Git Repository**, indicar tu repositorio y poner `compose.yaml` como Compose path. Para actualizar la aplicación, espera a que GitHub publique la nueva imagen y actualiza el stack con **Re-pull image**.

Variables opcionales:

| Variable | Predeterminado | Uso |
| --- | --- | --- |
| `PUID`, `PGID` | `1000`, `1000` | Permisos de las carpetas |
| `PORT` | `8787` | Puerto web del servidor |
| `CHECK_MINUTES` | `15` | Frecuencia de búsqueda |
| `MIN_EPISODE` | `75` | Propuesta inicial si la carpeta está vacía; se puede cambiar en Ajustes |
| `SEASON` | `2026` | Temporada |
| `YML_PROXY` | vacío | Proxy HTTP/HTTPS opcional; misma salida para consultas y vídeo |

Después de guardar Ajustes descarga los capítulos pendientes posteriores al último existente y al punto de inicio elegido. No rellena huecos anteriores automáticamente; siguen disponibles para descarga manual. Al reiniciar, recupera las tareas interrumpidas. Los errores se reintentan hasta tres veces, con al menos 15 minutos entre intentos; el botón **Reintentar** reinicia el contador.

El acceso por token protege las operaciones de la web. Para acceder desde Internet utiliza tu proxy inverso con HTTPS; el stack publica HTTP en el puerto elegido para la red del servidor.

## Almacenamiento

```text
/instalaciones/yo-me-llamo/
├── data/       # catálogo SQLite y estado
└── tmp/        # descargas .partial.mp4

/extraible2/Series/yo me llamo/
└── Temporada 2026/
    └── Yo me llamo - 2026 - E075.mp4
```

Si los volúmenes están en discos distintos, copia el archivo terminado a un nombre oculto en destino, sincroniza y lo renombra. Solo entonces elimina el temporal y marca el capítulo como guardado. Si falla la copia, conserva el temporal. Se necesita espacio libre para un capítulo en ambos discos durante el traslado.

La base de datos y los vídeos no forman parte de la imagen. Para migrar una instalación, conserva ambos. No necesitas importar una base de datos para reutilizar los capítulos existentes: el servicio inspecciona la carpeta elegida, incluidas sus subcarpetas, y cruza sus archivos con el catálogo. La carpeta real es la referencia: un estado antiguo de otra ubicación no hace que se omita un capítulo que falta en el destino nuevo. No se procesa contenido cifrado. La disponibilidad depende de la web de Caracol.

## Desarrollo local

```sh
python3 app.py
```

Abre `http://127.0.0.1:8787`. Requiere FFmpeg para descargar. Alternativa en este proyecto: `.venv/bin/python app.py` con `imageio-ffmpeg` instalado desde `requirements-dev.txt`.

```sh
python3 -m unittest discover -s tests
python3 -m py_compile app.py library.py
node --check static/app.js
```

Construcción local con Docker:

```sh
docker build -t yo-me-llamo:local .
```

Copia `.env.example` a `.env`, configura el token y usa `IMAGE=yo-me-llamo:local` para desplegar una imagen local con `docker compose up -d`. Para ejecutar sin Docker en Ubuntu también se incluye el servicio opcional `deploy/yo-me-llamo.service`.

## Proxy colombiano de respaldo

El stack usa primero la conexión directa. Si falla una consulta o descarga, prueba `YML_PROXY_FALLBACK`, configurado como `http://8.242.147.166:3128`. Se verificaron salida CO, TLS y ocho segundos del HLS con FFmpeg el 7 de noviembre de 2026. Fuente pública: https://proxyspace.pro/country/co/. Un proxy gratuito puede dejar de funcionar; se puede sustituir desde las variables del stack. Para desactivar el respaldo define `YML_PROXY_FALLBACK` vacío.

Si defines `YML_PROXY`, se fuerza esa salida para consultas y vídeos y no se cambia de IP a mitad de descarga. El respaldo vuelve a resolver el HLS por su propia salida, porque las URLs firmadas pueden estar ligadas a la IP. No se desactiva la validación TLS.
