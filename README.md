# Fiscaliza tu Congreso (recolector de datos)

Lee las votaciones del Pleno del Congreso de Guatemala y las guarda como archivos JSON
en la carpeta `datos/`. Cada dato lleva el enlace a la página oficial y el hash de la
página original, para poder auditarlo.

## Puesta en marcha (una sola vez)
1. Crea un repositorio **público** en GitHub y sube todo el contenido de esta carpeta
   (incluida la carpeta oculta `.github`).
2. En `recolector.py` ya está el correo de contacto (`USER_AGENT`). Cámbialo si quieres usar otro.
   El Congreso verá ese nombre en sus registros.
3. En GitHub: **Settings > Actions > General > Workflow permissions**, elige
   "Read and write permissions".
4. Ve a la pestaña **Actions > Actualizar votaciones > Run workflow** para la primera
   corrida. Repítela unas 8 veces hasta cubrir toda la legislatura (24 sesiones por corrida
   evitan cargar el servidor). Después corre sola.

## Qué produce
- `datos/votaciones/*.json`: una votación por archivo (voto de cada diputado).
- `datos/sesiones/*.json`: listado de preguntas de cada sesión.
- `datos/diputados.json`, `datos/bloques.json`: diputados con su bloque y distrito.
- `datos/iniciativas_lista.json` y `datos/iniciativas/`: lista oficial de iniciativas y la ficha de las que aparecen en votaciones.
- `datos/iniciativas_manual.json` (opcional): correcciones o resúmenes editoriales a mano, con el mismo formato de `datos/sitio/iniciativas.json`.
- `datos/sitio/`: lo que lee la página (`indice.json`, más un archivo por votación y uno por diputado).
- `index.html`: la página web (se publica con GitHub Pages: Settings > Pages > Deploy from a branch > main / root).

## Reglas que respeta
- Consulta `robots.txt` antes de cada solicitud y no descarga PDF: solo los enlaza.
- Espera 3 segundos entre solicitudes y se identifica con un nombre propio.
- Solo registra hechos con fuente oficial (votos, asistencia); sin opiniones.

## Prueba sin internet
    python recolector.py --sesiones votaciones_pleno.html --desde 2026-09-01
    python recolector.py --listado 41384.html
    python recolector.py 41388.html
