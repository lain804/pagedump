import unittest

from pagedump.monitor import categorize


class CategorizeTests(unittest.TestCase):
    def test_extensionless_assets_use_mime(self):
        for mime, category in (("image/avif", "images"), ("font/woff2", "fonts"),
                               ("audio/ogg", "audio"), ("video/mp4", "video"),
                               ("application/pdf", "documents"),
                               ("text/css; charset=utf-8", "css"),
                               ("APPLICATION/JAVASCRIPT", "scripts")):
            with self.subTest(mime=mime):
                self.assertEqual(categorize("fetch", "https://example.com/asset", mime), category)

    def test_mime_overrides_misleading_extension(self):
        self.assertEqual(categorize("image", "https://example.com/file.php", "image/png"), "images")
        self.assertEqual(categorize("fetch", "https://example.com/file.js", "video/mp4"), "video")

    def test_fallbacks_and_encoded_extensions(self):
        for resource, path, category in (("media", "stream", "media"),
                                          ("image", "asset", "images"),
                                          ("fetch", "photo%2Epng?v=1", "images"),
                                          ("fetch", "report.PDF", "documents"),
                                          ("xhr", "events", None)):
            with self.subTest(path=path):
                self.assertEqual(categorize(resource, "https://example.com/" + path), category)
