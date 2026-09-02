import unittest

from todo import TodoList


class TodoListTests(unittest.TestCase):
    def setUp(self):
        self.todos = TodoList([
            {"title": "write demo", "done": False},
            {"title": "ship project", "done": False},
            {"title": "record video", "done": False},
        ])

    def test_add(self):
        self.todos.add("answer questions")
        self.assertEqual(len(self.todos.items), 4)

    def test_remove_middle_item(self):
        removed = self.todos.remove(1)
        self.assertEqual(removed["title"], "ship project")
        self.assertEqual(len(self.todos.items), 2)

    def test_remove_first_item(self):
        removed = self.todos.remove(0)
        self.assertEqual(removed["title"], "write demo")

    def test_remove_last_item(self):
        removed = self.todos.remove(2)
        self.assertEqual(removed["title"], "record video")
        self.assertEqual(len(self.todos.items), 2)

    def test_invalid_index_is_safe(self):
        self.assertIsNone(self.todos.remove(9))


if __name__ == "__main__":
    unittest.main()
