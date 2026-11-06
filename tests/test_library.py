import tempfile
import unittest
from pathlib import Path
from unittest import mock

from digital_life import library


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir(parents=True)
        patcher = mock.patch.object(library, "root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        path = Path(self.tmp.name) / name
        path.write_text(text, "utf-8")
        return path

    def test_ingest_and_page_through_a_text_file(self):
        body = "\n".join(f"第{index}段。" + "字" * 60 for index in range(20))
        meta = library.ingest(str(self.write("rubbish-novel.txt", body)))
        self.assertEqual(meta["title"], "rubbish-novel.txt")
        first = library.read(meta, 0, 300)
        self.assertEqual(first["part"], 0)
        self.assertIn("第0段", first["text"])
        seen = [first["text"]]
        while first["next_part"] is not None:
            first = library.read(meta, first["next_part"], 300)
            seen.append(first["text"])
        joined = "".join(seen)
        for index in range(20):
            self.assertIn(f"第{index}段", joined)
        self.assertGreater(len(seen), 1)

    def test_read_without_part_resumes_where_it_stopped(self):
        meta = library.ingest(str(self.write("book.txt", "\n".join(f"第{i}段" + "字" * 50 for i in range(10)))))
        first = library.read(meta, 0, 400)
        self.assertEqual(first["part"], 0)
        second = library.read(meta, None, 400)
        self.assertEqual(second["part"], 1)
        self.assertEqual(second["progress"], "2/" + str(second["pages"]))
        self.assertNotEqual(first["text"], second["text"])

    def test_read_reports_finished_at_the_end(self):
        meta = library.ingest(str(self.write("short.txt", "只有一页")))
        page = library.read(meta, None, 1000)
        self.assertTrue(page["finished"])
        self.assertIsNone(page["next_part"])

    def test_ingest_rejects_unsupported_file_and_missing_path(self):
        with self.assertRaises(ValueError):
            library.ingest(str(self.write("a.pdf", "%PDF-1.4")))
        with self.assertRaises(ValueError):
            library.ingest(str(Path(self.tmp.name) / "nope.txt"))

    def test_ingest_rejects_empty_source(self):
        with self.assertRaises(ValueError):
            library.ingest(str(self.write("empty.txt", "   ")))

    def test_ingest_url_uses_the_injected_fetcher(self):
        meta = library.ingest("https://example.com/a.txt", fetcher=lambda url: "一章\n\n二章".encode())
        self.assertEqual(meta["chars"], 6)
        page = library.read(meta, 0, 500)
        self.assertIn("一章", page["text"])

    def test_books_lists_every_ingested_source(self):
        library.ingest(str(self.write("one.txt", "第一本")))
        library.ingest(str(self.write("two.txt", "第二本")))
        titles = {book["title"] for book in library.books()}
        self.assertEqual(titles, {"one.txt", "two.txt"})


if __name__ == "__main__":
    unittest.main()
