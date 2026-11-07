#!/usr/bin/env python3
"""Almacenamiento de grafos en JSON y operaciones para separar nodos a otro grafo.

Formato: {"meta": {...}, "nodes": {url: {...}}, "edges": [[origen, destino], ...]}
El nodo raíz virtual se llama "__ROOT__".

Uso:
    # mover a un grafo aparte las páginas que no se pueden visitar (HTTP >= 400 o error)
    python graph_store.py split grafo.json --out no_visitables.json --broken

    # mover todos los nodos que aparezcan en otro(s) grafo guardado(s)
    python graph_store.py split principal.json --out bloqueados.json --from-graph otro.json --prune

    # mover por URL exacta o regex (repetibles) y podar lo que quede inalcanzable
    python graph_store.py split grafo.json --out bloqueados.json \\
        --url https://www.uni.edu/intranet --regex '/estudiantes/.*' --prune

    # dibujar cualquier grafo guardado
    python graph_store.py draw no_visitables.json -o no_visitables.html
"""
import argparse
import json
import re
from collections import deque

ROOT = "__ROOT__"


def save_graph(path, nodes, edges, meta=None):
    data = {"meta": meta or {}, "nodes": nodes, "edges": sorted([a, b] for a, b in edges)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_graph(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["nodes"], {tuple(e) for e in data["edges"]}, data.get("meta", {})


def reachable_from_root(nodes, edges):
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    seen, queue = {ROOT}, deque([ROOT])
    while queue:
        for v in adj.get(queue.popleft(), []):
            if v not in seen:
                seen.add(v)
                queue.append(v)
    return seen & set(nodes)


def split_graph(nodes, edges, to_move, prune=False):
    """Separa `to_move` del grafo principal.

    Devuelve (principal, separado), cada uno como (nodes, edges). El separado conserva
    las aristas entre sus nodos; las aristas que cruzan entre ambos se descartan.
    Con prune=True también salen del principal (y pasan al separado) los nodos que
    quedan inalcanzables desde la raíz.
    """
    to_move = set(to_move) - {ROOT}
    if prune:
        main_nodes = set(nodes) - to_move
        main_edges = {(a, b) for a, b in edges if a in main_nodes and b in main_nodes}
        alive = reachable_from_root({n: nodes[n] for n in main_nodes}, main_edges)
        to_move |= main_nodes - alive
    keep = set(nodes) - to_move
    main = ({n: nodes[n] for n in keep}, {(a, b) for a, b in edges if a in keep and b in keep})
    moved = ({n: nodes[n] for n in to_move}, {(a, b) for a, b in edges if a in to_move and b in to_move})
    return main, moved


def select_nodes(nodes, urls=(), regexes=(), broken=False):
    from crawler import normalize  # import tardío: crawler importa este módulo

    wanted = {normalize(u) for u in urls}
    rx = [re.compile(r) for r in regexes]
    out = set()
    for url, d in nodes.items():
        if url == ROOT:
            continue
        status = d.get("status")
        if (url in wanted or any(r.search(url) for r in rx)
                or (broken and (status == 0 or (status or 0) >= 400))):
            out.add(url)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("split", help="mover nodos a otro grafo y quitarlos del principal")
    sp.add_argument("graph", help="grafo principal (JSON); se sobrescribe sin los nodos movidos")
    sp.add_argument("--out", required=True, help="JSON del grafo separado (si existe, se le agregan los nodos)")
    sp.add_argument("--url", action="append", default=[], help="URL exacta a mover (repetible)")
    sp.add_argument("--regex", action="append", default=[], help="regex sobre la URL completa (repetible)")
    sp.add_argument("--from-graph", action="append", default=[], metavar="JSON",
                    help="grafo JSON cuyos nodos se mueven fuera del principal (repetible)")
    sp.add_argument("--broken", action="store_true", help="mover las páginas con error o HTTP >= 400")
    sp.add_argument("--prune", action="store_true", help="mover también lo que quede inalcanzable desde la raíz")

    dp = sub.add_parser("draw", help="dibujar un grafo guardado")
    dp.add_argument("graph")
    dp.add_argument("-o", "--output", default="grafo.html")

    args = ap.parse_args()
    nodes, edges, meta = load_graph(args.graph)

    if args.cmd == "draw":
        from crawler import draw
        draw(nodes, edges, args.output)
        print(f"{len(nodes) - (ROOT in nodes)} páginas, {len(edges)} enlaces -> {args.output}")
        return

    chosen = select_nodes(nodes, args.url, args.regex, args.broken)
    for other in args.from_graph:
        other_nodes, _, _ = load_graph(other)
        chosen |= (set(other_nodes) & set(nodes)) - {ROOT}
    if not chosen and not args.prune:
        raise SystemExit("Ningún nodo coincide; no se cambió nada.")
    (m_nodes, m_edges), (s_nodes, s_edges) = split_graph(nodes, edges, chosen, args.prune)

    try:  # acumula sobre un grafo separado previo
        old_nodes, old_edges, _ = load_graph(args.out)
        s_nodes, s_edges = {**old_nodes, **s_nodes}, old_edges | s_edges
    except FileNotFoundError:
        pass
    s_nodes.pop(ROOT, None)  # el separado no necesita raíz; se agrega al dibujar si hace falta

    save_graph(args.out, s_nodes, s_edges, {**meta, "source": args.graph})
    save_graph(args.graph, m_nodes, m_edges, meta)
    print(f"Movidos {len(s_nodes)} nodos a {args.out}; principal: {len(m_nodes) - 1} páginas, {len(m_edges)} enlaces")


if __name__ == "__main__":
    main()
