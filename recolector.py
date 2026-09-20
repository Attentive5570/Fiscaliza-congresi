#!/usr/bin/env python3
"""
Recolector de votaciones del Pleno (Congreso de la República de Guatemala).

Lee UNA página de detalle de votación (por archivo o por URL) y la convierte
en JSON con: sesión, fase, fecha, pregunta y el estado/voto de cada diputado.

Uso:
  # 1) Todo el proceso (lista de sesiones -> listado de cada sesión -> detalle de cada votación)
  python recolector.py --actualizar https://www.congreso.gob.gt/seccion_informacion_legislativa/votaciones_pleno \
                       --desde 2024-01-14 -d datos/

  # 1b) Bancadas: baja la lista de diputados con su bloque y distrito, y arma el archivo del sitio
  python recolector.py --diputados -d datos/
  python recolector.py --iniciativas -d datos/     # título, texto oficial, ponentes y avance de cada iniciativa
  python recolector.py --consolidar -d datos/

  # 2) Pruebas con páginas guardadas (sin internet)
  python recolector.py --sesiones votaciones_pleno.html --desde 2026-09-01   # lista de sesiones
  python recolector.py --listado 41384.html                                  # listado de UNA sesión
  python recolector.py 41388.html                                            # detalle de UNA votación

Dependencias:  pip install beautifulsoup4

Buenas prácticas incluidas:
  - Respeta robots.txt (no pide rutas prohibidas ni PDF).
  - Se identifica con un User-Agent propio (cámbialo por el de tu proyecto).
  - Espera entre solicitudes.
  - Guarda el hash (SHA-256) de la página original para poder auditar el dato.
"""
import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Formato habitual de los bots honestos (como Googlebot): dice quién es y cómo contactarlo.
# El sitio del Congreso rechaza clientes sin identificar (curl/Python por defecto), pero acepta este.
USER_AGENT = "Mozilla/5.0 (compatible; FiscalizaCongresoBot/0.1; proyecto ciudadano; contacto: Attentive5570@proton.me)"
ROBOT_NAME = "FiscalizaCongresoBot"
ESPERA_SEGUNDOS = 3

# id de cada tabla en la página  ->  categoría normalizada
TABLAS = {
    "congreso_a_favor": "a_favor",
    "congreso_contra": "en_contra",
    "congreso_votos_nulos": "ausente",      # el sitio lo usa para la pestaña AUSENCIA
    "congreso_licencia": "licencia_excusa",
}


def _limpiar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


def clave_nombre(nombre: str) -> str:
    """Clave estable para unir con otras listas: minúsculas, sin tildes, sin espacios raros."""
    s = unicodedata.normalize("NFKD", nombre)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", "", s.lower()).strip().replace(" ", "-")


def parsear_detalle(html: str) -> dict:
    sopa = BeautifulSoup(html, "html.parser")

    # --- encabezado: "Detalle de la sesión No. 44, Fase 1, Fecha 17/09/2026 08:52:22"
    titulo = sopa.find(id="title_list")
    partes = [_limpiar(p.get_text()) for p in titulo.find_all("p")] if titulo else []
    texto = " | ".join(partes)

    def buscar(patron):
        m = re.search(patron, texto)
        return m.group(1).strip() if m else None

    sesion = buscar(r"sesi[oó]n No\.\s*(\d+)")
    fase = buscar(r"Fase\s*(\d+)")
    fecha_txt = buscar(r"Fecha\s*([\d/]+\s+[\d:]+)")
    pregunta = buscar(r"Pregunta:\s*(.+?)(?:\s*\||$)")
    numero = buscar(r"N[uú]mero:\s*(\d+)")

    fecha_iso = None
    if fecha_txt:
        fecha_iso = datetime.strptime(fecha_txt, "%d/%m/%Y %H:%M:%S").isoformat()

    # --- tablas de votos
    diputados = []
    for tabla_id, categoria in TABLAS.items():
        tabla = sopa.find("table", id=tabla_id)
        if tabla is None:
            continue
        for fila in tabla.find_all("tr"):
            celdas = [_limpiar(td.get_text()) for td in fila.find_all("td")]
            if not celdas:
                continue  # fila de encabezado
            diputados.append({
                "nombre": celdas[0],
                "clave": clave_nombre(celdas[0]),
                "estado": celdas[1] if len(celdas) > 1 else None,
                "voto": categoria,
            })

    conteo = {}
    for d in diputados:
        conteo[d["voto"]] = conteo.get(d["voto"], 0) + 1

    return {
        "sesion": int(sesion) if sesion else None,
        "fase": int(fase) if fase else None,
        "fecha": fecha_iso,
        "pregunta": pregunta,
        "numero_pregunta": int(numero) if numero else None,
        "conteo": conteo,
        "total_diputados": len(diputados),
        "diputados": diputados,
    }


