#!/usr/bin/env python3
"""Crawlea un sitio (p. ej. Drupal) y genera un grafo interactivo de páginas y enlaces.

Se crea un nodo raíz virtual (no es una página) enlazado a las URLs que le indiques.

Uso:
    python crawler.py                      # usa SEED_URLS y MAX_DEPTH definidos en este archivo
    python crawler.py urls.txt --depth 2   # o un archivo / --url (se suman a SEED_URLS)
    python crawler.py -u https://uni.edu/a -u https://uni.edu/b --depth 3
"""
import argparse
import re
import sys
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse

import requests
from bs4 import BeautifulSoup
from pyvis.network import Network

from graph_store import ROOT, save_graph

# ====== CONFIGURACIÓN: pon aquí tus URLs iniciales ======
SEED_URLS = [
  "https://www.eafit.edu.co/ciencia-tecnologia-innovacion",
  "https://www.eafit.edu.co/sistema-ciencia-tecnologia-innovacion/investigacion",
  "https://www.eafit.edu.co/sistema-ciencia-tecnologia-innovacion/innovacion",
  "https://www.eafit.edu.co/sistema-ciencia-tecnologia-innovacion/cultura-ciencia-tecnologia-innovacion",
  "https://www.eafit.edu.co/sistema-ciencia-tecnologia-innovacion/formacionctei",
  "https://www.eafit.edu.co/sistema-ciencia-tecnologia-innovacion/universidad-ninos",
  "https://www.eafit.edu.co/centros-estudio-incidencia",
  "https://www.eafit.edu.co/sistema-ciencia-tecnologia-innovacion/innovacion-desarrollo-tecnologico/proyectos-ctei",
  "https://www.eafit.edu.co/centro-cultural-biblioteca-luis-echavarria-villegas"
]

# SEED_URLS = [
#     "https://www.eafit.edu.co/pregrados",
#     "https://www.eafit.edu.co/posgrados",
#     "https://www.eafit.edu.co/otros-programas",
#     "https://www.eafit.edu.co/idiomas",
#     "https://www.eafit.edu.co/sistema-ciencia-tecnologia-innovacion/universidad-ninos",
#     "https://www.eafit.edu.co/otros-programas/eventos",
#     "https://www.eafit.edu.co/becas-y-financiacion",
#     "https://www.eafit.edu.co/registro-academico"
# ]
MAX_DEPTH = 2  # profundidad máxima (las URLs iniciales tienen profundidad 0)
# =========================================================

SKIP_EXT = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|png|jpe?g|gif|svg|webp|mp[34]|avi|css|js|ico|xml|json)$",
    re.I,
)
# Rutas típicas de Drupal que no aportan contenido
DEFAULT_EXCLUDE = [r"^/user", r"^/admin", r"^/node/\d+/(edit|delete)", r"^/search", r"^/sites/default/files"]


def normalize(url):
    url, _ = urldefrag(url)
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", "", ""))  # sin query string


def valid_link(base, href, hosts, exclude_re):
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    link = urljoin(base, href)
    p = urlparse(link)
    if p.scheme not in ("http", "https") or p.netloc.lower() not in hosts:
        return None
    link = normalize(link)
    path = urlparse(link).path
    if SKIP_EXT.search(path) or any(rx.search(path) for rx in exclude_re):
        return None
    return link


def crawl(seeds, max_depth, max_pages, delay, exclude, timeout):
    seeds = list(dict.fromkeys(normalize(s) for s in seeds))
    hosts = {urlparse(s).netloc for s in seeds}
    exclude_re = [re.compile(e) for e in exclude]
    session = requests.Session()
    session.headers["User-Agent"] = "site-graph-crawler/1.0"

    nodes = {ROOT: {"title": "Raíz (nodo virtual)", "depth": -1, "status": None}}
    edges = {(ROOT, s) for s in seeds}
    queue = deque((s, 0) for s in seeds)
    seen = set(seeds)

    while queue and len(nodes) - 1 < max_pages:
        url, depth = queue.popleft()
        try:
            r = session.get(url, timeout=timeout)
        except requests.RequestException as e:
            nodes[url] = {"title": f"ERROR: {e.__class__.__name__}", "depth": depth, "status": 0}
            continue
        nodes[url] = {"title": url, "depth": depth, "status": r.status_code}
        print(f"[{depth}] {r.status_code} {url}", file=sys.stderr)
        if "html" not in r.headers.get("Content-Type", "") or r.status_code >= 400:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        if soup.title and soup.title.string:
            nodes[url]["title"] = soup.title.string.strip()
        if depth >= max_depth:
            continue
        for a in soup.find_all("a", href=True):
            link = valid_link(url, a["href"], hosts, exclude_re)
            if not link or link == url:
                continue
            edges.add((url, link))
            if link not in seen:
                seen.add(link)
                queue.append((link, depth + 1))
        time.sleep(delay)

    # solo conserva aristas hacia nodos visitados
    edges = {(a, b) for a, b in edges if b in nodes}
    return nodes, edges


