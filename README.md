# Radar de empleo

Sistema simple para juntar en un solo dashboard las ofertas nuevas de
LinkedIn, Computrabajo, Bumeran, ZonaJobs, Indeed, etc. sin scrapear
ninguna página (lo cual violaría sus términos de uso y te puede banear
la cuenta o bloquear la IP).

**Cómo funciona:** vos configurás en cada portal que te manden alertas de
empleo por mail a una casilla dedicada. Un script (`scan_email.py`) lee esa
casilla, filtra por tus palabras clave, y guarda las ofertas nuevas en
`jobs.json`. El dashboard (`index.html`) lee ese archivo y te muestra las
ofertas, con un botón para generar (no enviar solo) el borrador del mail de
postulación.

---

## Paso 1 — Casilla dedicada

Creá un Gmail nuevo solo para esto, ej. `tunombre.jobs@gmail.com`. Así no
mezclás alertas de trabajo con tu correo personal.

Activá la verificación en 2 pasos y generá una **contraseña de aplicación**
(no la contraseña normal de la cuenta):
`Cuenta de Google → Seguridad → Verificación en dos pasos → Contraseñas de aplicaciones`

Guardá esa contraseña, la vas a necesitar en el Paso 4.

## Paso 2 — Configurar alertas en cada portal

Entrá a cada sitio con búsquedas guardadas y activá "avisarme por mail":
- **LinkedIn:** Empleos → Preferencias de alerta de empleo → creá una búsqueda y activá "Recibir alertas por correo electrónico"
- **Computrabajo:** creá una búsqueda y activá "Alertas de empleo"
- **Bumeran / ZonaJobs:** mismo concepto, "Crear alerta" en resultados de búsqueda
- **Indeed:** "Crear alerta de empleo" en la búsqueda

Usá el mail nuevo del Paso 1 en todos.

## Paso 3 — Repositorio en GitHub

1. Creá un repo (puede ser privado) y subí estos archivos.
2. Copiá `config.example.json` a `config.json` y completá:
   - `imap_user`: tu casilla dedicada
   - `senders`: dejalo como está o ajustalo si ves otra dirección real en tus mails
   - `keywords_include` / `keywords_exclude`: tus filtros
3. **No pongas la contraseña en config.json.** Se pasa como secret (paso 4).

## Paso 4 — Secrets de GitHub Actions

En el repo: `Settings → Secrets and variables → Actions → New repository secret`

- `IMAP_USER` → tu casilla dedicada
- `IMAP_PASS` → la contraseña de aplicación del Paso 1

El workflow en `.github/workflows/scan.yml` ya está listo: corre cada 30
minutos, ejecuta `scan_email.py`, y si hay ofertas nuevas actualiza
`jobs.json` y lo commitea de vuelta al repo. También lo podés disparar a
mano desde la pestaña **Actions → Scan job alerts → Run workflow**.

## Paso 5 — Publicar el dashboard (GitHub Pages)

`Settings → Pages → Deploy from branch → main → / (root)`

Con eso, `index.html` queda accesible en algo como
`https://tu-usuario.github.io/tu-repo/` y siempre va a estar leyendo la
versión más reciente de `jobs.json`, porque el workflow lo actualiza solo.

Si preferís probarlo local antes de subir nada:
```bash
cd job-alerts
python3 -m http.server
# abrí http://localhost:8000
```
(Abrir `index.html` con doble clic no funciona: los navegadores bloquean
`fetch()` sobre archivos locales por seguridad.)

## Sobre el envío de mails

El dashboard genera un **borrador** de mail de postulación por cada oferta
(con el botón "Generar borrador de mail"), que podés copiar o abrir
directamente en tu cliente de correo con "Abrir en mi correo". A propósito
no lo mandamos 100% automático: un mail genérico mal armado puede jugarte
en contra, y conviene que revises a qué te estás postulando antes de que
salga. Editá el texto en el `<textarea>` antes de mandarlo.

## Limitaciones a tener en cuenta

- Los extractores de `scan_email.py` son heurísticos: el formato de los
  mails de cada portal puede cambiar, y puede que el título/empresa no
  salga perfecto siempre. Si ves que un portal en particular sale mal
  parseado, mandame (o revisá vos) un mail real de ese portal y ajustamos
  el regex de extracción.
- El estado "Revisado/Descartado" que marcás en el dashboard es solo en
  memoria del navegador: se resetea si recargás la página. Si querés que
  persista, el siguiente paso natural es que el botón escriba de vuelta a
  `jobs.json` vía un pequeño backend, o guardarlo en una hoja de cálculo /
  base de datos simple — avisame si querés que lo sumemos.
- Esto depende 100% de que las alertas por mail de cada portal sigan
  llegando con contenido parseable. Si un portal cambia su formato de
  email radicalmente, hay que retocar su extractor.

## Archivos

| Archivo | Qué hace |
|---|---|
| `index.html` | Dashboard que muestra las ofertas y arma el borrador de mail |
| `scan_email.py` | Lee la casilla IMAP y genera/actualiza `jobs.json` |
| `config.example.json` | Plantilla de configuración (copiar a `config.json`) |
| `jobs.json` | Ofertas detectadas (se genera/actualiza solo) — trae 3 de ejemplo |
| `seen.json` | IDs ya procesados, para no duplicar alertas (se genera solo) |
| `.github/workflows/scan.yml` | Corre el scanner cada 30 min en GitHub Actions |
