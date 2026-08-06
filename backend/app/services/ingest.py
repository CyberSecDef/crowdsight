"""The ingestion pipeline, as jobs a task runner can execute.

Parse, chunk, propose an ontology, extract, persist. Split into two halves so
the operator can review the ontology before extraction spends real inference
time on it — a bad ontology wastes the most expensive stage in the pipeline.

The halves communicate through the filesystem rather than through memory. Phase
one writes ``document.txt`` and ``ontology.json``; phase two reads them back and
re-chunks. That works because chunking is deterministic: the same text and the
same settings produce byte-identical chunks, so offsets stay valid across a
restart or a review that takes a week.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import Config
from app.services.graph_builder import GraphBuilder
from app.services.ontology_generator import Ontology, OntologyGenerator
from app.services.tasks import TaskProgress
from app.storage.embedding_service import EmbeddingService
from app.storage.graph_storage import document_path, ontology_path
from app.storage.ner_extractor import NERExtractor
from app.utils.chunker import chunk_text
from app.utils.file_parser import ParsedDocument, parse_bytes
from app.utils.llm_client import LLMClient

logger = logging.getLogger(__name__)

__all__ = ["build_graph_job", "extract_and_build_job", "propose_ontology_job"]


def _persist_document(
    graph_id: str, document: ParsedDocument, data_dir: Path | None
) -> Path:
    path = document_path(graph_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document.text, encoding="utf-8")
    return path


async def propose_ontology_job(
    progress: TaskProgress,
    *,
    graph_id: str,
    data: bytes,
    filename: str,
    config: Config,
    llm: LLMClient,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Phase one: parse and propose an ontology, then wait for approval."""
    progress.update(stage="parse", progress=0.05, message=f"Parsing {filename}",
                    graph_id=graph_id)
    document = parse_bytes(data, filename, config)
    _persist_document(graph_id, document, data_dir)

    progress.update(stage="chunk", progress=0.15,
                    message=f"{document.char_count:,} characters")
    chunks = chunk_text(document.text, config=config)

    progress.update(stage="ontology", progress=0.25,
                    message=f"Proposing an ontology from {len(chunks)} chunks")
    ontology = await OntologyGenerator(config, llm=llm).generate(document.text)
    ontology.save(ontology_path(graph_id, data_dir))

    result = {
        "graph_id": graph_id,
        "filename": document.filename,
        "char_count": document.char_count,
        "chunk_count": len(chunks),
        "ontology": ontology.model_dump(),
    }
    progress.await_review(
        result,
        f"Proposed {len(ontology.entity_types)} entity types. Review and POST "
        f"the ontology to start extraction.",
    )
    return result


async def extract_and_build_job(
    progress: TaskProgress,
    *,
    graph_id: str,
    config: Config,
    llm: LLMClient,
    embeddings: EmbeddingService,
    builder: GraphBuilder,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Phase two: extract against the approved ontology and persist."""
    path = document_path(graph_id, data_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"No stored document for graph {graph_id!r}. Upload it again; the "
            f"ontology review step writes it before pausing."
        )
    text = path.read_text(encoding="utf-8")
    ontology = Ontology.load(ontology_path(graph_id, data_dir))

    progress.update(stage="chunk", progress=0.3, message="Re-chunking", graph_id=graph_id)
    chunks = chunk_text(text, config=config)

    progress.update(stage="extract", progress=0.35,
                    message=f"Extracting from {len(chunks)} chunks")
    extractor = NERExtractor(ontology, config, llm=llm, embeddings=embeddings)
    extraction = await extractor.extract(chunks)

    progress.update(stage="persist", progress=0.85, message=extraction.summary())
    document = ParsedDocument(
        text=text, filename=_stored_filename(graph_id, data_dir), extension="txt",
        byte_size=len(text.encode("utf-8")), char_count=len(text),
    )
    built = await builder.build(
        graph_id=graph_id, document=document, chunks=chunks,
        ontology=ontology, extraction=extraction, replace=True,
    )
    return {
        "graph_id": graph_id,
        "entities": built.entities,
        "relationships": built.relationships,
        "chunks": built.chunks,
        "mentions": built.mentions,
        "extraction": extraction.summary(),
    }


async def build_graph_job(
    progress: TaskProgress,
    *,
    graph_id: str,
    data: bytes,
    filename: str,
    config: Config,
    llm: LLMClient,
    embeddings: EmbeddingService,
    builder: GraphBuilder,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """The whole pipeline in one job, for callers not reviewing the ontology."""
    progress.update(stage="parse", progress=0.05, message=f"Parsing {filename}",
                    graph_id=graph_id)
    document = parse_bytes(data, filename, config)
    _persist_document(graph_id, document, data_dir)

    progress.update(stage="chunk", progress=0.1,
                    message=f"{document.char_count:,} characters")
    chunks = chunk_text(document.text, config=config)

    progress.update(stage="ontology", progress=0.2,
                    message=f"Proposing an ontology from {len(chunks)} chunks")
    ontology = await OntologyGenerator(config, llm=llm).generate(document.text)
    ontology.save(ontology_path(graph_id, data_dir))

    progress.update(stage="extract", progress=0.35,
                    message=f"Extracting from {len(chunks)} chunks")
    extractor = NERExtractor(ontology, config, llm=llm, embeddings=embeddings)
    extraction = await extractor.extract(chunks)

    progress.update(stage="persist", progress=0.85, message=extraction.summary())
    built = await builder.build(
        graph_id=graph_id, document=document, chunks=chunks,
        ontology=ontology, extraction=extraction, replace=True,
    )
    return {
        "graph_id": graph_id,
        "filename": document.filename,
        "entities": built.entities,
        "relationships": built.relationships,
        "chunks": built.chunks,
        "mentions": built.mentions,
        "extraction": extraction.summary(),
    }


def _stored_filename(graph_id: str, data_dir: Path | None) -> str:
    """Best-effort original filename, for the Document node on rebuild."""
    return f"{graph_id}.txt"