def draw(nodes, edges, output):
    if ROOT not in nodes:  # grafo separado: raíz virtual hacia los nodos sin enlaces entrantes
        nodes = {ROOT: {"title": "Raíz (nodo virtual)", "depth": -1, "status": None}, **nodes}
        targets = {b for _, b in edges}
        edges = set(edges) | {(ROOT, n) for n in nodes if n != ROOT and n not in targets}
    palette = ["#e63946", "#f4a261", "#e9c46a", "#2a9d8f", "#457b9d", "#8d99ae", "#6a4c93"]
    net = Network(height="95vh", width="100%", directed=True, bgcolor="#ffffff")
    indeg = {}
    for _, b in edges:
        indeg[b] = indeg.get(b, 0) + 1
    for url, d in nodes.items():
        if url == ROOT:
            net.add_node(url, label="RAÍZ", title=d["title"], color="#111111", shape="star", size=30)
            continue
        color = "#000000" if d["status"] >= 400 or d["status"] == 0 else palette[min(d["depth"], len(palette) - 1)]
        marcas = d.get("marcas") or []
        extra = f"<br>marcas: {', '.join(marcas)}" if marcas else ""
        size = 10 + 3 * min(indeg.get(url, 0), 15)
        if "og_image_rota" in marcas:
            color, size = "#ff00ff", size + 10
        elif d.get("cambios_automaticos"):
            color = "#ff8c00"
        net.add_node(
            url,
            label=urlparse(url).path or "/",
            title=f"{d['title']}<br>{url}<br>HTTP {d['status']} · profundidad {d['depth']}{extra}",
            color=color,
            size=size,
        )
    for a, b in edges:
        net.add_edge(a, b)
    net.barnes_hut(gravity=-8000, spring_length=150)
    net.write_html(output, notebook=False, open_browser=False)


def read_urls(path):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls_file", nargs="?", help="archivo de texto con una URL por línea (# para comentarios)")
    ap.add_argument("-u", "--url", action="append", default=[], help="URL inicial (repetible)")
    ap.add_argument("-d", "--depth", type=int, default=MAX_DEPTH,
                    help=f"profundidad máxima; las URLs iniciales tienen profundidad 0 (default {MAX_DEPTH})")
    ap.add_argument("-m", "--max-pages", type=int, default=500, help="tope de páginas (default 500)")
    ap.add_argument("--delay", type=float, default=0.3, help="segundos entre peticiones")
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--exclude", action="append", default=None,
                    help="regex de rutas a excluir (repetible); reemplaza los valores por defecto de Drupal")
    ap.add_argument("-o", "--output", default="grafo.html")
    ap.add_argument("--save", default="grafo.json", help="JSON donde se guarda el grafo (default grafo.json)")
    args = ap.parse_args()

    seeds = SEED_URLS + args.url + (read_urls(args.urls_file) if args.urls_file else [])
    if not seeds:
        ap.error("agrega URLs a SEED_URLS en el código, o usa un archivo / --url")

    nodes, edges = crawl(seeds, args.depth, args.max_pages, args.delay,
                         args.exclude if args.exclude is not None else DEFAULT_EXCLUDE, args.timeout)
    save_graph(args.save, nodes, edges, {"seeds": seeds, "max_depth": args.depth})
    draw(nodes, edges, args.output)
    print(f"{len(nodes) - 1} páginas, {len(edges)} enlaces -> {args.save}, {args.output}")


if __name__ == "__main__":
    main()
