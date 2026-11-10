"""Human-readable CLI/MCP renderers; inputs are the public result dictionaries."""
from __future__ import annotations

from collections import defaultdict


def render_search(res: dict) -> str:
    out = [f'Search: "{res["query"]}"  ({len(res["results"])} results)']
    if not res["results"]:
        out.append("No matches.")
        if res.get('suggestions'):
            out.append("Similar symbols: " + ", ".join(res['suggestions']))
        out.extend(res.get('hints', []))
    for i, r in enumerate(res["results"], 1):
        head = f'[{i}] {r["file_path"]}:{r["start_line"]}-{r["end_line"]}'
        if r["symbol"]:
            head += f'  {r["symbol_type"]} {r["symbol"]}'
        out.append(f'{head}  (score {r["score"]}, via {"+".join(r["matched_by"])})')
        if r["signature"]:
            out.append(f'    sig: {r["signature"]}')
        da = r.get("data_access")
        if da:
            chain = f' via {" > ".join(da["via_chain"])}' if da["via_chain"] else ""
            out.append(f'    data access: {da["access"]}/{da["op"]} {da["target"]} '
                       f'[{da["confidence"]}, {da["via"]}] line {da["line"]}{chain}')
        if r.get('explanation'):
            why = r['explanation']
            out.append('    matched query terms: ' + (', '.join(why['matched_query_terms']) or '(none)'))
            for layer, score in why['layers'].items():
                out.append(f"    {layer}: rank {score['rank']}, RRF contribution {score['rrf_contribution']:.8f}")
            for item in why['matched_expansions']:
                origins = ', '.join(source['source'] for source in item['sources'])
                out.append(f"    expansion: {item['term']} -> {', '.join(item['matched_terms'])} ({origins})")
            if why.get('routing') and why['routing'].get('evidence'):
                out.append('    evidence: ' + why['routing']['evidence'][:240])
        out.extend("    " + ln for ln in r["snippet"].split("\n"))
    page = res.get('pagination', {})
    if page.get('has_more'):
        out.append(f"More results: --cursor {page['next_cursor']}" if page.get('next_cursor') else
                   f"More results: --offset {page['next_offset']}")
    if res["related_files"]:
        out.append("Related files (dependency graph):")
        for rf in res["related_files"]:
            t = f' [{", ".join(rf["types"])}]' if rf["types"] else ""
            out.append(f'  {rf["file_path"]}{t}  <- {", ".join(rf["relations"])}')
    return "\n".join(out)


def render_relations(res: dict) -> str:
    if not res["definitions"]:
        msg = f'Symbol "{res["symbol"]}" not found.'
        if res.get("suggestions"):
            msg += " Similar: " + ", ".join(res["suggestions"])
        return msg
    out = []
    for d in res["definitions"]:
        out.append(f'{d["type"]} {d["qualified_name"]}  {d["file_path"]}:{d["start_line"]}-{d["end_line"]}')
        if d.get("signature"):
            out.append(f'  sig: {d["signature"]}')
        up = d.get("upstream")
        if up is not None:
            out.append("  UPSTREAM (who depends on it)")
            for x in up.get("callers", [])[:30]:
                out.append(f'    [{x["confidence"]}] {x["type"]} {x["symbol"]}  {x["file_path"]}:{x["line"]}  ({x["reason"]})')
            if up.get("callers_total", 0) > 30:
                out.append(f'    ... {up["callers_total"] - 30} more')
            for x in up.get("implementations", []):
                out.append(f'    implemented by {x["symbol"]}  {x["file_path"]}:{x["line"]}')
            for x in up.get("instantiated_by", [])[:15]:
                out.append(f'    [{x["confidence"]}] instantiated in {x["symbol"]}  {x["file_path"]}:{x["line"]}')
            for x in up["inheritors_or_implementers"]:
                out.append(f'    inherited/implemented by {x["type"]} {x["symbol"]}  {x["file_path"]}:{x["line"]}')
            for x in up["referenced_by"][:25]:
                out.append(f'    referenced at {x["file_path"]}:{x["line"]}' + (f' in {x["in_symbol"]}' if x["in_symbol"] else ""))
            if up["referenced_by_total"] > 25:
                out.append(f'    ... {up["referenced_by_total"] - 25} more references')
        down = d.get("downstream")
        if down is not None:
            out.append("  DOWNSTREAM (what it depends on)")
            for x in down.get("calls", [])[:40]:
                out.append(f'    [{x["confidence"]}] calls {x["type"]} {x["symbol"]}  {x["file_path"]}:{x["callee_line"]}'
                           f'  (line {x["line"]}; {x["reason"]})')
            for x in down.get("data_access", [])[:40]:
                chain = f' via {" > ".join(x["via_chain"])}' if x.get("via_chain") else ""
                out.append(f'    [{x["confidence"]}] {x["access"]}/{x["op"]} {x["target"]}  (line {x["line"]}; {x["via"]}){chain}')
            for b in down["base_types"]:
                where = ", ".join(f'{t["file_path"]}:{t["line"]}' for t in b["defined_in"]) or "external/unresolved"
                out.append(f'    base/interface {b["name"]}  -> {where}')
            for u in down["uses_types"][:30]:
                where = ", ".join(f'{t["file_path"]}:{t["line"]}' for t in u["defined_in"])
                out.append(f'    uses {u["name"]} (line {u["first_line"]})  -> {where}')
    if res.get("note"):
        out.append(f'note: {res["note"]}')
    return "\n".join(out)


