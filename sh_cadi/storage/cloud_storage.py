import os


class CloudStorage:
    def __init__(self, root: str = "CloudStorage"):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def _path_for(self, chunk_hash: str) -> str:
        return os.path.join(self.root, chunk_hash)

    def store_chunk(self, chunk_hash: str, data: bytes) -> str:
        path = self._path_for(chunk_hash)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
        return path

    def read_chunk(self, chunk_hash: str) -> bytes:
        path = self._path_for(chunk_hash)
        with open(path, "rb") as f:
            return f.read()

    def exists(self, chunk_hash: str) -> bool:
        return os.path.exists(self._path_for(chunk_hash))

    def chunk_count_on_disk(self) -> int:
        return len(os.listdir(self.root))
