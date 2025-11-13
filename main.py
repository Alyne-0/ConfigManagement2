"""
Вариант №13. Инструмент визуализации графа зависимостей для менеджера пакетов.

Этап 1: минимальный CLI с конфигурацией (печать параметров по --conf-dump).
Этап 2: сбор прямых зависимостей для пакета (Cargo) по API crates.io.
Этап 3: построение полного графа (DFS, рекурсия), игнор по подстроке, обработка циклов,
        режим тестового репозитория (локальный файл с описанием графа БОЛЬШИМИ ЛАТИНСКИМИ БУКВАМИ).
Этап 4: дополнительные операции — обратные зависимости (--reverse) для тестового репозитория.
Этап 5: визуализация — ASCII-дерево (по умолчанию) или вывод Graphviz DOT (--output dot).

"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict, deque
from typing import Dict, List, Set, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

# ------------------------- ЭТАП 1: CLI + валидации -------------------------

PKG_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")  # имя crate на crates.io
UPPER_RE = re.compile(r"^[A-Z]+$")                   # имя узла в тестовом репо

def is_url(s: str) -> bool:
    try:
        p = urlparse(s)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def fail(msg: str) -> "NoReturn":
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Dependency graph tool (Stages 1–5)"
    )
    p.add_argument("--package", help="имя анализируемого пакета (crate) для режима crates.io")
    p.add_argument("--repo", default="", help="URL (crates.io режим) ИЛИ путь к тестовому репозиторию (файл)")
    p.add_argument("--test-mode", action="store_true", help="режим тестового репозитория (локальный файл)")
    p.add_argument("--output", default="ascii-tree", choices=["ascii-tree", "dot"],
                   help="формат вывода зависимостей")
    p.add_argument("--filter", default="", help="подстрока, пакеты с которой в имени нужно игнорировать")
    p.add_argument("--conf-dump", action="store_true",
                   help="(Этап 1) вывести параметры ключ=значение и выйти")
    p.add_argument("--reverse", action="store_true",
                   help="(Этап 4) вывести ОБРАТНЫЕ зависимости (только в --test-mode)")
    return p.parse_args()

def validate_stage1(ns: argparse.Namespace) -> Dict[str, str]:
    """Валидации согласно Этапу 1."""
    if ns.test_mode:
        if not ns.repo:
            fail("in --test-mode you must provide local path via --repo")
        if is_url(ns.repo):
            fail("--repo must be a local file path in --test-mode")
        if not os.path.exists(ns.repo):
            fail(f"test repository file does not exist: {ns.repo}")
    else:
        # crates.io 
        if not ns.package:
            fail("--package is required in crates.io mode")
        if not PKG_RE.match(ns.package):
            fail("invalid crate name: use lowercase letters, digits, '-' or '_'")
        if ns.repo and not is_url(ns.repo):
            fail("--repo should be an HTTP/HTTPS URL (or omit) in crates.io mode")

    if ns.output not in ("ascii-tree", "dot"):
        fail("unsupported --output (allowed: ascii-tree, dot)")

    cfg = {
        "package": ns.package or "",
        "repo": ns.repo,
        "test_mode": "true" if ns.test_mode else "false",
        "output": ns.output,
        "filter": ns.filter or "",
        "reverse": "true" if ns.reverse else "false",
    }
    return cfg

# ------------------- ЭТАП 2:-------------------

CRATES_API = "https://crates.io/api/v1/crates"

def http_get_json(url: str) -> dict:
    try:
        print("[debug] GET", url)
        req = Request(url, headers={"User-Agent": "edu-deps-tool/1.0"})
        with urlopen(req, timeout=20) as r:
            data = r.read()
        return json.loads(data.decode("utf-8"))
    except (HTTPError, URLError) as e:
        fail(f"cannot GET {url}: {e}")
    except json.JSONDecodeError as e:
        fail(f"invalid JSON from {url}: {e}")

def crates_latest_version(crate: str) -> str:
    meta = http_get_json(f"{CRATES_API}/{crate}")
    newest = meta.get("crate", {}).get("newest_version")
    if not newest:
        versions = meta.get("versions") or []
        if not versions:
            fail(f"no versions for crate '{crate}'")
        newest = versions[0].get("num")
    return newest

def crates_direct_deps(crate: str, version: str) -> List[Tuple[str, str, str]]:
    url = f"{CRATES_API}/{crate}/{version}/dependencies"
    j = http_get_json(url)
    deps = []
    for d in j.get("dependencies", []):
        if d.get("optional"):
            continue
        kind = d.get("kind") or "normal"
        if kind != "normal":
            continue
        crate_id = d.get("crate_id")
        req = d.get("req") or ""
        deps.append((crate_id, req, kind))
    return deps

# ---------------------- ЭТАП 3: полный граф ----------------------

Graph = Dict[str, Set[str]]

def build_graph_cratesio(root: str, flt: str) -> Graph:
    graph: Graph = defaultdict(set)
    visited: Set[str] = set()

    flt_low = (flt or "").lower()

    def dfs(node: str):
        if flt_low and flt_low in node.lower():
            return
        if node in visited:
            return
        visited.add(node)
        try:
            ver = crates_latest_version(node)
            deps = crates_direct_deps(node, ver)
        except Exception:
            return
        for dep, _req, _kind in deps:
            if flt_low and flt_low in dep.lower():
                continue
            graph[node].add(dep)
            if dep in visited:
                continue
            dfs(dep)
    dfs(root)
    return graph

# --- тестовый репозиторий ---

def load_test_graph(path: str) -> Graph:
    """
    Формат файла: по одной зависимости на строку, двоеточие и пробелы:
      A: B C
      B: C
      C:
    """
    g: Graph = defaultdict(set)
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if ":" not in s:
                fail(f"bad test graph line (expected 'X: Y Z'): {s}")
            left, right = s.split(":", 1)
            left = left.strip()
            if not UPPER_RE.fullmatch(left):
                fail(f"invalid node name in test graph (must be A..Z+): {left}")
            deps = [t for t in right.strip().split() if t]
            for d in deps:
                if not UPPER_RE.fullmatch(d):
                    fail(f"invalid dep name in test graph (must be A..Z+): {d}")
                g[left].add(d)
            g.setdefault(left, set())  # гарантируем наличие узла
    return g

def dfs_prune_by_filter(g: Graph, start: str, flt: str) -> Graph:
    """Обходит граф DFS с учетом игнора по подстроке; возвращает подграф достижимых узлов."""
    flt_low = (flt or "").lower()
    out: Graph = defaultdict(set)
    visited: Set[str] = set()

    def dfs(u: str):
        if u in visited:
            return
        if flt_low and flt_low in u.lower():
            return
        visited.add(u)
        for v in g.get(u, ()):
            if flt_low and flt_low in v.lower():
                continue
            out[u].add(v)
            dfs(v)

    dfs(start)
    return out

def reverse_graph(g: Graph) -> Graph:
    r: Graph = defaultdict(set)
    for u, nbrs in g.items():
        for v in nbrs:
            r[v].add(u)
        r.setdefault(u, set())
    return r

def reachable(g: Graph, start: str) -> Set[str]:
    """Все узлы, из которых достижим start в rG (или в G — в зависимости от вызова)."""
    seen: Set[str] = set()
    dq = deque([start])
    while dq:
        u = dq.popleft()
        for v in g.get(u, ()):
            if v not in seen:
                seen.add(v)
                dq.append(v)
    return seen

# ---------------------- ЭТАП 5: визуализации (ASCII / DOT) ----------------------

def print_ascii_tree(g: Graph, root: str) -> None:
    visited: Set[str] = set()

    def rec(u: str, prefix: str, is_last: bool):
        connector = "└─ " if is_last else "├─ "
        print(prefix + connector + u)
        visited.add(u)
        children = sorted(list(g.get(u, [])))
        if not children:
            return
        new_pref = prefix + ("   " if is_last else "│  ")
        for i, v in enumerate(children):
            if v in visited:
                print(new_pref + ("└─ " if i == len(children) - 1 else "├─ ") + v + " (cycle/seen)")
            else:
                rec(v, new_pref, i == len(children) - 1)

    if root not in g:
        print(root)
        return
    rec(root, "", True)

def to_dot(g: Graph) -> str:
    lines = ["digraph deps {"]
    lines.append('  rankdir=LR; node [shape=box, fontsize=10];')
    nodes = set(g.keys())
    for u, nbrs in g.items():
        nodes.update(nbrs)
        for v in nbrs:
            lines.append(f'  "{u}" -> "{v}";')
    for n in nodes:
        if n not in g and not any(n in s for s in g.values()):
            lines.append(f'  "{n}";')
    lines.append("}")
    return "\n".join(lines)

# ----------------------------------- main -----------------------------------

def main() -> int:
    ns = parse_args()
    cfg = validate_stage1(ns)

    # ЭТАП 1:
    if ns.conf_dump:
        for k, v in cfg.items():
            print(f"{k}={v}")
        return 0

    if ns.test_mode:
        # ЭТАП 3:
        full_g = load_test_graph(ns.repo)
        if not ns.package or not UPPER_RE.fullmatch(ns.package):
            fail("--package must be provided as UPPER-CASE name in --test-mode (e.g., A)")
        sub = dfs_prune_by_filter(full_g, ns.package, ns.filter)

        if ns.reverse:
            # ЭТАП 4:
            r = reverse_graph(full_g)
            depends_on_me = reachable(r, ns.package)
            g_rev: Graph = defaultdict(set)
            for v in depends_on_me:
                for u in r.get(v, ()):
                    if u in depends_on_me or u == ns.package:
                        g_rev[v].add(u)
            # Визуализация
            if ns.output == "ascii-tree":
                print(f"Reverse dependencies of {ns.package}:")
                print_ascii_tree(r, ns.package)
            else:
                print(to_dot(g_rev))
            return 0

        # Обычный вывод графа
        if ns.output == "ascii-tree":
            print_ascii_tree(sub, ns.package)
        else:
            print(to_dot(sub))
        return 0

    # Режим crates.io (ЭТАП 2 + ЭТАП 3 + ЭТАП 5)
    root = ns.package
    # строим транзитивный граф с фильтром, DFS, обработкой циклов
    g = build_graph_cratesio(root, ns.filter)

    if ns.output == "ascii-tree":
        print_ascii_tree(g, root)
    else:
        print(to_dot(g))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
