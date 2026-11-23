#!/usr/bin/env python3
r"""
FINANCE KNOWLEDGE BASE WATCHER & DUCKDB INDEXER
================================================
Overvåker "G:\My Drive\AI trader v2\Finance Knowledge Base" (eller lokal sti)
og indekserer nye/endrede dokumenter (.pdf, .md, .txt, .json) inkrementelt inn i DuckDB.

Nøkkelfunksjoner:
  1. Inkrementell indeksering med SHA-256 hash (hopper over uendrede filer).
  2. Tekstekstraksjon fra PDF (pypdf/pdftotext), Markdown og ren tekst.
  3. Semantisk avsnittsoppdeling (chunking) for rask kontekst-henting i trading-agenter.
  4. Løpende bakgrunnsovervåking (polling) med egendefinert intervall (f.eks. hvert 60. sekund).
  5. Søkefunksjon (search_knowledge_base) integrert i både alpaca_paper_runner.py, runner_server.py og AI Chat.
"""

import os
import sys
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

import duckdb

# Konfigurer logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("KBWatcher")

BASE_DIR = Path(__file__).resolve().parent


def _default_kb_db_path() -> str:
    """Bruker %LOCALAPPDATA%/ai_trader_runtime på Windows for å unngå Google Drive-fillåsing."""
    env_path = os.getenv("KB_DB_PATH")
    if env_path:
        Path(env_path).parent.mkdir(parents=True, exist_ok=True)
        return env_path
    local_app = os.getenv("LOCALAPPDATA")
    if os.name == "nt" and local_app:
        p = Path(local_app) / "ai_trader_runtime"
        p.mkdir(parents=True, exist_ok=True)
        return str(p / "knowledge_base.duckdb")
    p = BASE_DIR / "data"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "knowledge_base.duckdb")


# Standardstier
DEFAULT_KB_DIR = os.getenv("KB_DIR", r"G:\My Drive\AI trader v2\Finance Knowledge Base")
DB_PATH = _default_kb_db_path()
SCAN_INTERVAL_SEC = int(os.getenv("KB_SCAN_INTERVAL", "60"))


def get_file_hash(filepath: Path) -> str:
    """Beregner SHA-256 sjekksum for å oppdage endringer i filen."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def init_db(db_path: str = DB_PATH):
    """Oppretter tabeller for dokumenter og tekstbiter i DuckDB."""
    with duckdb.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS indexed_files (
                filepath VARCHAR PRIMARY KEY,
                filename VARCHAR,
                file_hash VARCHAR,
                file_type VARCHAR,
                file_size_bytes BIGINT,
                last_modified TIMESTAMP,
                indexed_at TIMESTAMP,
                total_chunks INTEGER
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                chunk_id VARCHAR PRIMARY KEY,
                filepath VARCHAR,
                filename VARCHAR,
                chunk_index INTEGER,
                content TEXT,
                char_count INTEGER,
                created_at TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_file ON document_chunks(filepath);
        """)
    logger.info("✅ DuckDB Knowledge Base initialisert: %s", db_path)