def validar(reg: dict) -> list:
    """Devuelve una lista de problemas. Lista vacía = el registro es confiable."""
    problemas = []
    if reg["total_diputados"] != 160:
        problemas.append(f"Se esperaban 160 diputados y hay {reg['total_diputados']}.")
    claves = [d["clave"] for d in reg["diputados"]]
    if len(set(claves)) != len(claves):
        problemas.append("Hay nombres repetidos.")
    if not reg["fecha"] or not reg["pregunta"]:
        problemas.append("Falta la fecha o la pregunta: el formato de la página pudo cambiar.")
    return problemas


def parsear_sesiones(html: str) -> list:
    """Página 'Listado de Sesiones' (votaciones_pleno): una fila por sesión, de la más nueva a la más vieja."""
    sopa = BeautifulSoup(html, "html.parser")
    tabla = sopa.find("table", id="congreso_asistencias")
    sesiones = []
    for fila in (tabla.find_all("tr") if tabla else []):
        celdas = fila.find_all("td")
        if len(celdas) < 4:
            continue
        m_id = re.search(r"eventos_votaciones/(\d+)", str(celdas[3]))
        m_f = re.search(r"Fecha\s*([\d/]+\s+[\d:]+)", celdas[2].get_text())
        if not (m_id and m_f):
            continue
        sesiones.append({
            "tipo": _limpiar(celdas[0].get_text()),
            "numero": _limpiar(celdas[1].get_text()),
            "fecha": datetime.strptime(m_f.group(1), "%d/%m/%Y %H:%M:%S").isoformat(),
            "evento_id": int(m_id.group(1)),
            "url": f"https://www.congreso.gob.gt/eventos_votaciones/{m_id.group(1)}",
        })
    return sesiones


def parsear_listado(html: str) -> dict:
    """Página 'Listado de Eventos de Sesión': lista las votaciones (preguntas) de una sesión.

    Se leen las columnas por POSICIÓN y los enlaces por su patrón, no por el texto de los
    encabezados, porque el sitio a veces entrega tildes dañadas ("N?MERO").
    """
    sopa = BeautifulSoup(html, "html.parser")
    titulo = _limpiar(sopa.find(id="title_list").get_text(" ")) if sopa.find(id="title_list") else ""

    def buscar(patron):
        m = re.search(patron, titulo)
        return m.group(1).strip() if m else None

    fecha_txt = buscar(r"Fecha\s*([\d/]+\s+[\d:]+)")
    votaciones = []
    tabla = sopa.find("table", id="congreso_asistencias")
    for fila in (tabla.find_all("tr") if tabla else []):
        celdas = fila.find_all("td")
        if len(celdas) < 5:
            continue
        m = re.search(r"detalle_de_votacion/(\d+)/(\d+)", str(fila))
        if not m:
            continue
        pdf = re.search(r'href="([^"]*pdf_resultado_votacion[^"]*)"', str(fila))
        votaciones.append({
            "pregunta": _limpiar(celdas[0].get_text()),
            "numero_pregunta": int(_limpiar(celdas[1].get_text()) or 0),
            "fecha": datetime.strptime(_limpiar(celdas[2].get_text()), "%d/%m/%Y %H:%M:%S").isoformat(),
            # En la dirección detalle_de_votacion/{A}/{B}: A identifica cada PREGUNTA (votación)
            # y B identifica la sesión (evento). Una sesión con 2 preguntas repite B y cambia A.
            "votacion_id": int(m.group(1)),
            "evento_id": int(m.group(2)),
            "detalle_url": f"https://www.congreso.gob.gt/detalle_de_votacion/{m.group(1)}/{m.group(2)}",
            # solo se enlaza; el robots.txt del sitio pide no descargar PDF
            "pdf_url": pdf.group(1) if pdf else None,
        })
    return {
        "sesion": int(buscar(r"No\.\s*(\d+)")) if buscar(r"No\.\s*(\d+)") else None,
        "fase": int(buscar(r"Fase\s*(\d+)")) if buscar(r"Fase\s*(\d+)") else None,
        "fecha": datetime.strptime(fecha_txt, "%d/%m/%Y %H:%M:%S").isoformat() if fecha_txt else None,
        "tipo": buscar(r"Tipo:\s*(\w+)"),
        "votaciones": votaciones,
    }


