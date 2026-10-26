import os
import uuid


DEFAULT_CHUNK_SIZE = 4096  


def chunk_file(file_path: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list:
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"No such file: {file_path}")

    filename = os.path.basename(file_path)
    chunks = []

    with open(file_path, "rb") as f:
        index = 0
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            chunk_id = f"{filename}::chunk{index}::{uuid.uuid4().hex[:8]}"
            chunks.append({
                "chunk_id": chunk_id,
                "index": index,
                "data": data,
                "size": len(data),
            })
            index += 1

    return chunks