def _da_line(e: dict) -> str:
    chain = f' via {" > ".join(e["via_chain"])}' if e["via_chain"] else ""
    return (f'    [{e["confidence"]}] {e["source_type"]} {e["source"]}  {e["file_path"]}:{e["line"]}  '
            f'{e["op"]} ({e["via"]}){chain}')


def _more(n: int, shown: int, full: bool) -> str:
    return f"    ... {n - shown} more" + ("" if full else " (detail=true / --full lists all)")


def render_data_access(res: dict, full: bool = False) -> str:
    """Five entries per section by default; full and paginated output never silently truncate."""
    if not res["found"]:
        msg = f'No table or procedure named "{res["object"]}" found.'
        if res.get("suggestions"):
            msg += " Similar: " + ", ".join(res["suggestions"])
        return msg
    out = [f'{res["kind"]} {res["object"]}']
    for d in res["definitions"]:
        out.append(f'  defined in {d["file_path"]}:{d["line"]}')
    sections = [("WRITTEN BY", "writers"), ("READ BY", "readers"), ("EXECUTED BY", "executed_by"),
                ("MAPPED BY (EF)", "mapped_by"), ("SCHEMA CHANGED BY", "schema_changes")]
    cap = max([len(res.get(k, [])) for k in ('writers', 'readers', 'executed_by', 'mapped_by',
                                                      'possible', 'no_known_caller', 'schema_changes')]
                  + [len(v) for v in res.get('touches', {}).values()] + [5]) if full or res.get("pagination") else 5
    for title, key in sections:
        items = res.get(key, [])
        if items:
            out.append(f"  {title} ({len(items)})")
            out.extend(_da_line(e) for e in items[:cap])
            if len(items) > cap:
                out.append(_more(len(items), cap, full))
    if res.get("programs"):
        out.append(f'  USED BY PROGRAMS ({len(res["programs"])})')
        for p in res["programs"]:
            out.append(f'    [{p["confidence"]}] {p["program"]}  {"/".join(p["access"])}  {p["file_path"]}  '
                       f'({", ".join(p["actions"] if full or res.get("pagination") else p["actions"][:6])}{", ... (use --full)" if len(p["actions"]) > 6 and not full and not res.get("pagination") else ""})')
            if p.get("pages"):
                out.append(f'        pages: {", ".join(p["pages"] if full or res.get("pagination") else p["pages"][:5])}{" ... (use --full)" if len(p["pages"]) > 5 and not full and not res.get("pagination") else ""}')
    roots = res.get("no_known_caller") or []
    if roots:
        out.append(f"  NO KNOWN CALLER - entry points (API, jobs, Main) or unresolved calls ({len(roots)})")
        out.extend(f'    {r["symbol"]}  {"/".join(r["access"])}  {r["file_path"]}:{r["line"]}' for r in roots[:cap])
        if len(roots) > cap:
            out.append(_more(len(roots), cap, full))
    t = res.get("touches")
    if t:
        for title, key in (("WRITES", "writes"), ("READS", "reads"), ("EXECUTES", "executes"), ("SCHEMA CHANGES", "schema_changes")):
            if t.get(key):
                out.append(f"  {title} ({len(t[key])})")
                for e in t[key][:cap]:
                    chain = f' via {" > ".join(e["via_chain"])}' if e["via_chain"] else ""
                    out.append(f'    [{e["confidence"]}] {e["target"]}  {e["file_path"]}:{e["line"]}  {e["op"]}{chain}')
                if len(t[key]) > cap:
                    out.append(_more(len(t[key]), cap, full))
        if t["dynamic_sql"]:
            out.append(f'  DYNAMIC SQL inside ({len(t["dynamic_sql"])}) - targets cannot be determined')
            for e in t["dynamic_sql"][:(cap if full or res.get("pagination") else cap // 4 + 1)]:
                out.append(f'    {e["file_path"]}:{e["line"]}  {e["evidence"][:100]}')
            if not full and not res.get('pagination') and len(t['dynamic_sql']) > cap // 4 + 1:
                out.append(_more(len(t['dynamic_sql']), cap // 4 + 1, False))
    if res["possible"]:
        out.append(f'  POSSIBLE (uncertain: dynamic SQL, EF SaveChanges on tracked entities) ({len(res["possible"])})')
        out.extend(_da_line(e) for e in res["possible"][:(cap if full or res.get("pagination") else cap // 2 or 1)])
    if not res.get('pagination') and not res['possible'] and not any(res.get(k) for _t, k in sections) and not (t and any(t.get(k) for k in ("reads", "writes", "executes", "schema_changes"))):
        out.append("  no recorded access")
    page = res.get('pagination')
    if page:
        for section, info in page['sections'].items():
            out.append(f"  {section}: {info['returned']} returned / {info['total']} total")
            if info['next_offset'] is not None:
                out.append(f"    more: --offset {info['next_offset']} --limit {page['limit']} (MCP: offset={info['next_offset']})")
    if res['possible'] and not full and not page and len(res['possible']) > (cap // 2 or 1):
        out.append(_more(len(res['possible']), cap // 2 or 1, False))
    for n in res.get("notes", []) if full else []:
        out.append(f"note: {n}")
    return "\n".join(out)


def render_impact(res: dict, full: bool = False) -> str:
    cap = max(len(res['changed']), len(res['tables']), len(res['no_known_caller']), 15) if full else 15
    out = [f'Impact of changes since {res["base"]}: {len(res["changed"])} changed symbols']
    for c in res["changed"][:cap]:
        lines = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in (c["lines"] if full else c["lines"][:4]))
        out.append(f'  {c["type"]} {c["symbol"]}  {c["file_path"]}:{lines} [{c.get("snapshot", "current")}]')
    if len(res["changed"]) > cap:
        out.append(_more(len(res["changed"]), cap, full))
    if res.get('removed_files'):
        out.append('  removed files: ' + ', '.join(res['removed_files']))
    if res.get('note'):
        out.append('  ' + res['note'])
    if res["unmapped_files"]:
        out.append(f'  changed outside any symbol: {", ".join(res["unmapped_files"] if full else res["unmapped_files"][:10])}'
                   + (" ... (use --full)" if len(res["unmapped_files"]) > 10 and not full else ""))
    if res["tables"]:
        tables: dict[str, set[str]] = defaultdict(set)
        names: dict[str, str] = {}
        for t in res["tables"]:  # SQL Server names are case-insensitive: SFC_MLot and SFC_Mlot are one table
            names.setdefault(t["target"].lower(), t["target"])
            tables[t["target"].lower()].add(t["access"])
        out.append(f"  TABLES / PROCEDURES ACCESSED BY CHANGED CODE ({len(tables)})")
        out.extend(f'    {names[k]}  {"/".join(sorted(acc))}' for k, acc in sorted(tables.items())[:cap])
    if res["programs"]:
        out.append(f'  AFFECTED PROGRAMS ({len(res["programs"])})')
        for p in res["programs"]:
            out.append(f'    [{p["confidence"]}] {p["program"]}  {p["file_path"]}  ({", ".join(p["actions"][:6])}'
                       f'{", ..." if len(p["actions"]) > 6 else ""})')
            if p.get("pages"):
                out.append(f'        pages: {", ".join(p["pages"] if full or res.get("pagination") else p["pages"][:5])}{" ... (use --full)" if len(p["pages"]) > 5 and not full and not res.get("pagination") else ""}')
    if res["no_known_caller"]:
        out.append(f'  NO KNOWN CALLER ({len(res["no_known_caller"])})')
        out.extend(f'    {r["symbol"]}  {r["file_path"]}:{r["line"]} [{r.get("snapshot", "current")}]' for r in res["no_known_caller"][:cap])
    if not res["changed"] and not res["unmapped_files"]:
        out.append("  no changes")
    return "\n".join(out)


def render_status(st: dict) -> str:
    import datetime as _dt

    when = _dt.datetime.fromtimestamp(st["last_indexed_at"]).strftime("%Y-%m-%d %H:%M:%S") if st["last_indexed_at"] else "never"
    srv = st.get("server") or {}
    out = [f'index state: {st["state"].upper()}   server: {srv.get("version", "?")}',
           f'last indexed: {when}   files: {st["indexed_files"]}   symbols: {st["symbols"]}   chunks: {st["chunks"]}',
           f'embedder: {st["embedder"]}   index version: {st["index_version"]}']
    if srv.get("code_changed_since_start"):
        out.append("warning: the indexer's code changed on disk after this process started; restart the MCP server "
                   "(new Claude Code session) to run it")
    g = st["git"]
    if g["current"] is None and g["indexed"] is None:
        out.append("git: not a git work tree")
    else:
        for label, info in (("indexed at", g["indexed"]), ("current   ", g["current"])):
            if info:
                out.append(f'git {label}: {info["commit"][:10]} ({info["branch"] or "?"}), {info["dirty_files"]} uncommitted paths')
        if g["head_moved"]:
            out.append("git: HEAD has moved since the last index update")
    pc = st["pending_changes"]
    for key, label in (("new_files", "new"), ("deleted_files", "deleted"), ("possibly_modified_files", "possibly modified")):
        if pc[key]["count"]:
            more = " ..." if pc[key]["count"] > len(pc[key]["examples"]) else ""
            out.append(f'{label} since last update ({pc[key]["count"]}): ' + ", ".join(pc[key]["examples"]) + more)
    if st["failed_files"]:
        out.append(f'failed files ({len(st["failed_files"])}), not searchable until fixed:')
        out.extend(f'  {f["file_path"]}: {f["error"]}' for f in st["failed_files"][:20])
    if st["consistency"]["ok"]:
        out.append("consistency: ok")
    else:
        out.append("consistency problems (run `index --force` to rebuild):")
        out.extend("  " + p for p in st["consistency"]["problems"])
    if st.get("index_format_outdated"):
        out.append("index was built by an older version and will be rebuilt on the next update")
    if st["state"] in ("stale", "not_indexed"):
        out.append("hint: run `local-codebase-indexer index` (search/relations/context also refresh automatically)")
    return "\n".join(out)


def render_context(res: dict) -> str:
    head = f'{res["file_path"]} ({res["total_lines"]} lines), ranges: ' + ", ".join(f"{a}-{b}" for a, b in res["ranges"])
    if res["enclosing_symbols"]:
        head += "\nenclosing: " + "; ".join(
            f'{s["type"]} {s["symbol"]} ({s["start_line"]}-{s["end_line"]})' for s in res["enclosing_symbols"])
    tail = "\n[truncated: output limit reached; request fewer or narrower ranges]" if res["truncated"] else ""
    return head + "\n" + res["context"] + tail


def render_doctor(res: dict) -> str:
    out = [f"Project: {res['root']}", f"Indexable text files: {res['eligible_files']}",
           f"Index state: {res['index']['state']}"]
    for reason, count in sorted(res['counts'].items()):
        out.append(f"  {reason}: {count}")
        out.extend('    ' + path for path in res['examples'][reason][:3])
    if res['path']:
        out.append(f"{res['path']['path']}: {res['path']['reason']}")
    out.extend(res['hints'])
    out.append(res['note'])
    return '\n'.join(out)
