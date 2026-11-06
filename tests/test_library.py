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
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        for name, value in (("root", self.root), ("inbox_root", self.inbox)):
            patcher = mock.patch.object(library, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, name, text):
        path = self.inbox / name
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
        self.assertTrue(second["progress"].endswith("%"))
        self.assertEqual(second["offset"], first["offset"] + len(second["text"]))
        self.assertNotEqual(first["text"], second["text"])

    def test_the_bookmark_survives_a_change_of_page_width(self):
        body = "".join(f"第{i}段。" + "字" * 40 for i in range(12))
        meta = library.ingest(str(self.write("wide.txt", body)))
        wide = library.read(meta, None, 400)
        narrow = library.read(meta, None, 200)
        self.assertEqual(narrow["text"], body[wide["offset"]:wide["offset"] + len(narrow["text"])])
        self.assertEqual(narrow["offset"], wide["offset"] + len(narrow["text"]))
        self.assertTrue(body.startswith(wide["text"]))

    def test_an_old_page_number_is_read_as_a_character_offset(self):
        body = "字" * 9000
        meta = library.ingest(str(self.write("old.txt", body)))
        meta.pop("offset", None)
        meta["position"] = 1
        page = library.read(meta, None, 1000)
        self.assertEqual(page["offset"], library.MAX_CHARS + len(page["text"]))
        self.assertNotIn("position", meta)

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

    def test_github_blob_url_reads_the_raw_file(self):
        meta = library.ingest("https://github.com/acme/notes/blob/main/docs/a.md",
                              fetcher=lambda url: ("# 标题\n正文" if "raw.githubusercontent.com" in url
                                                   else b"wrong").encode())
        self.assertIn("a.md", meta["title"])
        self.assertIn("正文", library.read(meta, None, 500)["text"])
        self.assertEqual(meta["source"], "https://github.com/acme/notes/blob/main/docs/a.md")

    def test_github_repo_url_returns_readme_plus_a_file_index(self):
        import json as _json
        def fetcher(url):
            if url.startswith("https://api.github.com/repos/acme/notes/contents/"):
                return _json.dumps([{"type": "file", "name": "qa.json", "size": 99},
                                    {"type": "dir", "name": "docs"}]).encode()
            if url.endswith("/main/README.md"):
                return b"# readme"
            raise OSError("404")
        meta = library.ingest("https://github.com/acme/notes", fetcher=fetcher)
        text = library.read(meta, None, 900)["text"]
        self.assertIn("readme", text)
        self.assertIn("qa.json (99 B)", text)
        self.assertIn("docs/", text)

    def test_github_falls_back_when_raw_is_unreachable(self):
        import json as _json
        def fetcher(url):
            if url.startswith("https://raw.githubusercontent.com/"):
                raise OSError("blocked")
            if url.startswith("https://cdn.jsdelivr.net/"):
                raise OSError("blocked too")
            return _json.dumps({"content": "IyBmYWxsYmFjaw=="}).encode()
        meta = library.ingest("https://github.com/acme/notes/blob/main/a.md", fetcher=fetcher)
        self.assertIn("fallback", library.read(meta, None, 200)["text"])

    def test_github_tree_url_lists_text_files(self):
        import json as _json
        payload = _json.dumps([
            {"type": "file", "name": "a.md", "size": 10},
            {"type": "file", "name": "b.bin", "size": 99},
            {"type": "dir", "name": "src"},
        ]).encode()
        meta = library.ingest("https://github.com/acme/notes/tree/main/docs",
                              fetcher=lambda url: payload)
        text = library.read(meta, None, 800)["text"]
        self.assertIn("a.md", text)
        self.assertNotIn("b.bin", text)

    def test_local_source_outside_the_inbox_is_refused(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("不该被读到", "utf-8")
        with self.assertRaises(ValueError) as caught:
            library.ingest(str(outside))
        self.assertIn("收件目录", str(caught.exception))

    def test_relative_name_is_read_from_the_inbox(self):
        (self.inbox / "draft.md").write_text("# 草稿", "utf-8")
        meta = library.ingest("draft.md")
        self.assertEqual(meta["title"], "draft.md")

    def test_symlink_escaping_the_inbox_is_refused(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("不该被读到", "utf-8")
        link = self.inbox / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            library.ingest(str(link))

    def test_loopback_url_is_refused_before_any_request(self):
        with self.assertRaises(ValueError):
            library.ingest("http://127.0.0.1:8765/feed")

    def test_non_http_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            library.ingest("file:///etc/passwd")

    def test_books_lists_every_ingested_source(self):
        library.ingest(str(self.write("one.txt", "第一本")))
        library.ingest(str(self.write("two.txt", "第二本")))
        titles = {book["title"] for book in library.books()}
        self.assertEqual(titles, {"one.txt", "two.txt"})


if __name__ == "__main__":
    unittest.main()