_ROBOTS = {}


def _robots(base: str):
    """Lee robots.txt CON nuestro identificador (el lector estándar de Python usa uno genérico y el sitio lo corta)."""
    if base not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                rp.parse(r.read().decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.disallow_all = True
            elif 400 <= e.code < 500:
                rp.allow_all = True
            else:
                raise
        rp.modified()  # marca el archivo como leído; sin esto can_fetch() siempre dice que no
        _ROBOTS[base] = rp
    return _ROBOTS[base]


def descargar(url: str) -> str:
    partes = urlparse(url)
    base = f"{partes.scheme}://{partes.netloc}"
    if not _robots(base).can_fetch(ROBOT_NAME, url):
        raise PermissionError(f"robots.txt no permite pedir: {url}")
    time.sleep(ESPERA_SEGUNDOS)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _armar_registro(html: str, fuente) -> dict:
    reg = parsear_detalle(html)
    reg["fuente_url"] = fuente
    reg["sha256_pagina"] = hashlib.sha256(html.encode("utf-8")).hexdigest()
    reg["recolectado_en"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reg["problemas"] = validar(reg)
    return reg


def _guardar(ruta, obj):
    open(ruta, "w", encoding="utf-8").write(json.dumps(obj, ensure_ascii=False, indent=2))


def actualizar(url_lista: str, desde: str, carpeta: str, maximo: int, dias_recheck: int = 2, presupuesto_min: float = 0):
    """Descubre sesiones nuevas y guarda sus votaciones. Es seguro repetirlo: no vuelve a pedir lo ya guardado."""
    import os
    os.makedirs(os.path.join(carpeta, "sesiones"), exist_ok=True)
    os.makedirs(os.path.join(carpeta, "votaciones"), exist_ok=True)

    sesiones = [x for x in parsear_sesiones(descargar(url_lista)) if x["fecha"][:10] >= desde and x["tipo"] != "Prueba Sistema"]
    print(f"{len(sesiones)} sesiones desde {desde} (sin las de 'Prueba Sistema')")
    ahora = datetime.now()
    pedidas = 0
    inicio = time.monotonic()
    agotado = lambda: presupuesto_min and (time.monotonic() - inicio) > presupuesto_min * 60
    for ses in sesiones:  # de la más nueva a la más vieja
        if agotado():
            print(f"Se agotó el tiempo asignado ({presupuesto_min} min). Lo guardado se conserva; el resto sigue en la próxima corrida.")
            break
        ruta_ses = os.path.join(carpeta, "sesiones", f"{ses['evento_id']}.json")
        reciente = (ahora - datetime.fromisoformat(ses["fecha"])).days <= dias_recheck
        if os.path.exists(ruta_ses) and not reciente:
            continue  # sesión ya cerrada y guardada
        if pedidas >= maximo:
            print(f"Límite de {maximo} sesiones por corrida; el resto queda para la próxima.")
            break
        pedidas += 1
        try:
            lst = parsear_listado(descargar(ses["url"]))
        except Exception as e:
            print(f"  ERROR en la sesión {ses['numero']} ({ses['evento_id']}): {e}")
            continue
        incompleta = False
        for v in lst["votaciones"]:
            ruta = os.path.join(carpeta, "votaciones", f"{v['evento_id']}-{v['votacion_id']}.json")
            if os.path.exists(ruta):
                continue
            if agotado():
                incompleta = True
                break
            try:
                reg = _armar_registro(descargar(v["detalle_url"]), v["detalle_url"])
            except Exception as e:
                print(f"  ERROR en la votación {v['votacion_id']}: {e}")
                continue
            reg.update(votacion_id=v["votacion_id"], evento_id=ses["evento_id"], numero_pregunta=v["numero_pregunta"],
                       tipo_sesion=ses["tipo"], pdf_url=v["pdf_url"])
            _guardar(ruta, reg)
            print(f"  votación {v['votacion_id']} (sesión {ses['numero']}, {v['pregunta'][:40]}): {reg['conteo']} problemas: {reg['problemas'] or 'ninguno'}")
        if incompleta:
            print(f"Sesión {ses['numero']} quedó a medias; se completa en la próxima corrida.")
            break
        _guardar(ruta_ses, {**ses, "listado": lst, "recolectado_en": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def _armar_registro(html: str, fuente) -> dict:
    reg = parsear_detalle(html)
    reg["fuente_url"] = fuente
    reg["sha256_pagina"] = hashlib.sha256(html.encode("utf-8")).hexdigest()
    reg["recolectado_en"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reg["problemas"] = validar(reg)
    return reg


URL_DIPUTADOS = "https://www.congreso.gob.gt/ctrl_diputados_por_distrito/get_distritos_default"
URL_BLOQUES = "https://www.congreso.gob.gt/ctrl_bloques/searcher_blocks"
CODIGO_VOTO = {"a_favor": "F", "en_contra": "C", "ausente": "X", "licencia_excusa": "L"}


def post_json(url: str, payload=None):
    """POST con cuerpo JSON, como lo hace la propia página del Congreso para cargar sus tablas."""
    partes = urlparse(url)
    base = f"{partes.scheme}://{partes.netloc}"
    if not _robots(base).can_fetch(ROBOT_NAME, url):
        raise PermissionError(f"robots.txt no permite pedir: {url}")
    time.sleep(ESPERA_SEGUNDOS)
    cuerpo = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(url, data=cuerpo, method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        datos = json.loads(r.read().decode("utf-8", errors="replace"))
    return json.loads(datos) if isinstance(datos, str) else datos  # a veces llega como texto dentro de JSON


def parsear_diputados(datos: list) -> list:
    """Campos según el JavaScript de la página: id_diputado, nombres, apellidos, id_bloque, nombre_bloque, nombre_distrito.
    La lista de votos escribe 'Apellidos Nombres', así que la clave se arma en ese mismo orden."""
    salida = []
    for it in datos:
        nombre = _limpiar(f"{it.get('apellidos') or ''} {it.get('nombres') or ''}")
        salida.append({
            "id_diputado": it.get("id_diputado"),
            "nombre": nombre,
            "clave": clave_nombre(nombre),
            "id_bloque": it.get("id_bloque"),
            "bloque": _limpiar(it.get("nombre_bloque") or ""),
            "distrito": _limpiar(it.get("nombre_distrito") or ""),
            "perfil_url": f"https://www.congreso.gob.gt/perfil_diputado/{it.get('id_diputado')}",
        })
    return salida


MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
URL_INICIATIVAS = "https://www.congreso.gob.gt/seccion_informacion_legislativa/iniciativas"


def _fecha_larga(texto: str):
    """'Martes, 08 de septiembre de 2026' -> '2026-09-08'"""
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", texto or "")
    if not m or m.group(2).lower() not in MESES:
        return None
    return f"{int(m.group(3)):04d}-{MESES[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def parsear_lista_iniciativas(html: str) -> list:
    """Página 'Iniciativas': una tarjeta por iniciativa con número, fecha en que la conoció el Pleno,
    resumen oficial, enlace al detalle y enlace al PDF (el PDF solo se enlaza; el robots.txt pide no descargarlo)."""
    sopa = BeautifulSoup(html, "html.parser")
    salida = []
    for tarjeta in sopa.select("div.card"):
        cab = tarjeta.select_one(":scope > .card-header")
        cuerpo = tarjeta.select_one(":scope > .card-body")
        if not cab or not cuerpo:
            continue
        m = re.search(r"Iniciativa:\s*(\d+)", cab.get_text(" ", strip=True))
        if not m:
            continue
        textos = [_limpiar(p.get_text()) for p in cuerpo.select("p.card-text")]
        enlaces = [a.get("href") for a in cuerpo.select("a") if a.get("href")]
        detalle = next((e for e in enlaces if "detalle_pdf/iniciativas/" in e), None)
        pdf = next((e for e in enlaces if e.lower().endswith(".pdf")), None)
        salida.append({
            "numero": m.group(1),
            "fecha_pleno": _fecha_larga(textos[0]) if textos else None,
            "texto_oficial": textos[1] if len(textos) > 1 else "",
            "detalle_url": detalle,
            "id_interno": int(re.search(r"/(\d+)$", detalle).group(1)) if detalle else None,
            "pdf_url": pdf,
        })
    return salida


def titulo_corto(texto_oficial: str) -> str:
    """'Iniciativa que dispone aprobar Ley de X.' -> 'Ley de X'"""
    t = _limpiar(texto_oficial).rstrip(". ")
    t = re.sub(r"^Iniciativa\s+(?:de ley\s+)?que\s+dispone\s+(?:aprobar\s+)?", "", t, flags=re.I)
    return t[:1].upper() + t[1:] if t else texto_oficial


def parsear_detalle_iniciativa(html: str) -> dict:
    """Ficha de una iniciativa: número, texto oficial, institución, diputados ponentes, fecha y pasos de avance."""
    sopa = BeautifulSoup(html, "html.parser")
    for x in sopa(["script", "style", "noscript"]):
        x.decompose()
    texto = re.sub(r"[ \t\r\xa0]+", " ", sopa.get_text("\n"))
    texto = re.sub(r"\n\s*\n+", "\n", texto)

    def campo(patron):
        m = re.search(patron, texto, re.S)
        return _limpiar(m.group(1)) if m else None

    ponentes = re.findall(r"^\s*\d+\.-\s*(.+?)\s*$", texto, re.M)
    pasos = []
    for tabla in sopa.find_all("table"):
        encab = [_limpiar(th.get_text()) for th in tabla.find_all("th")]
        if "Paso" in encab and "Estado" in encab:
            for fila in tabla.find_all("tr"):
                celdas = [_limpiar(c.get_text()) for c in fila.find_all("td")]
                if len(celdas) >= 3:
                    f = re.match(r"(\d{2})-(\d{2})-(\d{4})", celdas[1])
                    pasos.append({"paso": celdas[0], "fecha": f"{f.group(3)}-{f.group(2)}-{f.group(1)}" if f else celdas[1],
                                  "estado": celdas[2]})
            break
    return {
        "numero": campo(r"N[uú]mero:\s*(\d+)"),
        "texto_oficial": campo(r"Detalle:\s*(.+?)\s*Instituci[oó]n Ponente:"),
        "institucion": campo(r"Instituci[oó]n Ponente:\s*(.+?)\s*Diputados\s+ponentes:"),
        "ponentes": ponentes,
        "fecha_pleno": _fecha_larga(campo(r"Fecha:\s*(.+?)\s*descargar") or ""),
        "pasos": pasos,
    }


def _citadas(carpeta: str) -> set:
    """Números de iniciativa mencionados en las votaciones ya guardadas."""
    import glob
    import os
    citadas = set()
    for ruta in glob.glob(os.path.join(carpeta, "votaciones", "*.json")):
        citadas.update(iniciativas_de(json.load(open(ruta, encoding="utf-8"))["pregunta"]))
    return citadas


def actualizar_iniciativas(carpeta: str, max_detalles: int = 30, dias_refresco: int = 3):
    """1) Baja la lista oficial de iniciativas (una sola página). 2) Para las iniciativas que aparecen en las
    votaciones, baja su ficha (diputados ponentes y pasos de avance). Repetirlo es seguro."""
    import os
    os.makedirs(os.path.join(carpeta, "iniciativas"), exist_ok=True)
    lista = parsear_lista_iniciativas(descargar(URL_INICIATIVAS))
    print(f"{len(lista)} iniciativas en la lista oficial")
    if len(lista) < 100:
        print("  AVISO: la lista llegó demasiado corta; el formato pudo cambiar. No se guarda nada.")
        return
    _guardar(os.path.join(carpeta, "iniciativas_lista.json"), sorted(lista, key=lambda x: int(x["numero"])))
    por_num = {x["numero"]: x for x in lista}
    citadas = _citadas(carpeta)
    fuera = sorted(n for n in citadas if n not in por_num)
    if fuera:
        print(f"  {len(fuera)} iniciativas citadas en votaciones no están en la lista oficial (son más antiguas): {', '.join(fuera[:15])}")
    pedidos = 0
    for n in sorted(citadas & set(por_num), key=int, reverse=True):
        ruta = os.path.join(carpeta, "iniciativas", f"{n}.json")
        if os.path.exists(ruta):
            previo = json.load(open(ruta, encoding="utf-8"))
            edad = datetime.now(timezone.utc) - datetime.fromisoformat(previo["recolectado_en"])
            if edad.days < dias_refresco:
                continue
        if pedidos >= max_detalles:
            print(f"Límite de {max_detalles} fichas por corrida; el resto queda para la próxima.")
            break
        pedidos += 1
        try:
            det = parsear_detalle_iniciativa(descargar(por_num[n]["detalle_url"]))
        except Exception as e:
            print(f"  ERROR en la ficha de la iniciativa {n}: {e}")
            continue
        det.update(numero=n, fuente_url=por_num[n]["detalle_url"],
                   recolectado_en=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _guardar(ruta, det)
        print(f"  ficha {n}: {len(det['ponentes'])} ponentes, {len(det['pasos'])} pasos")


def descargar_diputados(carpeta: str):
    """Guarda la lista de diputados. Es ACUMULATIVA: quien deja de aparecer queda como inactivo,
    para no perder su historial de votos (renuncias, suplentes, cambios de bloque)."""
    import os
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, "diputados.json")
    previos = {}
    if os.path.exists(ruta):
        previos = {d["clave"]: d for d in json.load(open(ruta, encoding="utf-8"))}
    actuales = parsear_diputados(post_json(URL_DIPUTADOS))
    print(f"{len(actuales)} diputados recibidos (se esperan 160)")
    hoy = datetime.now(timezone.utc).date().isoformat()
    vistos = set()
    for d in actuales:
        vistos.add(d["clave"])
        d["activo"], d["visto"] = True, hoy
        previos[d["clave"]] = {**previos.get(d["clave"], {}), **d}
    if len(actuales) >= 150:
        for k, d in previos.items():
            if k not in vistos:
                d["activo"] = False
    else:
        print("  AVISO: llegaron muy pocos diputados; no se marca a nadie como inactivo.")
    _guardar(ruta, sorted(previos.values(), key=lambda d: d["nombre"]))
    bloques = post_json(URL_BLOQUES, {"target": ""})
    _guardar(os.path.join(carpeta, "bloques.json"), bloques)
    print(f"{len(bloques) if isinstance(bloques, list) else '?'} bloques guardados")


def clasificar(pregunta: str) -> str:
    p = pregunta.upper()
    if re.search(r"TERCER DEBATE|[ÚU]NICO DEBATE", p):
        return "debate_final"
    if "PROYECTO" in p and re.search(r"ART[ÍI]CULO|ENMIENDA|PRE[ÁA]MBULO|REDACCI[ÓO]N FINAL|SEGUNDO DEBATE|PRIMER DEBATE", p):
        return "articulado"
    if re.search(r"ORDEN DEL D[ÍI]A|ACTAS?\b|MOCI[ÓO]N|AGENDA", p):
        return "procedimiento"
    return "otra"


def iniciativas_de(pregunta: str) -> list:
    """Números de iniciativa mencionados en el texto (tolera erratas del original, como 'INICITIVA')."""
    return sorted(set(re.findall(r"INICI\w*\s+(?:DE\s+LEY\s+)?(?:N[Oº°]\.?\s*)?(\d{3,5})", pregunta.upper())))


def consolidar(carpeta: str):
    """Genera los archivos que lee la página, en datos/sitio/:
         indice.json            diputados (con totales), bloques y lista de votaciones
         votos/<id>.json        voto de cada diputado en esa votación (se pide al abrirla)
         diputados/<clave>.json historial de votos de cada diputado (se pide al abrir su ficha)
    """
    import glob
    import os
    dips = json.load(open(os.path.join(carpeta, "diputados.json"), encoding="utf-8"))
    por_clave = {d["clave"]: d for d in dips}
    ruta_alias = os.path.join(carpeta, "alias.json")  # opcional: {"clave-en-votos": "clave-en-lista"}
    alias = json.load(open(ruta_alias, encoding="utf-8")) if os.path.exists(ruta_alias) else {}

    raiz = os.path.join(carpeta, "sitio")
    for sub in ("votos", "diputados"):
        os.makedirs(os.path.join(raiz, sub), exist_ok=True)

    votaciones, votos, historial, desconocidos = [], {}, {}, {}
    for ruta in glob.glob(os.path.join(carpeta, "votaciones", "*.json")):
        v = json.load(open(ruta, encoding="utf-8"))
        vid = f"{v['evento_id']}-{v['votacion_id']}"
        cont = {"F": 0, "C": 0, "X": 0, "L": 0}
        votos[vid] = {}
        for d in v["diputados"]:
            clave = alias.get(d["clave"], d["clave"])
            if clave not in por_clave:
                desconocidos[clave] = d["nombre"]
                por_clave[clave] = {"clave": clave, "nombre": d["nombre"], "bloque": "Sin dato de bloque",
                                    "distrito": "", "activo": False, "id_diputado": None, "perfil_url": None}
            codigo = CODIGO_VOTO[d["voto"]]
            votos[vid][clave] = codigo
            historial.setdefault(clave, {})[vid] = codigo
            cont[codigo] += 1
        votaciones.append({"id": vid, "fecha": v["fecha"], "sesion": v["sesion"], "tipo_sesion": v.get("tipo_sesion"),
                           "pregunta": v["pregunta"], "clase": clasificar(v["pregunta"]),
                           "iniciativas": iniciativas_de(v["pregunta"]), "conteo": cont,
                           "fuente_url": v["fuente_url"], "pdf_url": v.get("pdf_url"), "problemas": v["problemas"]})
    votaciones.sort(key=lambda x: x["fecha"], reverse=True)

    lista = []
    for clave, d in sorted(por_clave.items(), key=lambda kv: kv[1]["nombre"]):
        h = historial.get(clave, {})
        tot = {"F": 0, "C": 0, "X": 0, "L": 0}
        for c in h.values():
            tot[c] += 1
        lista.append({**{k: d.get(k) for k in ("clave", "nombre", "bloque", "distrito", "id_diputado", "perfil_url")},
                      "activo": d.get("activo", True),
                      "totales": tot, "n": len(h)})
        _escribir(os.path.join(raiz, "diputados", f"{clave}.json"), h)
    for vid, m in votos.items():
        _escribir(os.path.join(raiz, "votos", f"{vid}.json"), m)

    # --- iniciativas (título, texto oficial, ponentes, pasos) ---
    def _fichas():
        ruta_l = os.path.join(carpeta, "iniciativas_lista.json")
        por_num = {x["numero"]: x for x in json.load(open(ruta_l, encoding="utf-8"))} if os.path.exists(ruta_l) else {}
        por_tokens = {tuple(sorted(clave_nombre(d["nombre"]).split("-"))): c for c, d in por_clave.items()}
        manual = {}
        ruta_m = os.path.join(carpeta, "iniciativas_manual.json")  # correcciones o resúmenes editoriales, a mano
        if os.path.exists(ruta_m):
            manual = json.load(open(ruta_m, encoding="utf-8"))
        salida = {}
        for n in sorted({n for v in votaciones for n in v["iniciativas"]}):
            base = por_num.get(n)
            ruta_d = os.path.join(carpeta, "iniciativas", f"{n}.json")
            det = json.load(open(ruta_d, encoding="utf-8")) if os.path.exists(ruta_d) else {}
            if not base and not det and n not in manual:
                continue
            texto = det.get("texto_oficial") or (base or {}).get("texto_oficial") or ""
            ficha = {"titulo": titulo_corto(texto) if texto else "", "texto_oficial": texto,
                     "fecha_pleno": det.get("fecha_pleno") or (base or {}).get("fecha_pleno"),
                     "url": (base or {}).get("detalle_url"), "pdf": (base or {}).get("pdf_url"),
                     "institucion": det.get("institucion"), "pasos": det.get("pasos", []),
                     "ponentes": [{"nombre": nom, "clave": por_tokens.get(tuple(sorted(clave_nombre(nom).split("-"))))}
                                  for nom in det.get("ponentes", [])]}
            ficha.update(manual.get(n, {}))
            salida[n] = ficha
        return salida
    fichas = _fichas()
    _escribir(os.path.join(raiz, "iniciativas.json"), fichas)

    bloques = {}
    for d in lista:
        if d["activo"] or d["n"]:
            bloques[d["bloque"]] = bloques.get(d["bloque"], 0) + 1
    fechas = [x["fecha"] for x in votaciones]
    indice = {"generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "cobertura": {"desde": min(fechas) if fechas else None, "hasta": max(fechas) if fechas else None,
                            "votaciones": len(votaciones)},
              "diputados": lista, "votaciones": votaciones,
              "sin_dato_de_bloque": sorted(desconocidos.values())}
    _escribir(os.path.join(raiz, "indice.json"), indice)
    print(f"{len(votaciones)} votaciones, {len(lista)} diputados. Sin dato de bloque: {len(desconocidos)}")
    for n in sorted(desconocidos.values())[:20]:
        print("  no coincide con la lista de diputados:", n)


def _escribir(ruta, obj):
    open(ruta, "w", encoding="utf-8").write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archivo", nargs="?", help="HTML guardado de una página de detalle de votación")
    ap.add_argument("--url", help="URL de una página de detalle de votación")
    ap.add_argument("--listado", help="HTML guardado del listado de UNA sesión (eventos_votaciones)")
    ap.add_argument("--sesiones", help="HTML guardado de la lista de todas las sesiones (votaciones_pleno)")
    ap.add_argument("--actualizar", help="URL de la lista de sesiones: descarga lo nuevo")
    ap.add_argument("--diputados", action="store_true", help="baja la lista de diputados con bloque y distrito")
    ap.add_argument("--iniciativas", action="store_true", help="baja la lista oficial de iniciativas y la ficha de las citadas en votaciones")
    ap.add_argument("--consolidar", action="store_true", help="une votaciones y bloques en datos/sitio/ (lo que lee la página)")
    ap.add_argument("--desde", default="2024-01-14", help="fecha mínima AAAA-MM-DD (por defecto, inicio de la legislatura actual)")
    ap.add_argument("--presupuesto", type=float, default=0, help="minutos máximos de trabajo; al agotarse guarda lo hecho y termina")
    ap.add_argument("--max", type=int, default=25, help="máximo de sesiones a consultar por corrida")
    ap.add_argument("-d", "--carpeta", default="datos", help="carpeta de salida")
    ap.add_argument("--fuente", help="URL de origen (si usas archivo)")
    ap.add_argument("-o", "--salida", help="archivo JSON de salida (por defecto, pantalla)")
    a = ap.parse_args()

    if a.diputados:
        descargar_diputados(a.carpeta)
        return
    if a.iniciativas:
        actualizar_iniciativas(a.carpeta)
        return
    if a.consolidar:
        consolidar(a.carpeta)
        return
    if a.sesiones:
        L = [x for x in parsear_sesiones(open(a.sesiones, encoding="utf-8", errors="replace").read()) if x["fecha"][:10] >= a.desde]
        print(json.dumps(L, ensure_ascii=False, indent=2))
        return
    if a.listado:
        print(json.dumps(parsear_listado(open(a.listado, encoding="utf-8", errors="replace").read()), ensure_ascii=False, indent=2))
        return
    if a.actualizar:
        actualizar(a.actualizar, a.desde, a.carpeta, a.max, presupuesto_min=a.presupuesto)
        return

    if a.url:
        html, fuente = descargar(a.url), a.url
    elif a.archivo:
        html, fuente = open(a.archivo, encoding="utf-8", errors="replace").read(), a.fuente
    else:
        ap.error("indica un archivo, --url, --listado, --sesiones o --actualizar")

    reg = _armar_registro(html, fuente)
    salida = json.dumps(reg, ensure_ascii=False, indent=2)
    if a.salida:
        open(a.salida, "w", encoding="utf-8").write(salida)
        print(f"Guardado en {a.salida}. Conteo: {reg['conteo']}. Problemas: {reg['problemas'] or 'ninguno'}")
    else:
        print(salida)


if __name__ == "__main__":
    main()
