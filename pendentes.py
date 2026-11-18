#!/usr/bin/env python3
"""Ferramentas para os artigos que o doi_to_drive.py não conseguiu baixar.

  lista     Gera pendentes.txt (DOIs para colar no Zotero) e pendentes.html
            (links agrupados por editora) a partir do relatório results.csv.
  importar  Lê PDFs baixados manualmente (Zotero, navegador), descobre o DOI de
            cada um, renomeia no padrão Titulo_DOI.pdf, move para a pasta de
            artigos e marca o artigo como baixado no relatório.
"""
from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import shutil
import sys
from pathlib import Path

from doi_to_drive import (
    ArticleError,
    find_existing,
    index_existing_pdfs,
    normalize_doi,
    normalize_title,
    safe_filename,
)

PUBLISHERS = {
    "10.1109": "IEEE", "10.1145": "ACM", "10.1007": "Springer", "10.1186": "Springer (BMC)",
    "10.1016": "Elsevier", "10.1002": "Wiley", "10.1111": "Wiley", "10.3390": "MDPI",
    "10.1049": "IET", "10.5220": "SciTePress", "10.1142": "World Scientific", "10.1080": "Taylor & Francis",
    "10.1177": "SAGE", "10.1371": "PLOS", "10.7717": "PeerJ", "10.48550": "arXiv", "10.3233": "IOS Press",
    "10.14569": "IJACSA (SAI)", "10.1108": "Emerald", "10.1093": "Oxford",
}
REASON_TEXT = {
    "not_open_access": "sem versão aberta",
    "download_failed": "bloqueado / não baixou",
    "not_found": "DOI não encontrado",
    "api_error": "erro de consulta",
    "error": "erro",
}
logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # PDFs reais geram muitos avisos inofensivos
DOI_IN_TEXT_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>{}|\\^`\[\]]+", re.IGNORECASE)


def publisher_of(doi: str) -> str:
    prefix = doi.split("/", 1)[0].lower()
    return PUBLISHERS.get(prefix, f"Outras ({prefix})")


def read_report(report_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not report_path.exists():
        raise SystemExit(f"Relatório não encontrado: {report_path}. Rode o doi_to_drive.py primeiro.")
    with report_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def row_doi(row: dict[str, str]) -> str | None:
    for value in (row.get("doi_found", ""), row.get("doi", "")):
        try:
            return normalize_doi(value)
        except ArticleError:
            continue
    return None


# ----------------------------------------------------------------- lista
def cmd_lista(args: argparse.Namespace) -> int:
    download_dir = Path(args.download_dir)
    _, rows = read_report(download_dir / "results.csv")
    pending = [r for r in rows if r.get("status") == "failed" and r.get("reason") != "duplicate"]

    groups: dict[str, list[tuple[str, dict[str, str]]]] = {}
    no_doi: list[dict[str, str]] = []
    for row in pending:
        doi = row_doi(row)
        if doi:
            groups.setdefault(publisher_of(doi), []).append((doi, row))
        else:
            no_doi.append(row)

    all_dois = [doi for items in groups.values() for doi, _ in items]
    (download_dir / "pendentes.txt").write_text("\n".join(all_dois) + "\n", encoding="utf-8")
    html_path = download_dir / "pendentes.html"
    html_path.write_text(render_html(groups, no_doi, len(rows)), encoding="utf-8")

    print(f"{len(pending)} artigos pendentes ({len(all_dois)} com DOI, {len(no_doi)} sem DOI).")
    others = 0
    for name, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if name.startswith("Outras"):
            others += len(items)
        else:
            print(f"  {name}: {len(items)}")
    if others:
        print(f"  Outras editoras: {others}")
    print(f"DOIs para o Zotero: {(download_dir / 'pendentes.txt').resolve()}")
    print(f"Links por editora:  {html_path.resolve()}")
    return 0


def render_html(groups: dict[str, list[tuple[str, dict[str, str]]]], no_doi: list[dict[str, str]], total: int) -> str:
    e = html.escape
    count = sum(len(v) for v in groups.values()) + len(no_doi)
    parts = [f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Artigos pendentes</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--accent:#0a66c2;--chip:#f2f2f7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1c1e;--fg:#f2f2f7;--muted:#a1a1a6;--line:#38383a;--accent:#4ea1ff;--chip:#2c2c2e}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:0 auto;padding:24px 16px}}
h1{{font-size:1.5rem;margin:0 0 4px}} h2{{font-size:1.15rem;margin:32px 0 8px;display:flex;gap:8px;align-items:center}}
.muted{{color:var(--muted)}} .n{{background:var(--chip);border-radius:999px;padding:0 10px;font-size:.85rem}}
nav a{{margin-right:12px;color:var(--accent)}} a{{color:var(--accent)}}
table{{width:100%;border-collapse:collapse}} td{{border-top:1px solid var(--line);padding:6px 4px;vertical-align:top}}
td.r{{white-space:nowrap;color:var(--muted);font-size:.85rem}}
textarea{{width:100%;height:70px;background:var(--chip);color:var(--fg);border:1px solid var(--line);border-radius:6px;font:12px monospace}}
button{{font:inherit;font-size:.85rem;padding:2px 10px;border-radius:6px;border:1px solid var(--line);background:var(--chip);color:var(--fg);cursor:pointer}}
</style></head><body>
<h1>Artigos pendentes</h1>
<p class="muted">{count} de {total} artigos ainda não estão na pasta. Abra os links com o acesso da universidade
(CAPES/CAFe, VPN) ou cole a lista de DOIs no Zotero (“Adicionar item por identificador”). Depois rode
<code>python pendentes.py importar PASTA_DOS_PDFS</code>.</p><nav>"""]
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for i, (name, items) in enumerate(ordered):
        parts.append(f'<a href="#g{i}">{e(name)} ({len(items)})</a>')
    if no_doi:
        parts.append(f'<a href="#semdoi">Sem DOI ({len(no_doi)})</a>')
    parts.append("</nav>")
    for i, (name, items) in enumerate(ordered):
        dois = "\n".join(doi for doi, _ in items)
        parts.append(f'<h2 id="g{i}">{e(name)} <span class="n">{len(items)}</span>'
                     f'<button onclick="navigator.clipboard.writeText(document.getElementById(\'t{i}\').value)">'
                     f'copiar DOIs</button></h2><textarea id="t{i}" readonly>{e(dois)}</textarea><table>')
        for doi, row in items:
            reason = REASON_TEXT.get(row.get("reason", ""), row.get("reason", ""))
            parts.append(f'<tr><td><a href="https://doi.org/{e(doi)}" target="_blank" rel="noopener">'
                         f'{e(row.get("title") or doi)}</a><br><span class="muted">{e(doi)}</span></td>'
                         f'<td class="r">{e(reason)}</td></tr>')
        parts.append("</table>")
    if no_doi:
        parts.append(f'<h2 id="semdoi">Sem DOI <span class="n">{len(no_doi)}</span></h2>'
                     '<p class="muted">Busca pelo título no Google Acadêmico.</p><table>')
        for row in no_doi:
            title = row.get("title", "")
            url = "https://scholar.google.com/scholar?q=" + html.escape(re.sub(r"\s+", "+", title.strip()))
            parts.append(f'<tr><td><a href="{url}" target="_blank" rel="noopener">{e(title)}</a></td></tr>')
        parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts)


# -------------------------------------------------------------- importar
def pdf_text(path: Path, pages: int = 2) -> str:
    from pypdf import PdfReader  # import aqui para "lista" funcionar sem pypdf

    reader = PdfReader(str(path))
    chunks = []
    meta = reader.metadata or {}
    for key in ("/doi", "/Subject", "/Title", "/Keywords"):
        if meta.get(key):
            chunks.append(str(meta.get(key)))
    for page in reader.pages[:pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # páginas com codificação estranha
            pass
    return "\n".join(chunks)


def clean_doi(raw: str) -> str:
    return raw.rstrip(".,;:)]}'\"").strip()


def identify(path: Path, by_doi: dict[str, dict[str, str]], titles: list[tuple[str, dict[str, str]]]) -> tuple[str, dict[str, str], str] | None:
    """Return (doi, row, how) for a PDF, matching against the article list."""
    text = pdf_text(path)
    # 1) DOI escrito no PDF que esteja na lista
    for raw in DOI_IN_TEXT_RE.findall(text):
        doi = clean_doi(raw).lower()
        if doi in by_doi:
            return row_doi(by_doi[doi]) or doi, by_doi[doi], "DOI no PDF"
    # 2) DOI no nome do arquivo (ex.: 10.1109_xxx.pdf ou nomes do Zotero com DOI)
    stem = path.stem.replace("_", "/", 1) if path.stem.startswith("10.") else path.stem
    for raw in DOI_IN_TEXT_RE.findall(stem):
        doi = clean_doi(raw).lower()
        if doi in by_doi:
            return row_doi(by_doi[doi]) or doi, by_doi[doi], "DOI no nome do arquivo"
    # 3) título da lista aparecendo no texto da 1ª/2ª página
    flat = normalize_title(text)
    best = None
    for norm, row in titles:
        if len(norm) >= 25 and norm in flat and (best is None or len(norm) > len(best[0])):
            best = (norm, row)
    if best:
        doi = row_doi(best[1])
        if doi:
            return doi, best[1], "título no PDF"
    return None


def cmd_importar(args: argparse.Namespace) -> int:
    source_dir = Path(args.pasta).expanduser()
    download_dir = Path(args.download_dir)
    report_path = download_dir / "results.csv"
    fieldnames, rows = read_report(report_path)
    if not source_dir.is_dir():
        raise SystemExit(f"Pasta não encontrada: {source_dir}")

    by_doi: dict[str, dict[str, str]] = {}
    titles: list[tuple[str, dict[str, str]]] = []
    for row in rows:
        doi = row_doi(row)
        if doi:
            by_doi.setdefault(doi.lower(), row)
            titles.append((normalize_title(row.get("title", "")), row))

    existing = index_existing_pdfs(download_dir)
    pdfs = sorted(p for p in source_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    if source_dir.resolve() == download_dir.resolve():
        raise SystemExit("Use uma pasta diferente da pasta de artigos.")
    imported = skipped = unknown = 0
    for pdf in pdfs:
        try:
            match = identify(pdf, by_doi, titles)
        except Exception as exc:
            print(f"  ? {pdf.name}: não foi possível ler o PDF ({exc})")
            unknown += 1
            continue
        if not match:
            print(f"  ? {pdf.name}: artigo não identificado na lista (ficou na pasta)")
            unknown += 1
            continue
        doi, row, how = match
        already = find_existing(existing, doi)
        if already:
            print(f"  = {pdf.name}: já existe como {already.name} (ficou na pasta)")
            skipped += 1
            continue
        target = download_dir / safe_filename(row.get("title", ""), doi)
        if args.dry_run:
            print(f"  → {pdf.name}  ⇒  {target.name}  [{how}]  (simulação)")
        else:
            shutil.move(str(pdf), str(target))
            existing[target.name.lower()] = target
            row.update({"status": "ok", "reason": "already_downloaded", "file": target.name,
                        "source": "manual", "message": f"Importado manualmente ({how}); arquivo original: {pdf.name}"})
            print(f"  ✓ {pdf.name}  ⇒  {target.name}  [{how}]")
        imported += 1

    verb = "seriam importados" if args.dry_run else "importados"
    print(f"\n{imported} {verb}, {skipped} já existiam, {unknown} não identificados.")
    if imported and not args.dry_run:
        with report_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Relatório atualizado: {report_path.resolve()}")
        cmd_lista(args)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--download-dir", default="artigos_baixados", help="Pasta dos artigos e do results.csv")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lista", help="Gera pendentes.txt e pendentes.html")
    imp = sub.add_parser("importar", help="Importa PDFs baixados manualmente")
    imp.add_argument("pasta", help="Pasta com os PDFs baixados (inclui subpastas, ex.: storage do Zotero)")
    imp.add_argument("--dry-run", action="store_true", help="Só mostra o que seria feito, sem mover nada")
    args = parser.parse_args()
    return cmd_lista(args) if args.cmd == "lista" else cmd_importar(args)


if __name__ == "__main__":
    sys.exit(main())
