# ClipHunter

ClipHunter vigila como mucho **dos directos a la vez**, guarda un buffer rodante en disco y corta clips **sin re-encode**. Esta es la **fase 1**: un solo proceso en Python, asyncio y SQLite. No usa Docker, Redis ni Postgres.

Pensado para un portátil con Windows 10/11, sin GPU, con Python 3.12, ffmpeg y streamlink ya instalados y en el `PATH`. El objetivo de recursos es quedarse por debajo del 50 % de CPU de la máquina con dos directos en 480p: solo se copia el vídeo, no se decodifica.

Los canales pueden hablar inglés o español. El campo `context` acepta texto en cualquiera de los dos idiomas; la fase 1 lo guarda y lo copia en los metadatos del clip para el scorer de más adelante.

## Qué hace la fase 1

- Lee un YAML validado con Pydantic. Solo entran canales con `authorized: true`.
- Al arrancar `run`, pregunta una vez por canal: **¿confirmas que este canal está autorizado?** Si respondes que no, ese canal no se vigila. `--yes` salta la pregunta (tareas no interactivas).
- YouTube: detecta directo/offline sin gastar cuota de la Data API en el sondeo. Twitch usa Helix si hay credenciales. Kick está en stub.
- Por cada directo, una tarea asyncio lanza `streamlink` hacia `ffmpeg` con `-c copy`, segmentos de unos 4 s y un buffer de 10 minutos que se borra solo.
- `clip` corta los últimos N segundos **mientras `run` sigue grabando**. Los dos procesos comparten `index.json` en disco y la misma base SQLite (modo WAL).
- Métricas de CPU y RAM por directo (psutil) y logs en JSON, una línea por evento.

## Instalación en Windows

1. Comprueba Python 3.12, ffmpeg y streamlink. Abre PowerShell:

```powershell
py -3.12 --version
ffmpeg -version
streamlink --version
```

