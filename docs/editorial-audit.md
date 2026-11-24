# Auditoría: por qué entran clips flojos y se pierden los buenos

Esto describe el selector que ya está en producción, el de señales. Sigue siendo el respaldo cuando no hay `ANTHROPIC_API_KEY`. El editor nuevo no reemplaza la grabación ni Telegram; se sienta delante de este corte.

## Qué dispara un clip

Cada 2 segundos se puntúa una ventana de 10 segundos. Las únicas entradas son velocidad de chat, ráfaga de emotes, RMS de audio, un score de cambio de plano y el delta de espectadores. El momento se abre cuando el z-score ponderado pasa el umbral y se cierra cuando baja o cuando ya dura `clip_max_seconds`.

```191:207:cliphunter/signals/engine.py
        if phase == "fire":
            samples = list(state.tracker.samples)
            peak = state.peak_tick or tick
            state.peak_tick = None
            state.tracker.reset()
            handled = False
            if self.editorial is not None:
                try:
                    handled = await self.editorial.on_fire(state, samples, peak, now)
                except Exception as exc:
                    log_event("ERROR", "editorial.fire_failed", channel=view.channel_key, error=str(exc))
                    handled = False
            if not handled:
                state.cooldown_until = now + channel_cooldown(view.thresholds)
                await self._cut(state, samples, peak)
```

Sin editor, `handled` queda en falso y `_cut` corre. No hay una frase, un nombre ni una storyline en esa decisión. Un pico de chat o de volumen basta.

## Por qué gana lo ruidoso

`AudioEnergy` devuelve el RMS del segmento. `ChatVelocity` cuenta mensajes. `SceneChange` devuelve el máximo de `lavfi.scene_score`. Los tres se convierten en z-score contra una línea base de 10 minutos. Un grito, una lluvia de «JAJAJA» o un corte de cámara mueven el z-score. Una reacción callada, con poco chat y el mismo plano, se queda cerca de cero y no abre el momento.

No hay una categoría de humor, morbo o chisme. El umbral no distingue un pleito largo de un punchline. El peso por defecto le da al audio 0.30 y al chat 0.35: juntos son la mayor parte de la suma. El contenido no entra en la cuenta.

## La transcripción llega tarde

El texto se pide después de haber cortado, y solo si `transcript_on_trigger` está activo. El evento `moment.candidate` sale con `transcript: None`. El modelo, si estuviera, no vería el chiste antes de decidir.

```362:383:cliphunter/signals/engine.py
        payload = {
            "channel_key": state.view.channel_key,
            "clip_id": result.clip_id,
            "start": selected[0].wall_start,
            "end": selected[-1].wall_end,
            "peak_score": round(tick.score, 3),
            "breakdown": breakdown,
            "transcript": None,
            "path": str(result.path),
        }
        ...
        if self.config.transcript_on_trigger and result.clip_id is not None:
            asyncio.create_task(
                self._attach_transcript(result.clip_id, result.path),
                name=f"transcript-{result.clip_id}",
            )
```

Esa transcripción, además, era un wav pasado a faster-whisper por ruta. En el portátil de Windows eso revienta en PyAV (`metadata_errors`). Aunque se pidiera antes, no había texto usable. Ahora el PCM lo saca ffmpeg y entra como array; el selector viejo no lo usa para elegir el corte.

Tampoco hay una ficha de quién habla. Un nombre, un apodo o una rivalidad no existen en el esquema de la fase de señales. El programa no puede saber si el momento importa porque es Tokischa o porque alguien gritó.

## La ventana no es el chiste

`choose_bounds` no busca la preparación ni el payoff. Toma el pico del z-score, le resta unos 8 segundos y estira hasta el objetivo de 30, con techo de 45.

```119:134:cliphunter/signals/aggregate.py
    peak_t, _peak_score = max(samples, key=lambda item: item[1])
    rise = samples[0][0]
    fall = samples[-1][0]
    start = min(rise, peak_t - pre_roll)
    end = max(fall, start + target_seconds)
    ...
    if end - start > max_seconds:
        start = peak_t - min(pre_roll + 4.0, max_seconds * 0.4)
        end = start + max_seconds
```

Si el chiste empezó 40 segundos antes del pico de chat, el clip abre en medio. Si la reacción buena llega después de que el score ya cayó, queda fuera. Un silencio gracioso que no mueve el score no genera muestras y no hay nada que extender.

## El corte cae en el keyframe

`_cut` elige segmentos enteros y `create_clip` los une con `-c copy`. El inicio real es el principio del segmento que cubre la ventana, no la frase. Con segmentos de unos 4 segundos, el clip puede empezar antes o cortar la última palabra. `segments_for_window` además agranda o recorta por duración de segmento, no por oración.

Eso es aceptable para un respaldo rápido. No sirve para no pisar un punchline.

## Qué se pierde, en una frase

Se selecciona lo que hace ruido en el chat, el audio o el plano, en una ventana fija alrededor de ese ruido, cortada en el keyframe, sin saber qué se dijo ni quién importa. Un momento callado, una indirecta o el remate que cae fuera de esos 30 segundos no existe para el selector.
