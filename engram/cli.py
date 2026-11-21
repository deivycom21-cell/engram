"""Command-line interface: ``engram <command>``."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
import webbrowser
from pathlib import Path

from engram import __version__


def _out(data, as_json: bool) -> bool:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    return as_json


def _engine(args):
    from engram.core.config import load_settings
    from engram.core.engine import EngramEngine

    return EngramEngine(load_settings(data_dir=args.data_dir))


def _wrap(text: str, indent: str = "    ", width: int = 100, first: str | None = None) -> str:
    return textwrap.fill(" ".join(text.split()), width=width, initial_indent=first or indent, subsequent_indent=indent)


# ------------------------------------------------------------------ commands
def cmd_serve(args) -> int:
    import uvicorn

    from engram.api.app import create_app
    from engram.core.config import load_settings
    from engram.core.engine import EngramEngine

    settings = load_settings(data_dir=args.data_dir, host=args.host, port=args.port)
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠  Listening on {settings.host}: other devices on the network can reach your memory. "
              f"ENGRAM has no authentication; prefer 127.0.0.1.", file=sys.stderr)
    engine = EngramEngine(settings)
    app = create_app(engine)
    url = f"http://{'127.0.0.1' if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}"
    st = engine.status()
    print(f"ENGRAM {__version__} · data: {settings.data_dir}")
    print(f"embeddings: {st['embedding_provider']} — {st['embedding_status']}")
    print(f"open {url}")
    if args.open:
        webbrowser.open(url)
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    finally:
        engine.close()
    return 0


def cmd_ingest(args) -> int:
    with _engine(args) as e:
        results = []
        for p in args.paths:
            results += e.ingest(p, recursive=not args.no_recursive)
        if _out([r.as_dict() for r in results], args.json):
            return 0 if all(r.status != "failed" for r in results) else 1
        for r in results:
            mark = {"ingested": "✓", "duplicate": "=", "failed": "✗"}[r.status]
            extra = f"{r.engrams} engrams" if r.status == "ingested" else (r.error or "; ".join(r.warnings))
            print(f" {mark} {r.filename:<48} {r.status:<10} {extra}")
            assoc = {k: v for k, v in r.associations.items() if isinstance(v, int) and v and k != "examined"}
            if assoc:
                print("     relations proposed: " + ", ".join(f"{k}={v}" for k, v in assoc.items()))
            for w in r.warnings if r.status == "ingested" else []:
                print(f"     ⚠ {w}")
        failed = sum(r.status == "failed" for r in results)
        print(f"\n{len(results)} file(s): {sum(r.status == 'ingested' for r in results)} ingested, "
              f"{sum(r.status == 'duplicate' for r in results)} already known, {failed} failed")
        return 1 if failed else 0


def cmd_search(args) -> int:
    with _engine(args) as e:
        res = e.search(args.query, limit=args.limit, kinds=args.kinds.split(",") if args.kinds else None)
        if _out(res, args.json):
            return 0
        a = res["analysis"]
        print(f"terms: {a['terms']}  hints: {a['kind_hints']}  prefer: {a['prefer']}  period: {a['date_from']}–{a['date_to']}")
        if not res["results"]:
            print("No matching memories.")
        for r in res["results"]:
            g = r["engram"]
            p = g["provenance"]
            loc = f"p.{p['page']}" if p["page"] else (p["section"] or "")
            date = g["occurred_at"] or g["created_at"] or "undated"
            print(f"\n#{r['rank']} [{g['kind']}] {date} · {p['document_title']} {('· ' + loc) if loc else ''}  (id {g['id']}, rel {r['relevance']})")
            print(_wrap(g["content"][:400]))
            print("    why: " + "; ".join(r["why"]))
        return 0


def cmd_reconstruct(args) -> int:
    with _engine(args) as e:
        rec = e.reconstruct(args.question, narrate=args.narrate, consent_external=args.consent_external)
        if _out(rec, args.json):
            return 0
        print(f"\nRECONSTRUCTION — {rec['question']}\n")
        for line in rec["summary"]:
            print(_wrap(line, "    ", 100, "  * "))
        if rec.get("chronology"):
            print("\nCHRONOLOGY")
            for c in rec["chronology"]:
                print(f"  {c['date']:<11} [{c['label']}] {c['kind']:<11} {c['excerpt'][:90]}")
                print(f"              date from: {c['date_source']}")
        if rec.get("changes"):
            print("\nCHANGES (inferred, verify)")
            for c in rec["changes"]:
                print(f"  [{c['from']}] → [{c['to']}] {c['type']}")
        print("\nWHAT CANNOT BE DETERMINED")
        for u in rec["unknowns"]:
            print(_wrap(u, "    ", 100, "  - "))
        print("\nEVIDENCE")
        for ev in rec["evidence"]:
            g = ev["engram"]
            p = g["provenance"]
            loc = f", page {p['page']}" if p["page"] else (f", § {p['section']}" if p["section"] else "")
            print(f"  [{ev['label']}] {p['document_title']} ({p['filename']}{loc}, chars {p['char_start']}–{p['char_end']}) "
                  f"— {ev['date'] or 'undated'}")
            print(_wrap(g["content"][:300], "       "))
        if rec.get("narrative"):
            n = rec["narrative"]
            print("\nNARRATIVE (generated — verify against evidence)")
            print(_wrap(n.get("text") or n.get("error", ""), "  "))
        return 0


def cmd_evolution(args) -> int:
    with _engine(args) as e:
        ev = e.evolution(args.topic)
        if _out(ev, args.json):
            return 0
        fa = ev["first_appearance"]
        print(f"first appearance: {fa['date']} ({fa['date_source']}) — {fa['engram']['content'][:120]}" if fa
              else "first appearance: unknown (no dated records)")
        for c in ev["chronology"]:
            print(f"  {c['date']:<11} {c['engram']['kind']:<11} {c['engram']['provenance']['document_title'][:30]:<30} "
                  f"{c['engram']['content'][:70]}")
        for n in ev["notes"]:
            print(f"  note: {n}")
        return 0


def cmd_consolidate(args) -> int:
    with _engine(args) as e:
        rep = e.consolidate()
        if not _out(rep, args.json):
            print(json.dumps(rep, indent=2))
        return 0


def cmd_insights(args) -> int:
    with _engine(args) as e:
        items = e.store.list_insights("open", args.kind)
        if _out(items, args.json):
            return 0
        for it in items:
            print(f"[{it['kind']}] {it['title']}  (score {it['score']:.2f}, engrams {it['engram_ids']})")
            print(_wrap(it["explanation"][:500]))
        if not items:
            print("No open insights. Run `engram consolidate` after importing more documents.")
        return 0


def cmd_stats(args) -> int:
    with _engine(args) as e:
        data = {"status": e.status(), "stats": e.store.stats()}
        if not _out(data, args.json):
            print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0


def cmd_doctor(args) -> int:
    from engram.core.config import load_settings
    from engram.core.lifecycle import doctor

    ok = True
    for name, passed, detail in doctor(load_settings(data_dir=args.data_dir)):
        optional = "optional" in name or "model" in name
        mark = "✓" if passed else ("·" if optional else "✗")
        ok &= passed or optional
        print(f" {mark} {name:<40} {detail}")
    return 0 if ok else 1


def cmd_backup(args) -> int:
    with _engine(args) as e:
        p = e.backup()
        print(f"backup written: {p} ({p.stat().st_size / 1e6:.1f} MB)")
        return 0


def cmd_export(args) -> int:
    with _engine(args) as e:
        p = e.export_json()
        print(f"export written: {p}")
        return 0


def cmd_models(args) -> int:
    from engram.core.config import load_settings
    from engram.memory.embeddings import ONNX_MODELS, download_onnx_model, onnx_model_available

    s = load_settings(data_dir=args.data_dir)
    s.ensure_dirs()
    if args.action == "list":
        for name in ONNX_MODELS:
            print(f" {'✓' if onnx_model_available(s.models_dir, name) else '·'} {name}  ({ONNX_MODELS[name]['repo']})")
        return 0
    model = args.model or s.onnx_model
    print(f"Downloading {model} (public model files only; no personal data is sent) into {s.models_dir}")
    last = [""]

    def progress(msg: str):
        if msg.strip().endswith("MB)"):
            if msg[:8] != last[0][:8]:
                print(msg, end="\r", flush=True)
            last[0] = msg
        else:
            print(msg)

    download_onnx_model(s.models_dir, model, progress)
    print("\ndone. Existing memories keep their old vectors; run `engram reindex` to embed them with this model.")
    return 0


def cmd_reindex(args) -> int:
    """Embed engrams that have no vector for the active model (e.g. after downloading the model)."""
    with _engine(args) as e:
        if not e.provider:
            print("no embedding provider configured")
            return 1
        rows = e.db.query(
            "SELECT g.id, g.title, g.content FROM engrams g WHERE g.status='active' AND NOT EXISTS "
            "(SELECT 1 FROM embeddings x WHERE x.engram_id=g.id AND x.model=?)", (e.provider.name,))
        print(f"embedding {len(rows)} engram(s) with {e.provider.name} …")
        for i in range(0, len(rows), 64):
            part = rows[i : i + 64]
            vecs = e.provider.embed([(f"{r['title']}: " if r["title"] else "") + r["content"] for r in part])
            with e.db.transaction() as conn:
                for r, v in zip(part, vecs):
                    e.vectors.add(conn, r["id"], v)
        e.vectors.invalidate()
        print("re-running consolidation to propose relations with the new vectors …")
        print(json.dumps(e.consolidate()["new_relations"]))
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engram", description="ENGRAM — local-first second brain")
    p.add_argument("--version", action="version", version=f"engram {__version__}")
    p.add_argument("--data-dir", default=None, help="data directory (default: $ENGRAM_DATA_DIR, ./data in a checkout, ~/.engram)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    sp = add("serve", cmd_serve, "start the local web app")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--open", action="store_true", help="open the browser")
    sp = add("ingest", cmd_ingest, "import files or folders (pdf, md, txt)")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--no-recursive", action="store_true")
    sp = add("search", cmd_search, "contextual recall")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--kinds", default=None, help="comma-separated: decision,idea,…")
    sp = add("reconstruct", cmd_reconstruct, "reconstruct how a decision/idea evolved, with evidence")
    sp.add_argument("question")
    sp.add_argument("--narrate", action="store_true", help="add an optional LLM narrative (needs llm_provider)")
    sp.add_argument("--consent-external", action="store_true", help="allow sending evidence to an external LLM")
    sp = add("evolution", cmd_evolution, "chronology of a topic")
    sp.add_argument("topic")
    add("consolidate", cmd_consolidate, "detect duplicates, contradictions, new relations (non-destructive)")
    sp = add("insights", cmd_insights, "list open insights")
    sp.add_argument("--kind", default=None)
    add("stats", cmd_stats, "memory statistics")
    add("doctor", cmd_doctor, "check the environment")
    add("backup", cmd_backup, "zip database + original files")
    add("export", cmd_export, "portable JSON export")
    add("reindex", cmd_reindex, "embed engrams missing a vector for the active model")
    sp = add("models", cmd_models, "manage local embedding models")
    sp.add_argument("action", choices=["download", "list"])
    sp.add_argument("--model", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "Path"]
