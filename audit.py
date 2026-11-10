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


SUFIJO_LARGO, SUFIJO_CORTO = " | Universidad EAFIT", " | EAFIT"


def ajustar_page_title(campo, valor):
    """Intercambia '| Universidad EAFIT' y '| EAFIT' (ambos válidos según el Plan SEO) para entrar en el rango."""
    minimo, maximo = campo.get("min_caracteres"), campo.get("max_caracteres")
    texto = re.sub(r"(\s*[|\-–]\s*(Universidad\s+)?EAFIT)+\s*$", "", valor.strip(), flags=re.IGNORECASE)
    if texto == valor.strip():
        return valor
    for sufijo in (SUFIJO_LARGO, SUFIJO_CORTO):
        candidato = texto + sufijo
        if (minimo is None or len(candidato) >= minimo) and (maximo is None or len(candidato) <= maximo):
            return candidato
    return valor


def normalizar_keywords(valor):
    """Quita términos repetidos (sin distinguir mayúsculas/tildes) y 'EAFIT' suelto si ya hay otros con EAFIT."""
    vistos, terminos = set(), []
    for termino in (t.strip() for t in valor.split(",")):
        clave = normalizar_texto(termino)
        if termino and clave not in vistos:
            vistos.add(clave)
            terminos.append(termino)
    if len(terminos) > 2 and any("eafit" in normalizar_texto(t) and normalizar_texto(t) != "eafit" for t in terminos):
        terminos = [t for t in terminos if normalizar_texto(t) != "eafit"]
    return ", ".join(terminos)


def ajustar_sobrelongitud(campo, valor):
    if isinstance(valor, str) and campo.get("campo") == "page_title":
        return ajustar_page_title(campo, valor)
    if isinstance(valor, str) and campo.get("campo") == "keywords":
        return normalizar_keywords(valor)
    maximo = campo.get("max_caracteres")
    if (
        not isinstance(valor, str)
        or campo.get("campo") not in {"description", "abstract"}
        or maximo is None
        or len(valor) <= maximo
    ):
        return valor
    # Los modelos pequeños suelen escribir una oración de más. Se conserva el prefijo más largo que
    # termine en oración completa (o, si no hay, en cláusula) y que quede dentro del rango.
    minimo = campo.get("min_caracteres") or int(maximo * 0.6)
    oraciones = [valor[:m.end()].strip() for m in re.finditer(r"[.!?](?=\s|$)", valor)]
    clausulas = [
        valor[:m.start()].rstrip(" ,;:-") + "."
        for m in re.finditer(r"[,;]\s|\s(?:y|e|para|con)\s", valor)
        if valor[:m.start()].rstrip().split()[-1].lower() not in {"a", "al", "de", "del", "en", "la", "el", "los", "las"}
    ]
    for candidatos in (oraciones, clausulas):
        validos = [c for c in candidatos if minimo <= len(c) <= maximo]
        if validos:
            return max(validos, key=len)
    if len(valor) - maximo > 10:
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


def parece_lista_keywords(abstract, keywords, minimo=3):
    """True si el abstract contiene literalmente 3 o más frases de las palabras clave (sin contar 'Universidad EAFIT')."""
    if not isinstance(abstract, str) or not isinstance(keywords, str):
        return False
    texto = normalizar_texto(abstract)
    terminos = {
        normalizar_texto(t).strip() for t in keywords.split(",")
        if len(t.split()) >= 2 and normalizar_texto(t).strip() not in {"universidad eafit", "eafit"}
    }
    return sum(1 for t in terminos if t and t in texto) >= minimo


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


CARACTERES_POR_PALABRA = 7  # promedio en español incluyendo el espacio; los LLM cuentan palabras mejor que caracteres


