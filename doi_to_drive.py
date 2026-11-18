#!/usr/bin/env python3
"""Find open-access article PDFs by DOI and save them on this computer.

Sources (queried in order, stopping at the first valid PDF): Unpaywall, OpenAlex,
Semantic Scholar (including arXiv copies) and, when ELSEVIER_API_KEY is set, the Elsevier
ScienceDirect API for articles your institution subscribes to. Missing DOIs are looked up
by title (OpenAlex, Crossref).
This tool does not bypass publisher paywalls or authentication.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import html
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests

UNPAYWALL_URL = "https://api.unpaywall.org/v2/{doi}"
OPENALEX_URL = "https://api.openalex.org/works/doi:{doi}"
S2_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
ELSEVIER_ENTITLEMENT_URL = "https://api.elsevier.com/content/article/entitlement/doi/{doi}"
ELSEVIER_ARTICLE_URL = "https://api.elsevier.com/content/article/doi/{doi}"
OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
ALL_SOURCES = ["unpaywall", "openalex", "semanticscholar", "elsevier"]
SOURCE_LABELS = {
    "unpaywall": "Unpaywall", "openalex": "OpenAlex", "semanticscholar": "Semantic Scholar", "elsevier": "Elsevier",
}
TITLE_MATCH_THRESHOLD = 0.93  # semelhança mínima entre títulos para aceitar um DOI encontrado
USER_AGENT = "doi-article-downloader/1.0 (open-access article downloader)"
REPORT_FIELDS = ["title", "doi", "doi_found", "status", "reason", "file", "source", "source_url", "message"]

# Motivos (coluna "reason") usados no relatório.
REASON_LABELS = {
    "downloaded": "Baixado agora",
    "already_downloaded": "Já existia na pasta",
    "invalid_doi": "DOI inválido ou vazio",
    "duplicate": "DOI repetido na lista",
    "not_found": "DOI não encontrado em nenhuma fonte",
    "not_open_access": "Sem versão de acesso aberto em nenhuma fonte",
    "download_failed": "Versão encontrada, mas o PDF não pôde ser baixado",
    "api_error": "Erro ao consultar as fontes",
    "error": "Erro inesperado",
}


class ArticleError(Exception):
    """Failure with a report category (reason)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def normalize_doi(value: str) -> str:
    """Accept bare DOI or common DOI URL forms and return the DOI identifier."""
    doi = (value or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE).strip()
    if not re.match(r"^10\.\d{4,9}/\S+$", doi, flags=re.IGNORECASE):
        raise ArticleError("invalid_doi", f"DOI inválido: {value!r}")
    return doi


def doi_file_part(doi: str) -> str:
    return re.sub(r"[^\w.-]+", "_", doi, flags=re.UNICODE).strip("_.")


def safe_filename(title: str, doi: str) -> str:
    """Create a filesystem-safe PDF name from an article title and DOI."""
    title_part = re.sub(r"[^\w.-]+", "_", (title or "artigo").strip(), flags=re.UNICODE)
    title_part = re.sub(r"_+", "_", title_part).strip("_.")[:120] or "artigo"
    return f"{title_part}_{doi_file_part(doi)}.pdf"


def index_existing_pdfs(download_dir: Path) -> dict[str, Path]:
    """Map lowercase filename -> path for non-empty PDFs already in the folder."""
    return {p.name.lower(): p for p in download_dir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0}


def find_existing(existing: dict[str, Path], doi: str) -> Path | None:
    """Find an already-downloaded PDF by DOI (name ends with _<doi>.pdf), even if the title changed."""
    suffix = f"_{doi_file_part(doi)}.pdf".lower()
    for name, path in existing.items():
        if name.endswith(suffix) or name == suffix[1:]:
            return path
    return None