def extract_text_from_file(filepath: Path) -> str:
    """Trekker ut råtekst basert på filtype."""
    ext = filepath.suffix.lower()
    text = ""
    try:
        if ext in (".txt", ".md", ".csv", ".json"):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        elif ext == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(filepath))
                pages_text = []
                for idx, page in enumerate(reader.pages):
                    t = page.extract_text()
                    if t:
                        pages_text.append(f"--- Side {idx + 1} ---\n" + t)
                text = "\n\n".join(pages_text)
            except Exception as e:
                logger.warning("pypdf feilet for %s: %s. Prøver pdftotext...", filepath.name, e)
                import subprocess
                res = subprocess.run(
                    ["pdftotext", str(filepath), "-"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if res.returncode == 0:
                    text = res.stdout
        else:
            logger.debug("Hopper over filformat som ikke støttes: %s", filepath.name)
    except Exception as e:
        logger.error("Feil ved tekstuttrekking fra %s: %s", filepath.name, e)

    return text.strip()


def chunk_text(text: str, max_chars: int = 1500, overlap: int = 200) -> List[str]:
    """Deler lang tekst inn i overlappende avsnitt for RAG-oppslag."""
    if not text:
        return []
    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk: List[str] = []
    current_len = 0

    for p in paragraphs:
        p_clean = p.strip()
        if not p_clean:
            continue
        if current_len + len(p_clean) > max_chars and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [current_chunk[-1]] if len(current_chunk) > 1 else []
            current_len = sum(len(x) for x in current_chunk)

        current_chunk.append(p_clean)
        current_len += len(p_clean)

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def index_single_file(filepath: Path, db_path: str = DB_PATH) -> bool:
    """Indekserer eller oppdaterer en enkelt fil i DuckDB."""
    try:
        file_hash = get_file_hash(filepath)
        stat = filepath.stat()
        last_mod = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        with duckdb.connect(db_path) as conn:
            row = conn.execute(
                "SELECT file_hash FROM indexed_files WHERE filepath = ?", [str(filepath)]
            ).fetchone()
            if row and row[0] == file_hash:
                return False

        logger.info("🔄 Prosesserer endret eller ny fil: %s", filepath.name)
        content = extract_text_from_file(filepath)
        if not content:
            logger.warning("⚠️ Ingen tekst kunne trekkes ut fra %s", filepath.name)
            return False

        chunks = chunk_text(content)

        with duckdb.connect(db_path) as conn:
            conn.execute("DELETE FROM document_chunks WHERE filepath = ?", [str(filepath)])
            conn.execute("DELETE FROM indexed_files WHERE filepath = ?", [str(filepath)])

            conn.execute(
                """
                INSERT INTO indexed_files VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                [
                    str(filepath),
                    filepath.name,
                    file_hash,
                    filepath.suffix.lower(),
                    stat.st_size,
                    last_mod,
                    len(chunks),
                ],
            )

            for idx, chunk in enumerate(chunks):
                chunk_id = f"{file_hash[:12]}_{idx:04d}"
                conn.execute(
                    """
                    INSERT INTO document_chunks VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    [chunk_id, str(filepath), filepath.name, idx, chunk, len(chunk)],
                )

        logger.info("✅ Fullført indeksering: %s (%d chunks lagret)", filepath.name, len(chunks))
        return True
    except Exception as e:
        logger.error("Kunne ikke indeksere %s: %s", filepath.name, e)
        return False


def scan_knowledge_base(kb_dir: Path = Path(DEFAULT_KB_DIR), db_path: str = DB_PATH) -> int:
    """Skanner mappen rekursivt for dokumenter og indekserer nye/endrede."""
    init_db(db_path)
    dirs_to_scan = []
    if kb_dir.exists():
        dirs_to_scan.append(kb_dir)
    else:
        logger.warning("KB-mappen finnes ikke: %s (faller tilbake på lokal docs/)", kb_dir)
    local_docs = BASE_DIR / "docs"
    if local_docs.exists():
        dirs_to_scan.append(local_docs)

    supported_extensions = {".pdf", ".md", ".txt", ".json"}
    updated_count = 0
    for d in dirs_to_scan:
        all_files = [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in supported_extensions]
        for f in all_files:
            if index_single_file(f, db_path):
                updated_count += 1
    return updated_count


def get_kb_status(db_path: str = DB_PATH) -> Dict[str, Any]:
    """Returnerer antall indekserte filer og chunks i DuckDB Knowledge Base."""
    init_db(db_path)
    with duckdb.connect(db_path) as conn:
        files_row = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_chunks), 0) FROM indexed_files").fetchone()
        recent = conn.execute(
            "SELECT filename, file_type, total_chunks, CAST(indexed_at AS VARCHAR) FROM indexed_files ORDER BY indexed_at DESC LIMIT 10"
        ).fetchall()
    return {
        "kb_dir": DEFAULT_KB_DIR,
        "db_path": db_path,
        "indexed_files": int(files_row[0] or 0) if files_row else 0,
        "total_chunks": int(files_row[1] or 0) if files_row else 0,
        "recent_files": [
            {"filename": r[0], "type": r[1], "chunks": r[2], "indexed_at": str(r[3])[:19]}
            for r in recent
        ],
    }


def search_knowledge_base(query: str, top_k: int = 5, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Fulltekstsøk over indekserte chunks (støtter både eksakt frase og nøkkelord)."""
    q = (query or "").strip()
    if not q:
        return []
    init_db(db_path)
    words = [w for w in q.replace("?", "").replace(",", " ").split() if len(w) >= 3][:5]
    with duckdb.connect(db_path) as conn:
        res = conn.execute(
            """
            SELECT filename, chunk_index, content
            FROM document_chunks
            WHERE lower(content) LIKE lower(?)
            LIMIT ?
            """,
            [f"%{q}%", top_k],
        ).fetchall()
        if not res and words:
            clauses = " OR ".join("lower(content) LIKE lower(?)" for _ in words)
            params = [f"%{w}%" for w in words] + [top_k]
            res = conn.execute(
                f"SELECT filename, chunk_index, content FROM document_chunks WHERE {clauses} LIMIT ?",
                params,
            ).fetchall()
        return [
            {"filename": row[0], "chunk_index": row[1], "content": row[2]}
            for row in res
        ]


def run_continuous_watcher(kb_path_str: str = DEFAULT_KB_DIR, interval_sec: int = SCAN_INTERVAL_SEC):
    """Kjører kontinuerlig overvåking i bakgrunnen."""
    kb_path = Path(kb_path_str)
    init_db()

    logger.info("=" * 70)
    logger.info("KNOWLEDGE BASE WATCHER AKTIV")
    logger.info("Overvåker mappe : %s", kb_path)
    logger.info("Database        : %s", DB_PATH)
    logger.info("Sjekkintervall  : hvert %d. sekund", interval_sec)
    logger.info("=" * 70)

    while True:
        try:
            changes = scan_knowledge_base(kb_path)
            if changes > 0:
                logger.info("🎉 %d fil(er) ble indeksert eller oppdatert.", changes)
        except Exception as e:
            logger.error("Feil under periodisk skanning: %s", e)

        time.sleep(interval_sec)


if __name__ == "__main__":
    if "--once" in sys.argv:
        n = scan_knowledge_base(Path(DEFAULT_KB_DIR))
        print(f"Ferdig engangsskanning: {n} fil(er) oppdatert.", get_kb_status())
    else:
        target_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KB_DIR
        run_continuous_watcher(target_dir)
