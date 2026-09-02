"""Tiny Todo domain used by the AURORA TRACE demonstration."""


class TodoList:
    def __init__(self, items=None):
        self.items = [item if isinstance(item, dict) else {"title": item, "done": False}
                      for item in (items or [])]

    def add(self, title):
        self.items.append({"title": title, "done": False})

    def remove(self, index):
        # Intentional defect: the last item cannot be removed correctly.
        if 0 <= index < len(self.items) - 1:
            return self.items.pop(index)
        return None

    def completed(self):
        return [item for item in self.items if item["done"]]