Si falta Python, instálalo desde [python.org](https://www.python.org/downloads/windows/) y marca **Add python.exe to PATH**.

Si falta ffmpeg: `winget install Gyan.FFmpeg` o un build de [gyan.dev](https://www.gyan.dev/ffmpeg/builds/).  
Si falta streamlink: el instalador de [streamlink](https://streamlink.github.io/install.html) o, dentro del venv, `pip install streamlink`. Tiene que quedar el ejecutable `streamlink` en el `PATH` (cierra y abre la terminal después de instalar).

2. Clona el repo y crea el entorno:

```powershell
git clone <url-del-repo> cliphunter
cd cliphunter
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Si PowerShell bloquea el script de activación:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

3. Comprueba el entorno del proyecto:

```powershell
python -m cliphunter doctor
```

Tiene que encontrar `ffmpeg` y `streamlink`. `ffprobe` suele venir con ffmpeg.

4. Copia la configuración de ejemplo. `config.yaml` y `.env` están en `.gitignore`: no se suben al repo. Hay dos ejemplos:

- `config.example.yaml`: tres canales de plantilla, todos con `authorized: false`.
- `config.alofoke.example.yaml`: el canal de Alofoke Radio Show (`https://www.youtube.com/@Alofokeradioshow`), ya marcado como autorizado, con el permiso público y los pesos del scorer.

```powershell
copy config.example.yaml config.yaml
copy .env.example .env
```

Para usar el canal de Alofoke en lugar de la plantilla:

```powershell
copy config.alofoke.example.yaml config.yaml
```

El corte de ese canal es `python -m cliphunter clip --canal alofoke --ultimos 45`. La carpeta del buffer es `youtube_alofoke`.

5. Edita `config.yaml`. Pon `authorized: true` **solo** en canales que puedas grabar. `config.example.yaml` trae los tres en `false` a propósito. El de Alofoke ya viene en `true` porque el permiso está documentado en el propio archivo.

```yaml
max_concurrent_streams: 2
buffer_dir: 'C:\cliphunter\buffer'
buffer_minutes: 10
segment_seconds: 4
quality:
  - 480p
  - 720p
poll_interval_seconds: 45
channels:
  - platform: youtube
    handle: "@TuCanal"
    authorized: true
    context: "Variedades en español. Humor y reacciones."
    authorization:
      tipo: "permiso del creador"
      fuente: "https://ejemplo.invalid/permiso"
      fecha: "2026-11-17"
```

`context` puede ser un texto o un mapa con `idioma`, `nicho`, `buen_clip` y `evitar` (así está en el ejemplo de Alofoke). `authorization` es opcional: `tipo`, `fuente` y `fecha` (`AAAA-MM-DD`). Si rellenas alguno, el clip lo guarda en el JSON de atribución y en `attribution_json`.

`poll_interval_seconds` recomendado: entre 30 y 60 (en el borrador del ejemplo de Alofoke se llamaba `poll_interval_s`). `quality` es una preferencia: se prueba 480p, luego 720p y, si ninguna existe, la calidad más baja (`worst`), para no saltar a 1080p. También acepta la forma `"480p,720p"`.

La base queda al lado del buffer: `C:\cliphunter\cliphunter.sqlite`. Los clips, en `C:\cliphunter\clips\`.

## Clave de YouTube (opcional en la fase 1)

El sondeo de “¿está en directo?” **no usa** la API. La clave solo hace falta si no pudimos leer el título gratis.

1. Entra en [Google Cloud Console](https://console.cloud.google.com/).
2. Crea un proyecto.
3. **APIs y servicios → Biblioteca → YouTube Data API v3 → Habilitar**.
4. **APIs y servicios → Credenciales → Crear credenciales → Clave de API**.
5. Restringe la clave a YouTube Data API v3.
6. Pégala en `.env`:

```
YOUTUBE_API_KEY=tu_clave
```

No hace falta para empezar. Sin clave, el título sale de la página o de oEmbed.

### Cuota

| Llamada | Cuándo | Unidades |
| --- | --- | --- |
| GET de `https://www.youtube.com/@canal/live` | Cada sondeo | 0 |
| oEmbed, si falta el título | Una vez por vídeo, en caché | 0 |
| `videos.list` | Solo si lo anterior falló y hay clave | 1 por vídeo |

No usamos `search.list`. Cuesta 100 unidades y, sondeando cada 45 s, un solo canal se comería la cuota diaria por defecto (10.000) en menos de dos horas.

La detección depende del redirect público de `/live` hacia `/watch?v=…`. Si YouTube cambia esa página, el watcher puede equivocarse hasta que se actualice el parser. Un fallo de red **no** marca el directo como terminado: se conserva el estado y se reintenta en el siguiente sondeo.

## Twitch y Kick

Twitch (Helix) sí está implementado. En [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) crea una aplicación y rellena `.env`:

```
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
```

La app pide sola el token de cliente. Sin esas variables, el canal de Twitch no se vigila y el log lo dice.

Kick implementa la misma interfaz `Platform`, pero `check_live` responde `kick_not_implemented`. No graba, aunque pongas `authorized: true`. La API pública de Kick no es estable y queda para otra fase.

`ANTHROPIC_API_KEY` y `TELEGRAM_BOT_TOKEN` se leen para las fases siguientes y **no se usan** en la fase 1. El scorer de Claude está en `cliphunter/scoring.py` y lanza `NotImplementedError`. El token de Telegram será el bot de Approve / Reject / Adjust.

## Grabar

```powershell
python -m cliphunter run --config config.yaml
```

Verás, por cada canal autorizado:

```
¿confirmas que este canal está autorizado? [youtube:TuCanal] (s/n):
```

Respuestas que valen como sí: `s`, `si`, `sí`, `y`, `yes`. Cualquier otra cosa es no.

Para una tarea programada, sin teclado:

```powershell
python -m cliphunter run --config config.yaml --yes
```

También vale `skip_authorization_prompt: true` en el YAML, o `CLIPHUNTER_SKIP_AUTH_PROMPT=1`.

La pregunta se hace **una vez por canal al lanzar el proceso**, antes de grabar. La hora de esa respuesta se guarda en `sessions.confirmed_authorized_at` cuando el canal entra en directo. Si el directo se cae y vuelve en la misma ejecución, no se vuelve a preguntar: si no, un corte de red te dejaría el programa bloqueado en `input()`.

Para el proceso con Ctrl+C. Así manda `terminate()` a streamlink y a ffmpeg. Si matas el proceso a la fuerza, esos hijos pueden quedarse un rato hasta que el sistema los cierre.

## Atajo de teclado

Con `run` en marcha, **Ctrl+Alt+C** corta el directo activo. Usa el mismo camino que `clip` (ffmpeg `-c copy`, sin re-encode) y guarda el mp4 con `kind=manual`. Además anota un peso de calibración **3.0** en la fila del clip y en `decisions` (`action=approve`, `note=hotkey`), para que más adelante pese más que un corte automático.

- Si solo hay un directo, corta ese.
- Si hay dos, corta `active_channel` cuando está puesto (en el ejemplo de Alofoke es `alofoke`). Si no, corta el que empezó más tarde.
- Pulsaciones repetidas en menos de 3 segundos se ignoran (`hotkey_debounce_seconds`).

En el YAML:

```yaml
hotkey: "ctrl+alt+c"
hotkey_seconds: 45
hotkey_debounce_seconds: 3
active_channel: alofoke
```

`hotkey_seconds` son los segundos del corte (por defecto 45). El atajo se escucha con [pynput](https://pypi.org/project/pynput/), en un hilo, y el evento entra al bucle de asyncio con `call_soon_threadsafe`. No hace falta ejecutar la terminal como administrador (a diferencia de la librería `keyboard`).

Si pynput no está o no puede enganchar el teclado (pasa en la CI de Linux, sin sesión gráfica), `run` sigue y deja un aviso. Para apagar el atajo a propósito:

```powershell
python -m cliphunter run --config config.yaml --no-hotkey
```

Al cortar verás una línea `[cliphunter]` en la consola. En Windows, si instalas `plyer` o `win11toast`, también sale un aviso del sistema. Si no están, no pasa nada: el clip se guarda igual.

```powershell
pip install plyer
```

## Revisión por Telegram

El bot es **@clipowii_bot**. El token va en `.env` como `TELEGRAM_BOT_TOKEN` (no se commitea).

1. Arranca la vinculación y, en el teléfono, abre el bot y envía `/start`:

```powershell
python -m cliphunter telegram-setup --config config.yaml
```

Eso guarda el chat id en `telegram_chat.json`, al lado de la base. Solo se aceptan botones de ese chat.

2. Con `run` en marcha, cada clip manual (atajo o `clip`) entra en cola (`status=pending`). El proceso hace long polling: no hace falta un webhook público. El mensaje lleva el mp4, una leyenda (canal, marcas de tiempo, score y breakdown si existen) y los botones **Aprobar**, **Rechazar**, **Ajustar -5s inicio**, **Ajustar +5s inicio**, **Ajustar -5s fin** y **Ajustar +5s fin**.

3. Un ajuste vuelve a cortar el buffer **sin re-encode** (segmentos enteros, así que puede pasarse de 5 s) y reenvía la vista previa. Cada decisión queda en `decisions` con los segundos del ajuste y el breakdown de la señal, si lo hay.

4. **Aprobar** copia el mp4 a `listos_para_subir` (junto al buffer, por ejemplo `C:\cliphunter\listos_para_subir`) y escribe un `.txt` con `title`, `hook` y `hashtags` vacíos por ahora, más la línea de crédito del canal. En Alofoke es `🎙️ @Alofokeradioshow`. Los subtítulos, el 9:16 y Claude llegan después.

Si el mp4 pesa más de 50 MB, solo la copia que se manda a Telegram se baja a 480p con un ffmpeg de un hilo. El archivo que apruebas sigue siendo el original.

Si no hay token o chat id, o Telegram no responde, la grabación no se para. En ese caso la cola también está en `http://127.0.0.1:8765` (solo esta máquina), con los mismos botones y un aviso en la consola. El puerto se cambia con `review_port`.

Nada se graba de un canal con `authorized: false`.

Como mucho hay 2 ingestas a la vez (`max_concurrent_streams`, entre 1 y 2). Un tercero en directo espera hueco. Si una ingesta falla, reintenta con espera exponencial (1 s, 2 s, 4 s… hasta 60 s, con un poco de jitter) y **no** tumba al otro directo. Tras unos 30 s sanos, la espera vuelve a empezar en 1 s. streamlink solo reintenta un par de veces (`--retry-max 2`); la espera larga la lleva ClipHunter, para que un directo caído no deje el proceso colgado.

## Cortar un clip

En **otra** terminal, con el venv activado y `run` todavía en marcha:

```powershell
python -m cliphunter clip --config config.yaml --canal TuCanal --ultimos 45
```

`--canal` acepta el handle (`TuCanal` o `@TuCanal`) o la clave (`youtube_TuCanal`). `--ultimos` son segundos.

El mp4 y un JSON con la atribución quedan en `C:\cliphunter\clips\`. El JSON y la fila de SQLite incluyen streamer, plataforma, URL, título del directo, contexto del canal, las marcas de tiempo y, si el canal lo trae, el bloque `authorization` (`tipo`, `fuente`, `fecha`). `kind` queda en `manual` y `status` en `ready`.

`run` escribe `C:\cliphunter\buffer\<canal>\index.json` con reemplazo atómico (`os.replace`). `clip` lee ese fichero; no hace falta un puerto de control. El segmento que ffmpeg todavía está escribiendo (unos 4 s en el borde del directo) no entra en el corte.

Inicializar la base a mano, si quieres crearla antes del primer `run`:

```powershell
python -m cliphunter init-db --config config.yaml
```

## Precisión del corte

El corte es **sin re-encode** (`-c copy`). ffmpeg parte el directo en keyframes (`-break_non_keyframes 0`), así que “4 segundos” es un objetivo: cada segmento dura hasta el siguiente keyframe (en HLS suele andar entre 2 y 6 s, a veces un GOP más largo).

`clip` suma segmentos enteros desde el final hasta cubrir los segundos pedidos. El resultado puede pasarse **como mucho un segmento**. No se puede pedir un frame exacto sin recodificar, y la fase 1 no recodifica.

Al remuxar MPEG-TS a mp4 se aplica `-bsf:a aac_adtstoasc` (no recodifica, solo reempaqueta el AAC). Si un codec no cabe en mp4 a pelo, el comando falla y lo dice; no hay un segundo intento con libx264.

## Logs y CPU

Cada evento es un JSON en stdout. Una muestra de métricas se parece a esto:

```json
{
  "event": "metrics.sample",
  "system_cpu_pct": 22.5,
  "process_cpu_pct": 30.0,
  "process_rss_mb": 140.2,
  "streams": [
    {"channel_key": "youtube_TuCanal", "cpu_pct": 18.0, "rss_mb": 90.0, "pids": [100, 101]}
  ]
}
```

- `system_cpu_pct` es el uso de **toda la máquina**, de 0 a 100. Es el número que hay que comparar con el objetivo de menos del 50 %.
- `cpu_pct` de cada directo suma streamlink y ffmpeg según psutil. Puede pasar de 100 si un proceso usa más de un núcleo.
- La primera muestra de un proceso suele salir a 0: psutil necesita dos lecturas.
- Esas filas también se guardan en la tabla `metrics` (`cpu_pct`, `rss_mb`, y el desglose en `extra_json`).

Para bajar CPU y RAM: como mucho 2 directos, preferencia 480p, copia sin decodificar, un solo hilo de segmentos en streamlink y un borde HLS corto (`--hls-live-edge 2`).

## Pruebas

Hacen falta las dependencias de desarrollo. ffmpeg es opcional: si no está, los tests que lo usan se saltan.

```powershell
pip install -r requirements-dev.txt
pytest
```

En Linux es el mismo comando (`python3 -m venv .venv`, `source .venv/bin/activate`). Una ruta `C:\...` del ejemplo se redirige a `~/cliphunter/...` fuera de Windows, para poder cargar `config.example.yaml` en CI.

## Diseño (para las fases siguientes)

Un proceso. El bus es una interfaz (`EventBus`) con colas `asyncio.Queue`. El almacén es una interfaz (`Repo`) implementada con aiosqlite. Cambiar a Redis o Postgres es sustituir esas clases, no el watcher ni el clipper.

Eventos ya publicados: `stream.start`, `stream.end`, `segment.ready`, `clip.created`, `metrics.sample`.

| Pieza | Estado en la fase 1 |
| --- | --- |
| `config`, `bus`, `storage`, `watcher`, `ingest`, `clip`, `hotkey`, `observability` | Implementado |
| YouTube y Twitch (`platforms/`) | Implementado |
| Kick (`platforms/kick.py`) | Stub, misma interfaz |
| `signals/` | Interfaz `SignalDetector`. Nadie la llama todavía |
| `scoring.py` (`ClaudeScorer`) | Interfaz. No hay llamadas a Anthropic |
| `review/` | Telegram (@clipowii_bot) y página local en `127.0.0.1`. El 9:16 y Claude siguen después |
| `processing/` | Interfaz `reframe_vertical` (9:16) |
| `replay`, `calibrate` | Comandos que explican que llegan después |

Las tablas `clips` (`kind` manual\|auto, `signal_score`, `weight`, `breakdown_json`, `title`, `hook`, `hashtags`) y `decisions` (`weight`) guardan el corte del atajo como muestra de calibración. El scorer y Telegram las reutilizarán.

## Hoja de ruta

1. **Atajo global Ctrl+Alt+C**: ya está en `run` (esta fase). Se puede cambiar el combo y los segundos en el YAML.
2. **Señales en tiempo real** (audio, chat, picos) sobre `segment.ready`.
3. **Scorer con Claude**, usando `context`, `weights` y `thresholds` del canal. El título, el hook y los hashtags del clip salen de ahí.
4. **Revisión por Telegram**: botones Approve, Reject y Adjust ±5 s. Eso escribe en `decisions`.
5. **Procesado pesado solo al aprobar**: reencuadre 9:16 y el resto del acabado. La fase 1 no recodifica.
6. **`python -m cliphunter calibrate`**: ajustar umbrales con las decisiones ya guardadas.
7. **`python -m cliphunter replay`**: volver a puntuar un buffer que ya está en disco.

## Limitaciones conocidas

- El corte cae en el keyframe / segmento, no en el segundo exacto. Puede sobrar hasta un segmento (~4 s, a veces más si el GOP es largo).
- El segmento abierto (el que ffmpeg está escribiendo) no se incluye. El borde del directo queda fuera unos segundos.
- La detección de YouTube depende de la página pública `/live`. No gasta cuota en el sondeo; `videos.list` cuesta 1 unidad solo como último recurso para el título.
- Algunos codecs no entran en mp4 con `-c copy`. En ese caso el clip falla en vez de recodificar.
- Kick no detecta directos en esta fase.
- Twitch no detecta directos sin client id y secret. Los anuncios de Twitch pueden cortar la ingesta; el backoff vuelve a conectar.
- Si la única calidad disponible es muy alta y tampoco existe `worst`, streamlink no abre el directo. No subimos a 1080p a propósito.
- Un `kill -9` / cierre brusco puede dejar streamlink o ffmpeg vivos. Para el programa con Ctrl+C.
- La confirmación es por ejecución, no un registro legal más allá de `confirmed_authorized_at`.
- El atajo global depende de pynput y de una sesión de escritorio. Sin eso, `run` arranca igual y lo dice en el log. El aviso visual del sistema solo aparece si está `plyer` o `win11toast`.
- `replay` y `calibrate` todavía no hacen nada.
