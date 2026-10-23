"""Загрузка эмбеддингов из п.3 в FAISS (п.4).

Запуск из lab2/src:
    python -m index.load_to_vector_store                       # latest run из artifacts/latest.json
    python -m index.load_to_vector_store --run-id <run_id> --set index.hnsw.M=16
    python -m index.load_to_vector_store --rebuild | --drop-and-reindex | --reset [--all] | --list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import faiss
import numpy as np

from .config import Config, load_config

MANIFEST = "manifest.json"
INDEX_FILE = "index.faiss"
DOCSTORE_FILE = "docstore.jsonl"
ACTIVE_FILE = "active.json"

_QUANTIZERS = {
    "fp16": faiss.ScalarQuantizer.QT_fp16,
    "sq8": faiss.ScalarQuantizer.QT_8bit,
}


# ---------- имена и пути ----------

def resolve_run_id(config: Config, run_id: str | None) -> str:
    if run_id:
        return run_id
    latest = config.artifacts_dir / "latest.json"
    if not latest.is_file():
        raise SystemExit("Нет artifacts/latest.json: сначала запустите python -m index.build_index")
    return json.loads(latest.read_text(encoding="utf-8"))["run_id"]


def collection_name(config: Config, run_id: str) -> str:
    i = config.index
    if i.type == "flat":
        return f"{run_id}__flat"
    suffix = "" if i.quantization == "none" else f"-{i.quantization}"
    return f"{run_id}__hnsw-M{i.hnsw.M}-efc{i.hnsw.ef_construction}{suffix}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def collection_hash(config: Config, embeddings_sha: str) -> str:
    i = config.index
    payload = {
        "type": i.type,
        "metric": i.metric,
        "M": i.hnsw.M,
        "ef_construction": i.hnsw.ef_construction,
        "quantization": i.quantization,
        "embeddings_sha256": embeddings_sha,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def read_active(store_dir: Path) -> str | None:
    path = store_dir / ACTIVE_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("collection")


def write_active(store_dir: Path, name: str) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / ACTIVE_FILE).write_text(json.dumps({"collection": name}, indent=2), encoding="utf-8")


# ---------- построение ----------

def make_index(config: Config, vectors: np.ndarray) -> faiss.Index:
    i = config.index
    dim = vectors.shape[1]
    if i.type == "flat":
        index = faiss.IndexFlatIP(dim)
    elif i.quantization == "none":
        index = faiss.IndexHNSWFlat(dim, i.hnsw.M, faiss.METRIC_INNER_PRODUCT)
    else:
        index = faiss.IndexHNSWSQ(dim, _QUANTIZERS[i.quantization], i.hnsw.M, faiss.METRIC_INNER_PRODUCT)
    if i.type == "hnsw":
        index.hnsw.efConstruction = i.hnsw.ef_construction
    if not index.is_trained:
        index.train(vectors)
    index.add(vectors)
    return index


def set_ef_search(index: faiss.Index, config: Config) -> None:
    if config.index.type == "hnsw":
        faiss.downcast_index(index).hnsw.efSearch = config.index.hnsw.ef_search


def is_complete(directory: Path, digest: str) -> bool:
    manifest = directory / MANIFEST
    if not manifest.is_file() or not (directory / INDEX_FILE).is_file():
        return False
    return json.loads(manifest.read_text(encoding="utf-8")).get("collection_hash") == digest


def build_collection(config: Config, run_id: str, rebuild: bool = False) -> Path:
    run_dir = config.artifacts_dir / run_id
    emb_path, chunks_path = run_dir / "embeddings.npy", run_dir / "chunks.jsonl"
    if not emb_path.is_file() or not chunks_path.is_file():
        raise SystemExit(
            f"В {run_dir} нет embeddings.npy/chunks.jsonl: нужен dense или hybrid запуск п.3 "
            "(sparse индексируется BM25, а не FAISS)"
        )

    store_dir = config.faiss_dir
    name = collection_name(config, run_id)
    target = store_dir / name
    digest = collection_hash(config, file_sha256(emb_path))

    if not rebuild and is_complete(target, digest):
        print(f"Коллекция уже построена: {target} (--rebuild для пересборки)")
        write_active(store_dir, name)
        return target

    started = time.time()
    vectors = np.ascontiguousarray(np.load(emb_path), dtype=np.float32)
    lines = chunks_path.read_text(encoding="utf-8").splitlines()
    if len(lines) != len(vectors):
        raise SystemExit(f"Число чанков ({len(lines)}) не совпадает с числом векторов ({len(vectors)})")

    norms = np.linalg.norm(vectors, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-3):
        print("Векторы не нормализованы: нормализую для cosine")
        vectors = vectors / np.maximum(norms, 1e-12)[:, None]
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)

    index = make_index(config, vectors)
    if index.ntotal != len(vectors):
        raise SystemExit(f"ntotal={index.ntotal}, ожидалось {len(vectors)}")

    set_ef_search(index, config)
    _, found = index.search(vectors, 1)
    self_recall = float(np.mean(found[:, 0] == np.arange(len(vectors))))
    build_seconds = round(time.time() - started, 2)

    tmp = store_dir / f".tmp-{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    faiss.write_index(index, str(tmp / INDEX_FILE))
    with (tmp / DOCSTORE_FILE).open("w", encoding="utf-8") as handle:
        for faiss_id, line in enumerate(lines):
            record = json.loads(line)
            handle.write(json.dumps({"faiss_id": faiss_id, **record}, ensure_ascii=False) + "\n")
    manifest = {
        "collection": name,
        "collection_hash": digest,
        "run_id": run_id,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index": config.to_dict()["index"],
        "ntotal": int(index.ntotal),
        "dim": int(vectors.shape[1]),
        "self_recall_at1": self_recall,
        "build_seconds": build_seconds,
        "index_bytes": (tmp / INDEX_FILE).stat().st_size,
        "faiss_version": faiss.__version__,
    }
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    write_active(store_dir, name)
    print(
        f"Коллекция {name}: ntotal={manifest['ntotal']}, dim={manifest['dim']}, "
        f"self-recall@1={self_recall:.3f}, {manifest['index_bytes'] / 1024:.0f} КБ, {build_seconds} с"
    )
    return target


# ---------- сброс и список ----------

def reset(config: Config, run_id: str | None, everything: bool) -> None:
    store_dir = config.faiss_dir
    if everything:
        shutil.rmtree(store_dir, ignore_errors=True)
        print(f"Хранилище удалено: {store_dir}")
        return
    name = collection_name(config, resolve_run_id(config, run_id))
    shutil.rmtree(store_dir / name, ignore_errors=True)
    if read_active(store_dir) == name:
        (store_dir / ACTIVE_FILE).unlink(missing_ok=True)
    print(f"Коллекция удалена: {name}")


def list_collections(config: Config) -> None:
    store_dir = config.faiss_dir
    active = read_active(store_dir)
    manifests = sorted(store_dir.glob(f"*/{MANIFEST}")) if store_dir.is_dir() else []
    if not manifests:
        print("Коллекций нет")
    for path in manifests:
        m = json.loads(path.read_text(encoding="utf-8"))
        mark = "*" if m["collection"] == active else " "
        print(
            f"{mark} {m['collection']}  ntotal={m['ntotal']}  "
            f"self-recall@1={m['self_recall_at1']:.3f}  {m['index_bytes'] / 1024:.0f} КБ"
        )


# ---------- использование в п.5/п.6 ----------

def load_vector_store(config: Config, collection: str | None = None, embeddings=None):
    """LangChain FAISS поверх готовой коллекции; ef_search берётся из конфига."""
    from langchain_community.docstore.in_memory import InMemoryDocstore
    from langchain_community.vectorstores import FAISS
    from langchain_community.vectorstores.utils import DistanceStrategy
    from langchain_core.documents import Document

    store_dir = config.faiss_dir
    collection = collection or read_active(store_dir)
    if not collection or not (store_dir / collection / INDEX_FILE).is_file():
        raise SystemExit("Нет активной коллекции: запустите python -m index.load_to_vector_store")
    directory = store_dir / collection

    index = faiss.read_index(str(directory / INDEX_FILE))
    set_ef_search(index, config)
    docs, ids = {}, {}
    with (directory / DOCSTORE_FILE).open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            key = str(record["faiss_id"])
            ids[record["faiss_id"]] = key
            docs[key] = Document(page_content=record["text"], metadata=record["metadata"])
    if embeddings is None:
        from .embedders import PrefixedEmbeddings

        embeddings = PrefixedEmbeddings(config.embedding)
    return FAISS(
        embedding_function=embeddings,
        index=index,
        docstore=InMemoryDocstore(docs),
        index_to_docstore_id=ids,
        normalize_L2=False,
        distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка индекса в FAISS (п.4)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--run-id", default=None, help="запуск п.3 (по умолчанию из latest.json)")
    parser.add_argument("--rebuild", action="store_true", help="пересобрать коллекцию")
    parser.add_argument("--drop-and-reindex", action="store_true", help="удалить всё хранилище и построить заново")
    parser.add_argument("--reset", action="store_true", help="удалить коллекцию (с --all всё хранилище)")
    parser.add_argument("--all", action="store_true", help="вместе с --reset: удалить все коллекции")
    parser.add_argument("--list", action="store_true", help="показать коллекции")
    args = parser.parse_args()

    config = load_config(args.config, args.overrides)
    if args.list:
        list_collections(config)
    elif args.reset:
        reset(config, args.run_id, args.all)
    else:
        if args.drop_and_reindex:
            shutil.rmtree(config.faiss_dir, ignore_errors=True)
        build_collection(config, resolve_run_id(config, args.run_id), rebuild=args.rebuild)


if __name__ == "__main__":
    main()
