from sh_cadi.chunking.chunker import chunk_file
from sh_cadi.hashing.hasher import compute_hash
from sh_cadi.hash_index.hashtable import HashTable
from sh_cadi.storage.cloud_storage import CloudStorage


class ContentAddressableStorage:

    def __init__(self, chunk_size: int = 4096, storage_root: str = "CloudStorage", storage=None):
        self.chunk_size = chunk_size
        self.hash_table = HashTable()
        self.storage = storage if storage is not None else CloudStorage(root=storage_root)
        self.file_manifest = {}   
        self.upload_log = []      
        self.index_listener = None   # optional: object with on_upsert(hash, entry)

    def upload_file(self, file_path: str) -> dict:
        chunks = chunk_file(file_path, self.chunk_size)

        manifest = []
        unique_chunks = 0
        duplicate_chunks = 0
        bytes_uploaded = 0
        bytes_saved = 0

        for chunk in chunks:
            chunk_hash = compute_hash(chunk["data"])
            if self.index_listener is not None and hasattr(self.index_listener, "before_write"):
                self.index_listener.before_write(chunk_hash)   # verify/heal before reading or mutating
            bytes_uploaded += chunk["size"]

            existing_entry = self.hash_table.search(chunk_hash)

            if existing_entry is not None:
                existing_entry["ref_count"] += 1
                existing_entry["referenced_by"].append(chunk["chunk_id"])
                self.hash_table.update(chunk_hash, existing_entry)
                if self.index_listener:
                    self.index_listener.on_upsert(chunk_hash, existing_entry)
                duplicate_chunks += 1
                bytes_saved += chunk["size"]
            else:
                location = self.storage.store_chunk(chunk_hash, chunk["data"])
                entry = {
                    "chunk_id": chunk["chunk_id"],
                    "storage_location": location,
                    "size": chunk["size"],
                    "ref_count": 1,
                    "referenced_by": [chunk["chunk_id"]],
                }
                self.hash_table.insert(chunk_hash, entry)
                if self.index_listener:
                    self.index_listener.on_upsert(chunk_hash, entry)
                unique_chunks += 1

            manifest.append(chunk_hash)

        self.file_manifest[file_path] = manifest

        report = {
            "file_path": file_path,
            "total_chunks": len(chunks),
            "unique_chunks": unique_chunks,
            "duplicate_chunks": duplicate_chunks,
            "bytes_uploaded": bytes_uploaded,
            "bytes_saved": bytes_saved,
            "manifest": manifest,
        }
        self.upload_log.append(report)
        return report

    def reconstruct_file(self, file_path: str, output_path: str) -> None:
        manifest = self.file_manifest.get(file_path)
        if manifest is None:
            raise KeyError(f"No manifest recorded for {file_path}")

        with open(output_path, "wb") as out:
            for chunk_hash in manifest:
                out.write(self.storage.read_chunk(chunk_hash))

    def hash_table_dump(self) -> list:
        return list(self.hash_table.items())

    def global_stats(self) -> dict:
        total_logical_bytes = sum(r["bytes_uploaded"] for r in self.upload_log)
        total_saved_bytes = sum(r["bytes_saved"] for r in self.upload_log)
        return {
            "files_uploaded": len(self.upload_log),
            "unique_chunks_stored": len(self.hash_table),
            "chunks_on_disk": self.storage.chunk_count_on_disk(),
            "total_logical_bytes": total_logical_bytes,
            "total_saved_bytes": total_saved_bytes,
            "dedup_ratio": (
                round(total_saved_bytes / total_logical_bytes * 100, 2)
                if total_logical_bytes else 0.0
            ),
        }