def palabras_objetivo(regla):
    """Traduce el límite de caracteres a un rango de palabras que el modelo sí puede seguir."""
    minimo, maximo = regla.get("min_caracteres"), regla.get("max_caracteres")
    if maximo is None or regla.get("campo") == "keywords":
        return None
    tope = max(1, (maximo - 5) // CARACTERES_POR_PALABRA)
    if minimo is None:
        return f"máximo {tope} palabras"
    return f"entre {max(1, -(-minimo // CARACTERES_POR_PALABRA))} y {max(tope, -(-minimo // CARACTERES_POR_PALABRA))} palabras"


def instruccion_ajuste(regla, valor):
    """Instrucción concreta de cuánto recortar o alargar, en palabras."""
    minimo, maximo = regla.get("min_caracteres"), regla.get("max_caracteres")
    largo = len(valor)
    if maximo is not None and largo > maximo:
        sobra = largo - maximo + 5
        return (f"Te pasaste: tiene {largo} caracteres y el máximo es {maximo}. Elimina unas "
                f"{-(-sobra // CARACTERES_POR_PALABRA)} palabras (~{sobra} caracteres) quitando detalles secundarios.")
    if minimo is not None and largo < minimo:
        falta = minimo - largo + 3
        return (f"Quedó corto: tiene {largo} caracteres y el mínimo es {minimo}. Agrega unas "
                f"{-(-falta // CARACTERES_POR_PALABRA)} palabras (~{falta} caracteres) usando solo hechos del contexto.")
    return None


def generar_con_ollama(campos_pendientes, instrucciones, contexto, configuracion, temperatura=0.2):
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
            "Eres redactor SEO y GEO de la Universidad EAFIT (Medellín, Colombia). Completa únicamente "
            "los campos solicitados. Tienes libertad de redacción: reformula con tus propias palabras, "
            "sintetiza el texto de la página e infiere con sentido común el enfoque, el propósito y el "
            "público al que se dirige (por ejemplo, estudiantes, empresas, investigadores o donantes). "
            "Lo que no puedes inventar son hechos verificables: cifras, fechas, nombres propios, "
            "programas, premios, alianzas o datos de contacto que no aparezcan en el contexto. "
            "Las fórmulas son plantillas: rellena cada [corchete] con información de ESTA página; los temas, "
            "servicios y públicos deben salir del texto de la página, no de otras áreas de la universidad. "
            "Devuelve null solo si el contexto no permite saber de qué trata la página. "
            "No dejes campos vacíos ni uses solo siglas. Respeta la longitud indicada en palabras. "
            "Cada campo debe aportar algo distinto: no repitas la misma frase ni la misma estructura "
            "entre page_title, description, abstract y keywords."
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
             ),
             "palabras_objetivo": palabras_objetivo(instrucciones[clave])}
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
                "options": {"temperature": temperatura},
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
    """Genera campo por campo (los modelos pequeños controlan mucho mejor la longitud con una sola tarea).

    Cada campo recibe los ya generados como contexto, para que no repita frases ni estructura.
    Con "por_campo": false en la configuración del modelo se generan todos en una sola consulta.
    """
    if not configuracion.get("por_campo", True) or len(campos_pendientes) <= 1:
        return _generar_lote(campos_pendientes, instrucciones, contexto, configuracion)
    aceptados, errores = {}, {}
    for nombre in campos_pendientes:
        llenos = dict(contexto.get("campos_seleccionados_llenos", {}))
        llenos.update({k: {"etiqueta": k, "valor": v} for k, v in aceptados.items()})
        ok, err = _generar_lote([nombre], instrucciones, {**contexto, "campos_seleccionados_llenos": llenos}, configuracion)
        aceptados.update(ok)
        errores.update(err)
    return aceptados, errores


PALABRAS_OBJETIVO_REESCRITURA = 8.3  # caracteres por palabra que gemma3:4b produce de verdad (medido)


def reescribir_longitud(regla, valor, configuracion, variantes=3):
    """Pide al modelo una reescritura corta con un número de palabras calibrado; devuelve la primera válida o None.

    Una tarea pequeña ("reescribe en N palabras") la cumple mucho mejor un modelo de 4B que generar con
    todas las restricciones a la vez. Solo aplica a campos de texto con límite de caracteres.
    """
    minimo, maximo = regla.get("min_caracteres"), regla.get("max_caracteres")
    if not isinstance(valor, str) or maximo is None or regla.get("campo") == "keywords":
        return None
    objetivo = (minimo + maximo) / 2 if minimo is not None else maximo * 0.85
    palabras = max(4, round(objetivo / PALABRAS_OBJETIVO_REESCRITURA))
    rango = f"entre {minimo} y {maximo}" if minimo is not None else f"máximo {maximo}"
    prompt = (
        f"Reescribe el siguiente texto en {palabras} palabras ({rango} caracteres). "
        "Conserva qué es, la Universidad EAFIT, la ciudad y el público; elimina detalles secundarios. "
        "No agregues datos nuevos. Responde solo con el texto reescrito, sin comillas.\n\nTexto: " + valor
    )
    if regla.get("campo") == "page_title":
        prompt = prompt.replace("Responde solo", "Debe tener la forma 'Nombre: tema | EAFIT'. Responde solo")
    for i in range(variantes):
        try:
            respuesta = requests.post(
                configuracion.get("endpoint", OLLAMA_DEFAULT["endpoint"]),
                json={
                    "model": configuracion.get("nombre", OLLAMA_DEFAULT["nombre"]),
                    "prompt": prompt, "stream": False,
                    "options": {"temperature": 0.3 + 0.2 * i},
                },
                timeout=configuracion.get("timeout", OLLAMA_DEFAULT["timeout"]),
            )
            respuesta.raise_for_status()
            texto = str(respuesta.json().get("response", "")).strip().strip('"“”')
        except (requests.RequestException, ValueError):
            return None
        if regla.get("campo") == "page_title" and not re.search(r"\|\s*(Universidad\s+)?EAFIT\s*$", texto, re.I):
            continue
        try:
            return validar_valor_generado(regla, ajustar_sobrelongitud(regla, texto))
        except ValueError:
            continue
    return None


