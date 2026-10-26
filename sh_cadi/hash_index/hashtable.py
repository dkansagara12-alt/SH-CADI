class HashNode:

    __slots__ = ("key", "value", "next")

    def __init__(self, key, value):
        self.key = key
        self.value = value
        self.next = None


class HashTable:

    def __init__(self, capacity: int = 1024):
        self.capacity = capacity
        self.size = 0
        self.buckets = [None] * capacity

    def _bucket_index(self, key: str) -> int:
        return int(key[:8], 16) % self.capacity

    def insert(self, key: str, value: dict) -> None:
        idx = self._bucket_index(key)
        node = self.buckets[idx]
        while node:
            if node.key == key:
                node.value = value
                return
            node = node.next

        new_node = HashNode(key, value)
        new_node.next = self.buckets[idx]
        self.buckets[idx] = new_node
        self.size += 1

        if self.size / self.capacity > 0.75:
            self._resize()

    def search(self, key: str):
        idx = self._bucket_index(key)
        node = self.buckets[idx]
        while node:
            if node.key == key:
                return node.value
            node = node.next
        return None

    def update(self, key: str, value: dict) -> bool:
        idx = self._bucket_index(key)
        node = self.buckets[idx]
        while node:
            if node.key == key:
                node.value = value
                return True
            node = node.next
        return False

    def delete(self, key: str) -> bool:
        idx = self._bucket_index(key)
        node = self.buckets[idx]
        prev = None
        while node:
            if node.key == key:
                if prev:
                    prev.next = node.next
                else:
                    self.buckets[idx] = node.next
                self.size -= 1
                return True
            prev = node
            node = node.next
        return False

    def _resize(self) -> None:
        old_buckets = self.buckets
        self.capacity *= 2
        self.buckets = [None] * self.capacity
        self.size = 0
        for head in old_buckets:
            node = head
            while node:
                self.insert(node.key, node.value)
                node = node.next

    def items(self):
        for head in self.buckets:
            node = head
            while node:
                yield node.key, node.value
                node = node.next

    def __len__(self):
        return self.size

    def load_factor(self) -> float:
        return self.size / self.capacity