class SourceLookup(Exception):
    """A source had no usable result. kind: not_found | no_oa | api_error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _add(urls: list[str], *candidates: Any) -> None:
    for url in candidates:
        if isinstance(url, str) and url.startswith("http") and url not in urls:
            urls.append(url)


def _get_json(session: requests.Session, label: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """GET a JSON API, retrying once on HTTP 429 (rate limit)."""
    for attempt in range(2):
        try:
            response = session.get(url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise SourceLookup("api_error", f"{label}: falha de conexão ({exc})") from exc
        if response.status_code == 429 and attempt == 0:
            retry_after = response.headers.get("Retry-After", "")
            time.sleep(min(float(retry_after), 30) if retry_after.isdigit() else 5)
            continue
        if response.status_code == 404:
            raise SourceLookup("not_found", f"{label}: DOI não encontrado")
        if not response.ok:
            raise SourceLookup("api_error", f"{label}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceLookup("api_error", f"{label}: resposta inválida") from exc
    raise SourceLookup("api_error", f"{label}: limite de requisições (HTTP 429)")


def open_access_pdf_urls(metadata: dict[str, Any]) -> list[str]:
    """Return deduplicated PDF-first URLs from Unpaywall OA locations."""
    if metadata.get("is_oa") is not True:
        return []
    locations = []
    best = metadata.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)
    locations.extend(loc for loc in (metadata.get("oa_locations") or []) if isinstance(loc, dict))
    urls: list[str] = []
    for location in locations:
        _add(urls, location.get("url_for_pdf"), location.get("url"))
    return urls


def urls_from_unpaywall(doi: str, session: requests.Session, args: argparse.Namespace) -> list[str]:
    data = _get_json(session, "Unpaywall", UNPAYWALL_URL.format(doi=quote(doi, safe="/")), params={"email": args.email})
    return open_access_pdf_urls(data)


def urls_from_openalex(doi: str, session: requests.Session, args: argparse.Namespace) -> list[str]:
    params = {"mailto": args.email}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    data = _get_json(session, "OpenAlex", OPENALEX_URL.format(doi=quote(doi, safe="/")), params=params)
    urls: list[str] = []
    locations = [data.get("best_oa_location")] + list(data.get("locations") or [])
    oa_locations = [loc for loc in locations if isinstance(loc, dict) and loc.get("is_oa")]
    for loc in oa_locations:  # PDFs diretos primeiro, páginas depois
        _add(urls, loc.get("pdf_url"))
    for loc in oa_locations:
        _add(urls, loc.get("landing_page_url"))
    _add(urls, (data.get("open_access") or {}).get("oa_url"))
    return urls


def urls_from_semanticscholar(doi: str, session: requests.Session, args: argparse.Namespace) -> list[str]:
    headers = {}
    if os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    data = _get_json(
        session, "Semantic Scholar", S2_URL.format(doi=quote(doi, safe="/")),
        params={"fields": "openAccessPdf,externalIds"}, headers=headers,
    )
    urls: list[str] = []
    _add(urls, (data.get("openAccessPdf") or {}).get("url"))
    arxiv_id = (data.get("externalIds") or {}).get("ArXiv")
    if arxiv_id:
        _add(urls, f"https://arxiv.org/pdf/{arxiv_id}")
    return urls


def elsevier_headers() -> dict[str, str]:
    headers = {"X-ELS-APIKey": os.environ.get("ELSEVIER_API_KEY", "")}
    if os.environ.get("ELSEVIER_INSTTOKEN"):
        headers["X-ELS-Insttoken"] = os.environ["ELSEVIER_INSTTOKEN"]
    return headers


def urls_from_elsevier(doi: str, session: requests.Session, args: argparse.Namespace) -> list[str]:
    """Elsevier ScienceDirect: only for 10.1016 DOIs the institution is entitled to."""
    if not doi.lower().startswith("10.1016/"):
        raise SourceLookup("skip", "")
    try:
        data = _get_json(
            session, "Elsevier", ELSEVIER_ENTITLEMENT_URL.format(doi=quote(doi, safe="/")),
            headers={**elsevier_headers(), "Accept": "application/json"},
        )
    except SourceLookup as exc:
        if exc.kind == "api_error":
            raise SourceLookup(
                "api_error", f"{exc} (verifique ELSEVIER_API_KEY e se está na rede/VPN da instituição)"
            ) from exc
        raise
    entitlement = (data.get("entitlement-response") or {}).get("document-entitlement") or {}
    if isinstance(entitlement, list):
        entitlement = entitlement[0] if entitlement else {}
    if str(entitlement.get("entitled", "")).lower() != "true":
        raise SourceLookup("no_oa", "Elsevier: sem acesso institucional a este artigo (está na rede/VPN da instituição?)")
    return [ELSEVIER_ARTICLE_URL.format(doi=quote(doi, safe="/"))]


SOURCE_FUNCS = {
    "unpaywall": urls_from_unpaywall,
    "openalex": urls_from_openalex,
    "semanticscholar": urls_from_semanticscholar,
    "elsevier": urls_from_elsevier,
}
# Cabeçalhos extras usados ao baixar o PDF de cada fonte.
SOURCE_DOWNLOAD_HEADERS = {"elsevier": lambda: {**elsevier_headers(), "Accept": "application/pdf"}}


def pdf_page_count(content: bytes) -> int:
    """Rough page count (0 when page objects are compressed and can't be counted)."""
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", content))


CITATION_PDF_RE = re.compile(
    r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)["']"""
    r"""|<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']""",
    re.IGNORECASE,
)


def download_pdf(
    url: str, session: requests.Session, follow_landing: bool = True, headers: dict[str, str] | None = None
) -> bytes:
    candidate = session.get(url, timeout=(10, 45), allow_redirects=True, headers=headers)
    candidate.raise_for_status()
    content = candidate.content
    if b"%PDF-" in content[:1024]:
        return content
    # Página do artigo (HTML): muitas declaram o PDF na meta tag padrão citation_pdf_url.
    if follow_landing and "html" in candidate.headers.get("Content-Type", "").lower():
        match = CITATION_PDF_RE.search(candidate.text)
        if match:
            pdf_url = urljoin(candidate.url, html.unescape(match.group(1) or match.group(2)))
            if pdf_url != url:
                try:
                    return download_pdf(pdf_url, session, follow_landing=False)
                except (requests.RequestException, ValueError) as exc:
                    raise ValueError(f"página indicou {pdf_url}, mas falhou: {exc}") from exc
    raise ValueError("o endereço não retornou um PDF")


def fetch_pdf(doi: str, session: requests.Session, args: argparse.Namespace) -> tuple[bytes, str, str]:
    """Try each source in order; return (content, url, source) for the first valid PDF."""
    tried: set[str] = set()
    lookup_kinds: list[str] = []
    notes: list[str] = []
    download_errors: list[str] = []

    for source in args.sources:
        label = SOURCE_LABELS[source]
        try:
            urls = SOURCE_FUNCS[source](doi, session, args)
        except SourceLookup as exc:
            if exc.kind != "skip":  # "skip": fonte não se aplica a este DOI
                lookup_kinds.append(exc.kind)
                notes.append(str(exc))
            continue
        new_urls = [u for u in urls if u not in tried]
        if not new_urls:
            lookup_kinds.append("no_oa")
            notes.append(f"{label}: nenhuma versão aberta" + (" nova" if urls else ""))
            continue
        extra_headers = SOURCE_DOWNLOAD_HEADERS[source]() if source in SOURCE_DOWNLOAD_HEADERS else None
        for url in new_urls:
            tried.add(url)
            try:
                content = download_pdf(url, session, follow_landing=extra_headers is None, headers=extra_headers)
                if source == "elsevier" and pdf_page_count(content) == 1:
                    raise ValueError("a Elsevier devolveu só a 1ª página (acesso completo negado)")
                return content, url, source
            except (requests.RequestException, ValueError) as exc:
                download_errors.append(f"[{label}] {url}: {exc}")

    detail = " | ".join(notes + download_errors)
    if download_errors:
        raise ArticleError("download_failed", "Versões encontradas, mas nenhum PDF válido foi baixado. " + detail)
    if "no_oa" in lookup_kinds:
        raise ArticleError("not_open_access", detail)
    if lookup_kinds and all(kind == "not_found" for kind in lookup_kinds):
        raise ArticleError("not_found", detail)
    raise ArticleError("api_error", detail)


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _best_title_match(title: str, candidates: list[tuple[str, str]]) -> str | None:
    """candidates: (doi, title). Return the DOI whose title is close enough to ours."""
    wanted = normalize_title(title)
    best_doi, best_score = None, 0.0
    for doi, cand_title in candidates:
        score = difflib.SequenceMatcher(None, wanted, normalize_title(cand_title)).ratio()
        if doi and score > best_score:
            best_doi, best_score = doi, score
    return best_doi if best_score >= TITLE_MATCH_THRESHOLD else None


def find_doi_by_title(title: str, session: requests.Session, args: argparse.Namespace) -> tuple[str, str] | None:
    """Look up a DOI by exact-ish title on OpenAlex, then Crossref. Returns (doi, source label)."""
    words = normalize_title(title)
    if len(words) < 15:  # títulos muito curtos geram falsos positivos
        return None

    params: dict[str, Any] = {"filter": f"title.search:{words}", "per_page": 5, "select": "doi,title", "mailto": args.email}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    try:
        data = _get_json(session, "OpenAlex", OPENALEX_SEARCH_URL, params=params)
        found = _best_title_match(title, [(r.get("doi") or "", r.get("title") or "") for r in data.get("results") or []])
        if found:
            return normalize_doi(found), "OpenAlex"
    except (SourceLookup, ArticleError):
        pass

    try:
        data = _get_json(
            session, "Crossref", CROSSREF_SEARCH_URL,
            params={"query.bibliographic": title, "rows": 5, "select": "DOI,title", "mailto": args.email},
        )
        items = (data.get("message") or {}).get("items") or []
        found = _best_title_match(title, [(i.get("DOI") or "", (i.get("title") or [""])[0]) for i in items])
        if found:
            return normalize_doi(found), "Crossref"
    except (SourceLookup, ArticleError):
        pass
    return None


def process_csv(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    download_dir = Path(args.download_dir).expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)
    report_path = download_dir / "results.csv"

    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"title", "abstract", "doi"}
        headers = {h.strip().lower() for h in (reader.fieldnames or [])}
        missing = required - headers
        if missing:
            raise ValueError("Colunas ausentes no CSV: " + ", ".join(sorted(missing)))
        rows = list(reader)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, application/pdf;q=0.9, */*;q=0.8"})
    existing = index_existing_pdfs(download_dir)
    seen_dois: set[str] = set()
    counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}

    # O relatório é gravado linha a linha: se a execução for interrompida, o que já foi feito fica registrado.
    with report_path.open("w", encoding="utf-8-sig", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=REPORT_FIELDS)
        writer.writeheader()

        for index, row in enumerate(rows, start=1):
            row = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
            title, doi = row.get("title", ""), row.get("doi", "")
            reason, message, source_url, file_name, source = "error", "", "", "", ""
            doi_found, note = "", ""
            used_network = False
            try:
                try:
                    normalized = normalize_doi(doi)
                except ArticleError:
                    if not (args.doi_lookup and title):
                        raise
                    used_network = True
                    result = find_doi_by_title(title, session, args)
                    if not result:
                        problem = "DOI vazio" if not doi else f"DOI inválido ({doi!r})"
                        raise ArticleError("invalid_doi", f"{problem} e nenhum DOI correspondente foi encontrado pelo título.")
                    normalized, finder = result
                    doi_found = normalized
                    note = f"DOI encontrado pelo título via {finder}. "
                doi_key = normalized.lower()
                if doi_key in seen_dois:
                    raise ArticleError("duplicate", "DOI duplicado na lista; esta ocorrência foi ignorada.")
                seen_dois.add(doi_key)

                found = find_existing(existing, normalized)
                if found:
                    reason, file_name = "already_downloaded", found.name
                    message = "Arquivo já existe; download ignorado."
                else:
                    used_network = True
                    content, source_url, source = fetch_pdf(normalized, session, args)
                    local_path = download_dir / safe_filename(title, normalized)
                    tmp_path = local_path.with_name(local_path.name + ".part")
                    tmp_path.write_bytes(content)
                    tmp_path.replace(local_path)  # evita PDFs pela metade se o script for interrompido
                    existing[local_path.name.lower()] = local_path
                    reason, file_name = "downloaded", local_path.name
                    message = f"PDF salvo no computador (fonte: {SOURCE_LABELS[source]})."
            except ArticleError as exc:
                reason, message = exc.reason, str(exc)
            except Exception as exc:  # Continue processing the remaining bibliography.
                reason, message = "error", str(exc)
            message = note + message

            status = "ok" if reason in ("downloaded", "already_downloaded") else "failed"
            counts[reason] = counts.get(reason, 0) + 1
            if source:
                source_counts[source] = source_counts.get(source, 0) + 1
            writer.writerow({
                "title": title, "doi": doi, "doi_found": doi_found, "status": status, "reason": reason,
                "file": file_name, "source": source, "source_url": source_url, "message": message,
            })
            report.flush()
            suffix = f" ({SOURCE_LABELS[source]})" if source else ""
            if status != "ok":
                suffix = f" — {message}"
            print(f"[{index}/{len(rows)}] {REASON_LABELS[reason]}: {title or doi}{suffix}")
            if used_network and index < len(rows):
                time.sleep(args.delay)

    total = len(rows)
    ok = counts.get("downloaded", 0) + counts.get("already_downloaded", 0)
    print("\nResumo:")
    for reason, label in REASON_LABELS.items():
        if counts.get(reason):
            print(f"  {label}: {counts[reason]}")
    for src in args.sources:
        if source_counts.get(src):
            print(f"    via {SOURCE_LABELS[src]}: {source_counts[src]}")
    print(f"Concluído: {ok}/{total} artigos disponíveis na pasta. Relatório: {report_path.resolve()}")
    return 0 if ok == total else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baixa versões de acesso aberto de artigos por DOI para uma pasta neste computador."
    )
    parser.add_argument("--input", required=True, help="CSV com as colunas title, abstract e doi")
    parser.add_argument("--email", required=True, help="E-mail de contato enviado ao Unpaywall e ao OpenAlex")
    parser.add_argument(
        "--sources", default=None,
        help="Fontes em ordem, separadas por vírgula (padrão: unpaywall,openalex,semanticscholar"
             " e, se ELSEVIER_API_KEY estiver definida, elsevier)",
    )
    parser.add_argument(
        "--no-doi-lookup", dest="doi_lookup", action="store_false",
        help="Não procurar pelo título o DOI de linhas com DOI vazio ou inválido",
    )
    parser.add_argument("--download-dir", default="artigos_baixados", help="Pasta local para PDFs e relatório")
    parser.add_argument("--delay", type=float, default=1.0, help="Pausa entre artigos em segundos (padrão: 1)")
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay não pode ser negativo")
    has_elsevier_key = bool(os.environ.get("ELSEVIER_API_KEY"))
    if args.sources is None:
        args.sources = ",".join(src for src in ALL_SOURCES if src != "elsevier" or has_elsevier_key)
    args.sources = [src.strip().lower() for src in args.sources.split(",") if src.strip()]
    if "elsevier" in args.sources and not has_elsevier_key:
        parser.error("a fonte elsevier exige a variável de ambiente ELSEVIER_API_KEY")
    unknown = [src for src in args.sources if src not in SOURCE_FUNCS]
    if unknown or not args.sources:
        parser.error("fontes inválidas: " + ", ".join(unknown) + f" (use: {', '.join(ALL_SOURCES)})")
    try:
        return process_csv(args)
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