def _generar_lote(campos_pendientes, instrucciones, contexto, configuracion):
    pendientes = list(campos_pendientes)
    aceptados = {}
    errores = {}
    contexto_actual = contexto
    intentos = max(1, int(configuracion.get("intentos", 5)))
    for intento in range(intentos):
        # Subir un poco la temperatura en los reintentos evita que el modelo repita el mismo texto inválido.
        temperatura = min(0.8, float(configuracion.get("temperatura", 0.4)) + 0.1 * intento)
        salidas = generar_con_ollama(pendientes, instrucciones, contexto_actual, configuracion, temperatura)
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
                reescrito = reescribir_longitud(instrucciones[nombre], valor, configuracion)
                if reescrito:
                    aceptados[nombre] = reescrito
                    errores.pop(nombre, None)
                    continue
                if intento < intentos - 1:
                    corregir.append(nombre)
                    ajuste = instruccion_ajuste(instrucciones[nombre], valor) if isinstance(valor, str) else None
                    motivos[nombre] = {
                        "valor_rechazado": valor,
                        "motivo": str(exc),
                        "instruccion": ajuste or (
                            "Reescribe este valor para que cumpla la instrucción del campo; no agregues hechos."
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
                if intento < intentos - 1:
                    corregir.append(nombre)
                    motivos[nombre] = {
                        "valor_rechazado": valor,
                        "motivo": motivo,
                        "instruccion": "Reescribe el campo con un texto diferente, sin inventar datos y respetando sus límites.",
                    }
                else:
                    errores[nombre] = motivo
        keywords = aceptados.get("keywords", existentes.get("keywords", {}).get("valor"))
        if "abstract" in aceptados and parece_lista_keywords(aceptados["abstract"], keywords):
            valor = aceptados.pop("abstract")
            motivo = "parece la lista de palabras clave; debe ser una síntesis redactada"
            if intento < intentos - 1:
                corregir.append("abstract")
                motivos["abstract"] = {
                    "valor_rechazado": valor,
                    "motivo": motivo,
                    "instruccion": (
                        "Reescribe el abstract como una síntesis con la fórmula indicada: qué es, qué "
                        "desarrolla y para quién. No enumeres las palabras clave."
                    ),
                }
            else:
                errores["abstract"] = motivo
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
      const subtitulos = Array.from(main.querySelectorAll('h2, h3')).map(limpio).filter(Boolean);
      const parrafos = Array.from(main.querySelectorAll('p, li')).map(limpio).filter((t) => t.length > 40);
      return {
        h1_count: h1.length,
        h1: h1[0] || '',
        primer_parrafo: intro,
        subtitulos: [...new Set(subtitulos)].slice(0, 12),
        texto_principal: [...new Set(parrafos)].join(' '),
        imagenes_sin_alt: imagenes.filter((img) => !img.hasAttribute('alt') || !img.getAttribute('alt').trim()).length
      };
    }""")
    datos["h1"] = datos["h1"][:1000]
    datos["primer_parrafo"] = datos["primer_parrafo"][:3000]
    datos["subtitulos"] = [s[:150] for s in datos.get("subtitulos", [])]
    datos["texto_principal"] = datos.get("texto_principal", "")[:1800]
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


def motivo_valor_existente_invalido(regla, valor, valores):
    """Motivo por el que un valor ya escrito en Drupal incumple las reglas del Plan SEO, o None si es válido."""
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        validar_valor_generado(regla, valor)
    except ValueError as exc:
        return str(exc)
    nombre = regla["campo"]
    if nombre == "abstract":
        for otro in ("description", "keywords"):
            if textos_iguales(valor, valores.get(otro)):
                return f"es igual a {otro}"
    return None


def procesar(ctx, url, campos, instrucciones, checklist, modelo_cfg, apply, guardar_sel, titulo="", corregir=False):
    info = {"campos": {}, "marcas": [], "revisado_en": datetime.now().isoformat(timespec="seconds")}
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    contenido = extraer_contexto_pagina(page)
    info["revision_contenido"] = {
        **{k: v for k, v in contenido.items() if k != "texto_principal"},
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
            "subtitulos": contenido.get("subtitulos", []),
            "texto_principal": contenido.get("texto_principal", ""),
        },
    }

    invalidos = {}
    for regla in instrucciones:
        nombre = regla["campo"]
        if regla["modo"] == "modelo" and nombre in elementos:
            motivo = motivo_valor_existente_invalido(regla, valores[nombre], valores)
            if motivo:
                invalidos[nombre] = motivo
                if not corregir:
                    info["marcas"].append(f"existente_invalido_{nombre}")
    a_regenerar = set(invalidos) if corregir else set()

    pendientes_modelo = [
        regla["campo"] for regla in instrucciones
        if regla["modo"] == "modelo"
        and regla["campo"] in elementos
        and (valor_vacio(valores[regla["campo"]], campos[regla["campo"]]) or regla["campo"] in a_regenerar)
    ]
    if a_regenerar:
        contexto["valores_existentes_rechazados"] = {
            nombre: {"valor": valores[nombre], "motivo": invalidos[nombre]} for nombre in a_regenerar
        }
        contexto["campos_seleccionados_llenos"] = {
            k: v for k, v in contexto["campos_seleccionados_llenos"].items() if k not in a_regenerar
        }
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
        if not valor_vacio(previo, campo) and nombre not in a_regenerar:
            info["campos"][nombre] = {"previo": previo, "nuevo": previo, "accion": "sin_cambios"}
            if nombre in invalidos:
                info["campos"][nombre]["advertencias"] = [f"valor existente inválido: {invalidos[nombre]}"]
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
            info["campos"][nombre]["accion"] = "llenado_sin_guardar"
        # Quitar el foco del último campo (suele ser og_locale) para que Inicio/Fin desplacen la página.
        page.evaluate("document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0)")
        # No se guarda aquí: el usuario revisa la pestaña abierta y guarda a mano en Drupal.
        info["pendiente_guardar"] = True
    elif propuestos:
        for nombre in propuestos:
            info["campos"][nombre]["accion"] = "pendiente_dry_run"

    cambios = [
        nombre for nombre, dato in info["campos"].items()
        if dato["accion"] in ("aplicado", "pendiente_dry_run", "llenado_sin_guardar")
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


def resumen_pagina(url, nodo, modelo_nombre, apply):
    """Texto de consola: por cada página, qué campos se llenaron, con qué contenido y de dónde salió."""
    def mostrar(valor):
        if valor is None or valor == "":
            return "(vacío)"
        if isinstance(valor, bool):
            return "marcado" if valor else "sin marcar"
        return f"\"{valor}\" ({len(str(valor))} car.)"

    def origen(dato):
        if dato.get("origen") == "modelo":
            return f"IA {dato.get('modelo', modelo_nombre)}"
        if dato.get("origen") == "predeterminado":
            return "valor predeterminado"
        if dato.get("origen"):
            return f"copia de {dato['origen']}"
        return "?"

    verbo = "LLENADO SIN GUARDAR" if apply else "SE LLENARÍA"
    lineas = ["", "=" * 78, f"{url}", f"  estado: {nodo.get('estado')}"]
    if nodo.get("error"):
        lineas.append(f"  ERROR: {nodo['error']}")
    campos = nodo.get("campos", {})
    llenados = {n: d for n, d in campos.items() if d.get("accion") in ("aplicado", "pendiente_dry_run", "propuesto", "llenado_sin_guardar")}
    revisar = {n: d for n, d in campos.items() if d.get("accion") in ("error_modelo", "requiere_revision")}
    no_encontrados = [n for n, d in campos.items() if d.get("accion") == "campo_no_encontrado"]
    sin_cambios = {n: d for n, d in campos.items() if d.get("accion") in ("sin_cambios", "omitido_sin_og_image")}

    lineas.append(f"  {verbo} ({len(llenados)}):" if llenados else f"  {verbo}: ningún campo")
    for nombre, dato in llenados.items():
        lineas.append(f"    + {nombre}  [{origen(dato)}]")
        lineas.append(f"        antes: {mostrar(dato.get('previo'))}")
        lineas.append(f"        nuevo: {mostrar(dato.get('nuevo'))}")
    if revisar:
        lineas.append(f"  REQUIERE REVISIÓN ({len(revisar)}):")
        for nombre, dato in revisar.items():
            lineas.append(f"    ! {nombre}: {dato.get('error') or dato.get('accion')}")
            if dato.get("nuevo") not in (None, ""):
                lineas.append(f"        rechazado: {mostrar(dato.get('nuevo'))}")
    if no_encontrados:
        lineas.append(f"  NO ENCONTRADOS EN EL FORMULARIO: {', '.join(no_encontrados)}")
    if sin_cambios:
        lineas.append(f"  SIN CAMBIOS: {', '.join(sin_cambios)}")
        for nombre, dato in sin_cambios.items():
            for advertencia in dato.get("advertencias", []):
                lineas.append(f"    ~ {nombre}: {advertencia}")
    contenido = nodo.get("revision_contenido") or {}
    revision = [m for m in nodo.get("marcas", []) if m in {
        "h1_ausente", "h1_no_unico", "primer_parrafo_ausente", "imagenes_sin_alt", "sin_acceso",
        "edicion_no_abrio", "og_image_rota", "error_playwright",
    }]
    if revision:
        detalle = f" ({contenido['imagenes_sin_alt']} imágenes sin alt)" if "imagenes_sin_alt" in revision else ""
        lineas.append(f"  REVISIÓN MANUAL: {', '.join(revision)}{detalle}")
    return "\n".join(lineas)


def ultimo_chunk(nodes, meta, tamano=10):
    """Páginas del último chunk ejecutado. Si el archivo es anterior a este registro, se toman las
    `tamano` páginas revisadas más recientemente."""
    guardado = [u for u in meta.get("ultimo_chunk", []) if u in nodes]
    if guardado:
        return guardado
    fechas = {u: d["revisado_en"] for u, d in nodes.items() if u != ROOT and d.get("revisado_en")}
    return sorted(fechas, key=fechas.get)[-tamano:]


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
    if args.ultimo:
        pendientes = ultimo_chunk(nodes, meta, args.chunk)
        if not pendientes:
            print("No hay registro de un chunk anterior en", args.out)
            return
        print("Rehaciendo el último chunk visitado:")
        for u in pendientes:
            print(f"  - {u}")
    else:
        pendientes = [u for u, d in nodes.items() if u != ROOT and (args.redo or not d.get("estado") or d["estado"].startswith(("dry_run", "sin_guardar", "error_guardar")))][: args.chunk]
    if not pendientes:
        print("No quedan nodos pendientes.")
        return
    meta["ultimo_chunk"] = pendientes
    print(("APLICANDO" if args.apply else "DRY-RUN") + f": {len(pendientes)} páginas")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, **LAUNCH_OPTS)
        pages = []
        por_guardar = {}
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
                    corregir=args.corregir,
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
            if nodes[url].pop("pendiente_guardar", False) and page:
                # Hasta que se guarde, la página sigue pendiente: si el proceso se corta, se vuelve a visitar.
                por_guardar[url] = (page, nodes[url]["estado"])
                nodes[url]["estado"] = "sin_guardar"
            nodes[url].pop("error", None)
            if page:
                pages.append(page)
            print(resumen_pagina(url, nodes[url], modelo_cfg["nombre"], args.apply))
            save_graph(args.out, nodes, edges, meta)
        if por_guardar:
            # El script nunca pulsa Guardar: el usuario revisa y guarda cada pestaña a mano en Drupal.
            print(f"\n{len(por_guardar)} páginas quedaron llenadas SIN GUARDAR en el navegador:")
            for url in por_guardar:
                print(f"  - {url}")
            print("Revísalas, corrige lo que necesites y pulsa Guardar en Drupal en cada una.")
        input("Revisa a mano imágenes, títulos, subtítulos, alt, banner y h1. "
              "Cuando termines (y hayas guardado), Enter para cerrar y marcar el chunk... ")
        for u in pendientes:
            if args.apply:
                if u in por_guardar:
                    for dato in nodes[u].get("campos", {}).values():
                        if dato.get("accion") == "llenado_sin_guardar":
                            dato["accion"] = "llenado_guardado_manual"
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
    print(f"Grafo -> {args.html} (magenta: og_image rota, verde: contenido IA, azul: cambios automáticos)")


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
    u.add_argument("--ultimo", action="store_true", help="volver a procesar exactamente las páginas del último chunk ejecutado")
    u.add_argument(
        "--corregir", action="store_true",
        help="regenerar con IA los campos ya llenos que incumplen las reglas (longitud, solo siglas, abstract copiado)",
    )
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
