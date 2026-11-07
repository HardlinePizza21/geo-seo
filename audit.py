#!/usr/bin/env python3
"""Auditoría de metatags en Drupal, trabajo conjunto usuario + script.

Paso 1 (mapeo): graba los campos que tocas en el panel de edición de un nodo.
    python audit.py record --site https://www.eafit.edu.co
    -> grabacion_campos.json (solo selectores y etiquetas)

Paso 2 (generación/validación por chunks de 10 páginas; dry-run salvo --apply):
    python audit.py run [--apply] [--chunk 10] [--source resultado.json] [--out auditoria.json]

Paso 3 (reinicio del progreso sin borrar evidencia):
    python audit.py reset --out auditoria.json

Los campos con contenido se preservan y pueden alimentar Ollama. Los campos vacíos siguen
las instrucciones estructuradas de instrucciones_seo.json. Solo --apply escribe en Drupal.
Los valores existentes que exceden el máximo o duplican el abstract se señalan para revisión;
no se modifican automáticamente, incluso con --redo.
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests

from graph_store import ROOT, load_graph, save_graph

STATE_DIR = "drupal_state"
PROFILE = os.path.join(STATE_DIR, "profile")  # perfil del navegador con la sesión Microsoft; no versionar
CAMPOS = "campos.json"
INSTRUCCIONES = "instrucciones_seo.json"
GRABACION = "grabacion_campos.json"
OLLAMA_DEFAULT = {
    "endpoint": "http://127.0.0.1:11434/api/generate",
    "nombre": "gemma3:4b",
    "timeout": 180,
}
# Sin esto el sitio detecta la automatización y no carga el JS de Drupal (menú contextual, etc.)
LAUNCH_OPTS = {"ignore_default_args": ["--enable-automation"], "args": ["--disable-blink-features=AutomationControlled"]}

RECORD_JS = """
(() => {
  const enEdicionDrupal = () => /^\\/node\\/\\d+\\/edit\\/?$/.test(location.pathname);
  const describe = (el) => {
    if (!enEdicionDrupal() || !el || !['INPUT','TEXTAREA','SELECT'].includes(el.tagName)) return null;
    if (['hidden', 'password', 'submit', 'button', 'file', 'image'].includes((el.type || '').toLowerCase())) return null;
    if (!el.name && !el.id) return null;
    const lab = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    const group = el.closest('details');
    return {
      tag: el.tagName.toLowerCase(), type: el.type || null, id: el.id || null, name: el.name || null,
      selector: el.id ? `#${CSS.escape(el.id)}` : `[name="${el.name.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"')}"]`,
      label: lab ? lab.innerText.trim() : null,
      seccion: group && group.querySelector('summary') ? group.querySelector('summary').innerText.trim() : null,
    };
  };
  const mantenerCheckboxRobotsMarcado = (el) => {
    if (enEdicionDrupal() && el && el.tagName === 'INPUT' && el.type === 'checkbox' &&
        /\\[robots\\]\\[(index|follow)\\]$/.test(el.name)) {
      el.checked = true;
    }
  };
  document.addEventListener('focusin', (ev) => {
    const d = describe(ev.target);
    if (d) {
      mantenerCheckboxRobotsMarcado(ev.target);
      window.__grabar(JSON.stringify({evento: 'focusin', ...d}));
    }
  }, true);
  document.addEventListener('input', (ev) => mantenerCheckboxRobotsMarcado(ev.target), true);
  document.addEventListener('change', (ev) => mantenerCheckboxRobotsMarcado(ev.target), true);
})();
"""


class AuditProcessingError(RuntimeError):
    """Error esperado de configuración, generación o validación de una página."""


def normalizar_texto(texto):
    descompuesto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in descompuesto if not unicodedata.combining(c)).lower()


def clave_campo(evento):
    nombre = normalizar_texto(evento.get("name", "")).replace("_", " ")
    etiqueta = normalizar_texto(evento.get("label", "")).replace("_", " ")
    seccion = normalizar_texto(evento.get("seccion", "")).replace("_", " ")
    texto = f"{nombre} {etiqueta} {seccion}"

    if "open graph" in texto or "open_graph" in evento.get("name", ""):
        if "description" in texto or "descripcion" in texto:
            return "og_description"
        if "title" in texto or "titulo" in texto:
            return "og_title"
        if "image height" in texto or "altura" in texto:
            return "og_image_height"
        if "image url" in texto or "image_url" in nombre:
            return "og_image_url"
        if "image" in texto or "imagen" in texto:
            return "og_image"
        if "site name" in texto or "nombre del sitio" in texto:
            return "og_site_name"
        if "locale" in texto:
            return "og_locale"
        if "type" in texto or "tipo" in texto:
            return "og_type"
        if "url" in texto:
            return "og_url"

    if "schema" in texto:
        if "description" in texto or "descripcion" in texto:
            return "schema_description"
        if "type" in texto or "tipo" in texto:
            return "schema_type"
        if "name" in texto or "nombre" in texto:
            return "schema_name"

    if "canonical" in texto:
        return "canonical_url"
    if "robots" in texto and "index" in texto:
        return "robots_index"
    if "robots" in texto and "follow" in texto:
        return "robots_follow"
    if "basic tags" in seccion and ("title" in texto or "titulo" in texto):
        return "page_title"
    if "keyword" in texto or "palabra clave" in texto:
        return "keywords"
    if "abstract" in texto or "resumen" in texto:
        return "abstract"
    if "description" in texto or "descripcion" in texto:
        return "description"
    if "page title" in texto or "titulo de la pagina" in texto:
        return "page_title"

    slug = re.sub(r"[^a-z0-9]+", "_", normalizar_texto(evento.get("name") or evento.get("label") or "campo")).strip("_")
    return f"campo_{slug or 'sin_nombre'}"


def construir_campos_grabados(eventos, campos_anteriores=None):
    campos_anteriores = campos_anteriores or {}
    valores_fijos = {
        "canonical_url": "[current-page:url:absolute]",
        "robots_index": True,
        "robots_follow": True,
        "og_site_name": "[site:name]",
        "og_url": "[current-page:url]",
        "og_image_url": "[node:field_image:entity:url]",
        "og_image_height": "630px",
        "og_locale": "es_CO",
    }
    tecnicos = {
        "canonical_url", "robots_index", "robots_follow", "og_url", "og_image",
        "og_image_url", "og_image_height", "og_type", "og_locale",
    }
    resultado = {}
    selectores = set()
    for evento in eventos:
        selector = evento.get("selector")
        if evento.get("evento") != "focusin" or not selector or selector in selectores:
            continue
        selectores.add(selector)
        clave = clave_campo(evento)
        base = clave
        sufijo = 2
        while clave in resultado:
            clave = f"{base}_{sufijo}"
            sufijo += 1
        tipo = (evento.get("type") or "").lower()
        campo = {
            "selector": selector,
            "tipo": tipo,
            "label": evento.get("label") or clave,
            "seccion": evento.get("seccion"),
            "contexto": tipo not in {"checkbox", "radio"} and clave not in tecnicos,
        }
        anterior = campos_anteriores.get(base, {})
        for propiedad in ("default", "solo_si_og_image"):
            if propiedad in anterior:
                campo[propiedad] = anterior[propiedad]
        if "default" not in campo and base in valores_fijos:
            campo["default"] = valores_fijos[base]
        if base in {"og_image_url", "og_image_height"}:
            campo["solo_si_og_image"] = True
        resultado[clave] = campo
    return resultado


def cargar_instrucciones(path=INSTRUCCIONES):
    with open(path, encoding="utf-8") as f:
        configuracion = json.load(f)
    if not isinstance(configuracion, dict) or not isinstance(configuracion.get("campos"), list):
        raise ValueError(f"{path} debe contener una lista de campos bajo la clave 'campos'.")
    instrucciones = configuracion["campos"]
    claves = [item.get("campo") for item in instrucciones]
    if any(not clave for clave in claves) or len(claves) != len(set(claves)):
        raise ValueError(f"{path} tiene claves de campo vacías o duplicadas.")
    if any(item.get("modo") not in {"modelo", "copiar", "manual"} for item in instrucciones):
        raise ValueError(f"{path} contiene un modo de generación no reconocido.")
    if not isinstance(configuracion.get("checklist", []), list):
        raise ValueError(f"{path} debe contener una lista en 'checklist'.")
    return configuracion


def cargar_campos_grabados(path=GRABACION):
    try:
        with open(path, encoding="utf-8") as f:
            grabacion = json.load(f)
    except FileNotFoundError as exc:
        raise AuditProcessingError(f"Primero ejecuta `python audit.py record` para crear {path}.") from exc
    if not isinstance(grabacion, dict) or not isinstance(grabacion.get("campos"), dict) or not grabacion["campos"]:
        raise AuditProcessingError(f"{path} no contiene campos grabados. Ejecuta `python audit.py record`.")
    return grabacion["campos"]


def validar_valor_generado(campo, valor):
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("el modelo devolvió un valor vacío o no textual")
    valor = valor.strip()
    normalizado = normalizar_texto(valor).strip(" .,:;!?")
    if normalizado in {"null", "none", "undefined", "n/a", "na", "no aplica", "sin datos", "sin informacion"}:
        raise ValueError("el modelo devolvió un marcador de ausencia de contenido")
    letras = [caracter for caracter in valor if caracter.isalpha()]
    if letras and all(caracter.isupper() for caracter in letras):
        raise ValueError("el valor contiene solo siglas o texto en mayúsculas")
    minimo_palabras = campo.get("min_palabras")
    if minimo_palabras is not None and len(valor.split()) < minimo_palabras:
        raise ValueError(f"tiene menos de {minimo_palabras} palabras")
    minimo = campo.get("min_caracteres")
    maximo = campo.get("max_caracteres")
    if minimo is not None and len(valor) < minimo:
        raise ValueError(f"tiene {len(valor)} caracteres; el mínimo es {minimo}")
    if maximo is not None and len(valor) > maximo:
        raise ValueError(f"tiene {len(valor)} caracteres; el máximo es {maximo}")
    if campo.get("campo") == "keywords":
        terminos = [termino.strip() for termino in valor.split(",")]
        if len(terminos) < 2 or any(not termino for termino in terminos):
            raise ValueError("las palabras clave deben ser términos no vacíos separados por comas")
    return valor


def ajustar_sobrelongitud(campo, valor):
    maximo = campo.get("max_caracteres")
    if (
        not isinstance(valor, str)
        or campo.get("campo") not in {"description", "abstract"}
        or maximo is None
        or len(valor) <= maximo
        or len(valor) - maximo > 10
    ):
        return valor
    prefijo = valor[:maximo]
    if " " not in prefijo:
        return valor
    ajustado = prefijo.rsplit(" ", 1)[0].rstrip(" ,;:-")
    conectores = {"a", "al", "con", "de", "del", "el", "en", "la", "las", "los", "o", "para", "por", "que", "y"}
    while ajustado and ajustado.split()[-1].lower().strip(".,;:") in conectores:
        ajustado = ajustado.rsplit(" ", 1)[0].rstrip(" ,;:-")
    if len(ajustado) < maximo and not ajustado.endswith((".", "!", "?")):
        ajustado += "."
    return ajustado


def textos_iguales(primero, segundo):
    if not isinstance(primero, str) or not isinstance(segundo, str):
        return False
    def comparable(texto):
        return " ".join(texto.casefold().split()).strip(" .,:;!?")
    return bool(comparable(primero)) and comparable(primero) == comparable(segundo)


def advertencias_campos(campos, instrucciones):
    advertencias = []
    for regla in instrucciones:
        nombre = regla["campo"]
        valor = campos.get(nombre, {}).get("nuevo")
        maximo = regla.get("max_caracteres")
        if isinstance(valor, str) and maximo is not None and len(valor) > maximo:
            advertencias.append((
                nombre, f"excede_limite_{nombre}",
                f"tiene {len(valor)} caracteres; el máximo es {maximo}",
            ))
    abstract = campos.get("abstract", {}).get("nuevo")
    description = campos.get("description", {}).get("nuevo")
    if textos_iguales(abstract, description):
        advertencias.append(("abstract", "abstract_igual_description", "es igual a description"))
    return advertencias


def generar_con_ollama(campos_pendientes, instrucciones, contexto, configuracion):
    endpoint = configuracion.get("endpoint", OLLAMA_DEFAULT["endpoint"])
    url = urlparse(endpoint)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AuditProcessingError("El endpoint de Ollama debe apuntar a localhost.")

    modelo = configuracion.get("nombre", OLLAMA_DEFAULT["nombre"])
    timeout = configuracion.get("timeout", OLLAMA_DEFAULT["timeout"])
    esquema = {
        "type": "object",
        "properties": {clave: {"type": ["string", "null"]} for clave in campos_pendientes},
        "required": list(campos_pendientes),
        "additionalProperties": False,
    }
    prompt = {
        "tarea": (
            "Completa únicamente los campos solicitados. No dejes campos vacíos ni uses solo siglas. "
            "Usa solo hechos del contexto, "
            "no inventes ciudad, público, servicios ni datos institucionales. Si falta "
            "información factual para cumplir la instrucción, devuelve null para ese campo. "
            "Cuenta los caracteres antes de responder: espacios y signos también cuentan. "
            "Abstract y description son campos distintos: no copies el mismo texto en ambos."
        ),
        "contexto": contexto,
        "campos_a_generar": [
            {"campo": clave, "instruccion": instrucciones[clave]["instruccion"],
             "min_caracteres": instrucciones[clave].get("min_caracteres"),
             "max_caracteres": instrucciones[clave].get("max_caracteres"),
             "min_palabras": instrucciones[clave].get("min_palabras"),
             "longitud_objetivo": (
                 f"{instrucciones[clave]['min_caracteres']}-{max(instrucciones[clave]['min_caracteres'], instrucciones[clave]['max_caracteres'] - 5)} caracteres"
                 if instrucciones[clave].get("min_caracteres") is not None
                 and instrucciones[clave].get("max_caracteres") is not None
                 else f"máximo {max(1, instrucciones[clave]['max_caracteres'] - 10)} caracteres"
                 if instrucciones[clave].get("max_caracteres") is not None
                 else None
             )}
            for clave in campos_pendientes
        ],
        "respuesta": "Un objeto JSON con exactamente las claves solicitadas; cada valor es texto o null.",
    }
    try:
        respuesta = requests.post(
            endpoint,
            json={
                "model": modelo,
                "prompt": json.dumps(prompt, ensure_ascii=False),
                "stream": False,
                "format": esquema,
                "options": {"temperature": 0.2},
            },
            timeout=timeout,
        )
        respuesta.raise_for_status()
    except requests.RequestException as exc:
        raise AuditProcessingError(f"No se pudo consultar Ollama ({modelo}): {exc}") from exc
    try:
        cuerpo = respuesta.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise AuditProcessingError("Ollama devolvió una respuesta HTTP que no es JSON.") from exc
    if not isinstance(cuerpo, dict) or not isinstance(cuerpo.get("response"), str):
        raise AuditProcessingError("La respuesta de Ollama no contiene el texto JSON esperado.")
    try:
        resultado = json.loads(cuerpo["response"])
    except json.JSONDecodeError as exc:
        raise AuditProcessingError("Ollama devolvió contenido que no es JSON válido.") from exc
    if not isinstance(resultado, dict) or set(resultado) != set(campos_pendientes):
        raise AuditProcessingError("La respuesta de Ollama no contiene exactamente los campos solicitados.")
    return resultado


def generar_campos_validados(campos_pendientes, instrucciones, contexto, configuracion):
    pendientes = list(campos_pendientes)
    aceptados = {}
    errores = {}
    contexto_actual = contexto
    for intento in range(3):
        salidas = generar_con_ollama(pendientes, instrucciones, contexto_actual, configuracion)
        corregir = []
        motivos = {}
        for nombre in pendientes:
            valor = salidas[nombre]
            if valor is None:
                errores[nombre] = "Falta información factual; requiere revisión humana."
                continue
            valor = ajustar_sobrelongitud(instrucciones[nombre], valor)
            try:
                aceptados[nombre] = validar_valor_generado(instrucciones[nombre], valor)
                errores.pop(nombre, None)
            except ValueError as exc:
                if intento == 0:
                    corregir.append(nombre)
                    motivos[nombre] = {
                        "valor_rechazado": valor,
                        "motivo": str(exc),
                        "instruccion": (
                            "Reescribe este valor para que cumpla el rango indicado. Cuenta los caracteres "
                            "y apunta a cinco caracteres por debajo del máximo; no agregues hechos."
                        ),
                    }
                else:
                    if intento < 2:
                        corregir.append(nombre)
                        motivos[nombre] = {
                            "valor_rechazado": valor,
                            "motivo": str(exc),
                            "instruccion": (
                                "La corrección anterior aún incumplió el límite. Haz una versión más breve "
                                "que conserve los hechos y cumple el rango exactamente."
                            ),
                        }
                    else:
                        errores[nombre] = str(exc)
        existentes = contexto.get("campos_seleccionados_llenos", {})
        description = aceptados.get("description", existentes.get("description", {}).get("valor"))
        abstract = aceptados.get("abstract", existentes.get("abstract", {}).get("valor"))
        if textos_iguales(abstract, description):
            nombre = "abstract" if "abstract" in aceptados else (
                "description" if "description" in aceptados else None
            )
            if nombre:
                valor = aceptados.pop(nombre)
                otro = "description" if nombre == "abstract" else "abstract"
                motivo = f"es igual a {otro}; ambos campos deben tener textos distintos"
                if intento < 2:
                    corregir.append(nombre)
                    motivos[nombre] = {
                        "valor_rechazado": valor,
                        "motivo": motivo,
                        "instruccion": "Reescribe el campo con un texto diferente, sin inventar datos y respetando sus límites.",
                    }
                else:
                    errores[nombre] = motivo
        if not corregir:
            break
        contexto_actual = {
            **contexto,
            "correcciones_solicitadas": motivos,
            "nota_de_correccion": (
                "Corrige únicamente los campos indicados. Respeta sus límites y conserva los hechos "
                "del contexto; no agregues información nueva para alcanzar una longitud."
            ),
        }
        pendientes = corregir
    return aceptados, errores


def reiniciar_estados(nodes):
    reiniciados = 0
    for url, nodo in nodes.items():
        if url != ROOT and "estado" in nodo:
            del nodo["estado"]
            reiniciados += 1
    return reiniciados


def campos_propuestos_por_ia(nodo):
    return {
        nombre: campo
        for nombre, campo in nodo.get("campos", {}).items()
        if campo.get("origen") == "modelo"
    }


def cmd_record(args):
    from playwright.sync_api import sync_playwright

    os.makedirs(STATE_DIR, exist_ok=True)
    eventos = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, **LAUNCH_OPTS)

        def grabar(_source, payload):
            e = json.loads(payload)
            if e not in eventos:
                eventos.append(e)
                print(f"  campo [{e['seccion']}] {e['label']!r} name={e['name']} id={e['id']}")

        ctx.expose_binding("__grabar", grabar)
        ctx.add_init_script(RECORD_JS)
        page = ctx.new_page()
        page.goto(args.site)
        print("En la ventana del navegador, abre un nodo en edición y enfoca cada campo que quieras seleccionar.")
        print("Solo se registran selectores y etiquetas dentro de /node/<id>/edit; nunca se guardan valores.")
        input("Cuando termines de seleccionar los campos, presiona Enter aquí en la terminal... ")
        ctx.close()
    with open(CAMPOS, encoding="utf-8") as f:
        configuracion = json.load(f)
    campos = construir_campos_grabados(eventos, configuracion.get("campos", {}))
    if not campos:
        raise AuditProcessingError("No se grabaron campos. No se modificaron las configuraciones existentes.")
    with open(GRABACION, "w", encoding="utf-8") as f:
        json.dump({"campos": campos}, f, ensure_ascii=False, indent=1)
    print(f"{len(campos)} campos seleccionados -> {GRABACION}")


def leer(page, campo):
    el = page.locator(campo["selector"]).first
    if el.count() == 0:
        return None, None
    if campo.get("tipo") == "checkbox":
        return el, el.is_checked()
    return el, el.input_value()


def og_image_check(url, page_url, page):
    """Devuelve (url_efectiva, status, ok). Si el valor tiene tokens, usa el og:image publicado."""
    if not url or "[" in url or not url.startswith(("http", "/")):
        try:
            r = requests.get(page_url, timeout=15, headers={"User-Agent": "site-graph-audit/1.0"})
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', r.text)
            url = m.group(1) if m else None
        except requests.RequestException:
            url = None
        if not url:
            return None, None, False
    url = urljoin(page_url, url)
    try:
        r = requests.head(url, timeout=15, allow_redirects=True)
        if r.status_code >= 400 or r.status_code == 405:
            r = requests.get(url, timeout=15, stream=True)
        return url, r.status_code, r.status_code < 400
    except requests.RequestException:
        return url, 0, False


EDIT_LINK = "li.entitynodeedit-form a[href*='/edit']"


def enlace_editar(page):
    """Enlace Edit del menú contextual (el botón del banner); se carga por AJAX y a veces requiere abrir el menú."""
    link = page.locator(EDIT_LINK)
    try:
        link.first.wait_for(state="attached", timeout=15000)
    except Exception:
        trig = page.locator("div.contextual button.trigger").first
        if trig.count() == 0:
            return None
        trig.dispatch_event("click")
        try:
            link.first.wait_for(state="attached", timeout=5000)
        except Exception:
            return None
    return link.first.get_attribute("href")


def valor_vacio(valor, campo):
    if campo.get("tipo") == "checkbox" and "default" in campo:
        return valor != bool(campo["default"])
    return not str(valor or "").strip()


def extraer_contexto_pagina(page):
    datos = page.evaluate("""() => {
      const limpio = (el) => (el?.innerText || '').replace(/\\s+/g, ' ').trim();
      const h1 = Array.from(document.querySelectorAll('h1')).map(limpio).filter(Boolean);
      const main = document.querySelector('main, [role="main"], article') || document.body;
      const intro = Array.from(main.querySelectorAll('p')).map(limpio).find(Boolean) || '';
      const imagenes = Array.from(main.querySelectorAll('img'));
      return {
        h1_count: h1.length,
        h1: h1[0] || '',
        primer_parrafo: intro,
        imagenes_sin_alt: imagenes.filter((img) => !img.hasAttribute('alt') || !img.getAttribute('alt').trim()).length
      };
    }""")
    datos["h1"] = datos["h1"][:1000]
    datos["primer_parrafo"] = datos["primer_parrafo"][:3000]
    return datos


def construir_contexto_campos(campos, valores, elementos):
    contexto = {}
    tecnicos = {
        "canonical_url", "robots_index", "robots_follow", "og_url", "og_image",
        "og_image_url", "og_image_height", "og_type", "og_locale",
    }
    for nombre, campo in campos.items():
        previo = valores.get(nombre)
        es_contexto = campo.get(
            "contexto",
            nombre not in tecnicos and campo.get("tipo") not in {"checkbox", "radio"},
        )
        if nombre in elementos and es_contexto and str(previo or "").strip():
            contexto[nombre] = {
                "etiqueta": campo.get("label", nombre),
                "valor": str(previo),
            }
    return contexto


def procesar(ctx, url, campos, instrucciones, checklist, modelo_cfg, apply, guardar_sel, titulo=""):
    info = {"campos": {}, "marcas": [], "revisado_en": datetime.now().isoformat(timespec="seconds")}
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    contenido = extraer_contexto_pagina(page)
    info["revision_contenido"] = {
        **contenido,
        "instrucciones_pdf": checklist,
    }
    if contenido["h1_count"] != 1:
        info["marcas"].append("h1_ausente" if not contenido["h1_count"] else "h1_no_unico")
    if not contenido["primer_parrafo"]:
        info["marcas"].append("primer_parrafo_ausente")
    if contenido["imagenes_sin_alt"]:
        info["marcas"].append("imagenes_sin_alt")

    href = enlace_editar(page)
    if not href:
        info["marcas"].append("sin_acceso")
        return info, page
    info["node_edit"] = href
    page.goto(urljoin(url, href), wait_until="domcontentloaded")
    if not re.search(r"/node/\d+/edit", page.url):
        info["marcas"].append("edicion_no_abrio")
        return info, page
    page.wait_for_load_state("load")
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")

    reglas = {regla["campo"]: regla for regla in instrucciones}
    valores = {}
    elementos = {}
    faltantes = set()
    for nombre, campo in campos.items():
        el, previo = leer(page, campo)
        if el is None:
            faltantes.add(nombre)
            info["campos"][nombre] = {"previo": None, "nuevo": None, "accion": "campo_no_encontrado"}
            info["marcas"].append(f"falta_{nombre}")
            continue
        elementos[nombre] = el
        valores[nombre] = previo

    for regla in instrucciones:
        nombre = regla["campo"]
        if nombre not in campos:
            info["marcas"].append(f"sin_selector_{nombre}")

    og_img_val = (valores.get("og_image") or "").strip()
    contexto = {
        "campos_seleccionados_llenos": construir_contexto_campos(campos, valores, elementos),
        "pagina": {
            "url": url,
            "titulo": titulo,
            "h1": contenido["h1"],
            "primer_parrafo": contenido["primer_parrafo"],
        },
    }

    pendientes_modelo = [
        regla["campo"] for regla in instrucciones
        if regla["modo"] == "modelo"
        and regla["campo"] in elementos
        and valor_vacio(valores[regla["campo"]], campos[regla["campo"]])
    ]
    salidas_modelo = {}
    errores_modelo = []
    errores_por_campo = {}
    if pendientes_modelo:
        info["modelo_ia"] = modelo_cfg["nombre"]
        try:
            salidas_modelo, errores_por_campo = generar_campos_validados(
                pendientes_modelo,
                reglas,
                contexto,
                modelo_cfg,
            )
            errores_modelo.extend(f"{nombre}: {error}" for nombre, error in errores_por_campo.items())
        except AuditProcessingError as exc:
            errores_modelo.append(str(exc))
            errores_por_campo = {nombre: str(exc) for nombre in pendientes_modelo}

    propuestos = {}
    for regla in instrucciones:
        nombre = regla["campo"]
        if nombre not in elementos:
            continue
        previo = valores[nombre]
        campo = campos[nombre]
        if not valor_vacio(previo, campo):
            info["campos"][nombre] = {"previo": previo, "nuevo": previo, "accion": "sin_cambios"}
            continue

        if campo.get("solo_si_og_image") and not og_img_val:
            info["campos"][nombre] = {
                "previo": previo, "nuevo": previo, "accion": "omitido_sin_og_image",
            }
            continue

        if regla["modo"] == "modelo":
            if nombre in errores_por_campo:
                info["campos"][nombre] = {
                    "previo": previo, "nuevo": salidas_modelo.get(nombre),
                    "accion": "error_modelo", "error": errores_por_campo[nombre],
                    "origen": "modelo", "modelo": modelo_cfg["nombre"],
                }
                continue
            nuevo = salidas_modelo.get(nombre)
            if nuevo is None:
                error = "El modelo indicó que falta información factual; requiere revisión humana."
                errores_modelo.append(f"{nombre}: {error}")
                errores_por_campo[nombre] = error
                info["campos"][nombre] = {
                    "previo": previo, "nuevo": None, "accion": "requiere_revision", "error": error,
                }
                continue
        elif regla["modo"] == "manual":
            if "default" in campo:
                nuevo = campo["default"]
                if isinstance(nuevo, str):
                    nuevo = nuevo.replace("{titulo}", titulo)
                if campo.get("tipo") == "checkbox":
                    nuevo = bool(nuevo)
                valores[nombre] = nuevo
                propuestos[nombre] = nuevo
                info["campos"][nombre] = {
                    "previo": previo, "nuevo": nuevo, "accion": "propuesto",
                    "origen": "predeterminado",
                }
                continue
            error = regla["instruccion"]
            info["campos"][nombre] = {
                "previo": previo, "nuevo": None, "accion": "requiere_revision", "error": error,
            }
            info["marcas"].append(f"requiere_revision_{nombre}")
            continue
        else:
            nuevo = valores.get(regla["origen"])
            if not str(nuevo or "").strip():
                error = f"No hay valor disponible en el campo fuente {regla['origen']}."
                info["campos"][nombre] = {
                    "previo": previo, "nuevo": None, "accion": "requiere_revision", "error": error,
                }
                info["marcas"].append(f"fuente_vacia_{regla['origen']}")
                continue
            nuevo = str(nuevo).strip()
            maximo = regla.get("max_caracteres")
            if maximo is not None and len(nuevo) > maximo:
                error = f"El valor copiado desde {regla['origen']} excede {maximo} caracteres."
                info["campos"][nombre] = {
                    "previo": previo, "nuevo": nuevo, "accion": "requiere_revision", "error": error,
                }
                info["marcas"].append(f"copia_excede_limite_{nombre}")
                continue

        valores[nombre] = nuevo
        propuestos[nombre] = nuevo
        info["campos"][nombre] = {
            "previo": previo,
            "nuevo": nuevo,
            "accion": "propuesto",
            "origen": "modelo" if regla["modo"] == "modelo" else regla["origen"],
        }
        if regla["modo"] == "modelo":
            info["campos"][nombre]["modelo"] = modelo_cfg["nombre"]

    for nombre, campo in campos.items():
        if nombre in faltantes or nombre in info["campos"]:
            continue
        previo = valores[nombre]
        if not valor_vacio(previo, campo):
            info["campos"][nombre] = {"previo": previo, "nuevo": previo, "accion": "sin_cambios"}
            continue
        if campo.get("solo_si_og_image") and not og_img_val:
            info["campos"][nombre] = {
                "previo": previo, "nuevo": previo, "accion": "omitido_sin_og_image",
            }
            continue
        if "default" not in campo:
            info["campos"][nombre] = {
                "previo": previo, "nuevo": None, "accion": "requiere_revision",
                "error": "Campo vacío sin regla de generación ni valor predeterminado.",
            }
            info["marcas"].append(f"vacio_sin_regla_{nombre}")
            continue
        nuevo = campo["default"]
        if isinstance(nuevo, str):
            nuevo = nuevo.replace("{titulo}", titulo)
        if campo.get("tipo") == "checkbox":
            nuevo = bool(nuevo)
        valores[nombre] = nuevo
        propuestos[nombre] = nuevo
        info["campos"][nombre] = {
            "previo": previo, "nuevo": nuevo, "accion": "propuesto", "origen": "predeterminado",
        }

    if errores_modelo:
        info["error_modelo"] = "; ".join(errores_modelo)
        info["marcas"].append("error_modelo")

    for nombre, marca, mensaje in advertencias_campos(info["campos"], instrucciones):
        info["marcas"].append(marca)
        info["campos"][nombre].setdefault("advertencias", []).append(mensaje)

    if apply and propuestos:
        for nombre, nuevo in propuestos.items():
            campo = campos[nombre]
            el = elementos[nombre]
            if campo.get("tipo") == "checkbox":
                el.set_checked(bool(nuevo))
            else:
                el.fill(str(nuevo))
            info["campos"][nombre]["accion"] = "aplicado"
        page.locator(guardar_sel).first.click()
        page.wait_for_load_state("domcontentloaded")
    elif propuestos:
        for nombre in propuestos:
            info["campos"][nombre]["accion"] = "pendiente_dry_run"

    cambios = [
        nombre for nombre, dato in info["campos"].items()
        if dato["accion"] in ("aplicado", "pendiente_dry_run")
    ]
    info["cambios_automaticos"] = cambios
    if cambios:
        info["marcas"].append("cambio_automatico")

    if og_img_val:
        u, status, ok = og_image_check(og_img_val, url, page)
        info["og_image"] = {"url": u, "status": status, "ok": ok}
        if not ok:
            info["marcas"].append("og_image_rota")
    else:
        info["og_image"] = {"url": None, "status": None, "ok": None, "resultado": "sin_imagen"}
    return info, page


def cmd_run(args):
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    with open(CAMPOS, encoding="utf-8") as f:
        cfg = json.load(f)
    campos = cargar_campos_grabados()
    guardar_sel = cfg.get("guardar", "#edit-submit")
    instrucciones = cargar_instrucciones()
    reglas, checklist = instrucciones["campos"], instrucciones.get("checklist", [])
    modelo_cfg = {**OLLAMA_DEFAULT, **cfg.get("modelo", {})}

    if os.path.exists(args.out):
        nodes, edges, meta = load_graph(args.out)
    else:
        nodes, edges, meta = load_graph(args.source)
        meta = {**meta, "source": args.source}
    pendientes = [u for u, d in nodes.items() if u != ROOT and (args.redo or not d.get("estado") or d["estado"].startswith("dry_run"))][: args.chunk]
    if not pendientes:
        print("No quedan nodos pendientes.")
        return
    print(("APLICANDO" if args.apply else "DRY-RUN") + f": {len(pendientes)} páginas")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, **LAUNCH_OPTS)
        pages = []
        for url in pendientes:
            try:
                info, page = procesar(
                    ctx,
                    url,
                    campos,
                    reglas,
                    checklist,
                    modelo_cfg,
                    args.apply,
                    guardar_sel,
                    re.sub(r"\s*-\s*Universidad EAFIT$", "", nodes[url].get("title", "")),
                )
            except PlaywrightError as exc:
                info, page = {"marcas": ["error_playwright"], "error": str(exc)[:400]}, None
            nodes[url].update(info)
            if info.get("error_modelo"):
                nodes[url]["estado"] = "error_modelo"
            else:
                nodes[url]["estado"] = ("" if args.apply else "dry_run_") + (
                    "automatico_ok" if not info["marcas"] else "automatico_con_marcas"
                )
            nodes[url].pop("error", None)
            if page:
                pages.append(page)
            print(f"  {url}: {nodes[url]['estado']} {info['marcas']}")
            for nombre, propuesta in campos_propuestos_por_ia(info).items():
                print(
                    f"    IA/{propuesta.get('modelo', modelo_cfg['nombre'])} -> {nombre} "
                    f"[{propuesta['accion']}]: {propuesta.get('nuevo')!r}"
                )
                if propuesta.get("error"):
                    print(f"      motivo: {propuesta['error']}")
            save_graph(args.out, nodes, edges, meta)
        input("Revisa a mano imágenes, títulos, subtítulos, alt, banner y h1. Enter para cerrar y marcar el chunk... ")
        for u in pendientes:
            if args.apply:
                nodes[u]["estado"] = "revisado"
        save_graph(args.out, nodes, edges, meta)
        ctx.close()
    print(f"Guardado en {args.out}")


def cmd_reset(args):
    nodes, edges, meta = load_graph(args.out)
    reiniciados = reiniciar_estados(nodes)
    save_graph(args.out, nodes, edges, meta)
    print(f"Progreso reiniciado: {reiniciados} páginas vuelven a estar pendientes en {args.out}.")


def cmd_report(args):
    nodes, edges, meta = load_graph(args.out)
    mod = {u: d for u, d in nodes.items() if d.get("cambios_automaticos")}
    total = sum(len(d["cambios_automaticos"]) for d in mod.values())
    print(f"Páginas procesadas: {sum(1 for d in nodes.values() if d.get('campos'))}")
    print(f"Páginas con cambios automáticos: {len(mod)} | cambios (campos) en total: {total}")
    for u, d in mod.items():
        print(f"  {u}: {', '.join(d['cambios_automaticos'])}")
    print("Propuestas de IA guardadas en el grafo:")
    total_ia = 0
    for url, nodo in nodes.items():
        for nombre, campo in campos_propuestos_por_ia(nodo).items():
            total_ia += 1
            print(
                f"  {url}\n"
                f"    {nombre} [{campo.get('accion')}], "
                f"modelo {campo.get('modelo', nodo.get('modelo_ia', 'no registrado'))}: "
                f"{campo.get('nuevo')!r}"
            )
            if campo.get("error"):
                print(f"    motivo: {campo['error']}")
    if not total_ia:
        print("  No hay propuestas de IA guardadas todavía.")
    reglas = cargar_instrucciones()["campos"]
    print("Valores que requieren revisión (incluye valores existentes):")
    total_advertencias = 0
    for url, nodo in nodes.items():
        for nombre, _, mensaje in advertencias_campos(nodo.get("campos", {}), reglas):
            total_advertencias += 1
            print(f"  {url}\n    {nombre}: {mensaje}")
    if not total_advertencias:
        print("  No se detectaron valores fuera de límite o abstracts idénticos.")
    con = [u for u, d in nodes.items() if (d.get("og_image") or {}).get("url")]
    rotas = [u for u in con if not nodes[u]["og_image"]["ok"]]
    print(f"Con imagen OG: {len(con)} | rotas: {len(rotas)} | sin imagen OG: "
          f"{sum(1 for d in nodes.values() if (d.get('og_image') or {}).get('resultado') == 'sin_imagen')}")
    for u in rotas:
        print(f"  ROTA {nodes[u]['og_image']['status']} {nodes[u]['og_image']['url']} <- {u}")
    from crawler import draw
    draw(nodes, edges, args.html)
    print(f"Grafo -> {args.html} (magenta: og_image rota, naranja: cambios automáticos)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--site", default="https://www.eafit.edu.co")
    u = sub.add_parser("run")
    u.add_argument("--apply", action="store_true", help="escribir y guardar en Drupal (por defecto dry-run)")
    u.add_argument("--chunk", type=int, default=10)
    u.add_argument("--source", default="resultado.json")
    u.add_argument("--out", default="auditoria.json")
    u.add_argument("--redo", action="store_true", help="reprocesar nodos ya procesados (p. ej. tras corregir campos.json)")
    rp = sub.add_parser("report", help="resumen de cambios automáticos y og:image; regenera el grafo")
    rp.add_argument("--out", default="auditoria.json")
    rp.add_argument("--html", default="auditoria.html")
    rs = sub.add_parser("reset", help="reiniciar solo los estados de proceso, conservando la evidencia")
    rs.add_argument("--out", default="auditoria.json")
    args = ap.parse_args()
    try:
        {"record": cmd_record, "run": cmd_run, "report": cmd_report, "reset": cmd_reset}[args.cmd](args)
    except AuditProcessingError as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    sys.exit(main())
